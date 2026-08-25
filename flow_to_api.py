"""
Flow -> Replayable API Client Generator
========================================
mobile_re.py se capture hue session.jsonl se koi bhi flow utha kar
ready-to-run Python client generate karta hai.
Dynamic values (timestamp, nonce, token) ko auto-template karta hai.

Usage:
    python flow_to_api.py re_capture/session.jsonl

Authorized testing only.
"""

import json
import re
import sys
from datetime import datetime

# Dynamic/changing values detect karne ke patterns
DYNAMIC_PATTERNS = [
    (re.compile(r'"(timestamp|ts|time|created_at|request_time|expire)"\s*:\s*"?(\d{10,13})"?'), "TIMESTAMP"),
    (re.compile(r'"(nonce|uuid|request_id|trace_id|correlation_id|session)"\s*:\s*"([^"]+)"'), "UUID"),
    (re.compile(r'Bearer\s+([A-Za-z0-9_\-\.]{20,})'), "AUTH_TOKEN"),
]


def looks_dynamic(value: str) -> bool:
    """Lambe random-looking strings ko bhi dynamic maano"""
    return len(value) > 24 and bool(re.match(r"^[A-Za-z0-9_\-]+$", value))


def extract_template(body: str):
    """Body se variables nikaalo aur ${VAR} placeholders daal do"""
    variables = {}

    def add_var(name_hint, val):
        var_name = f"{name_hint}_{len(variables)}"
        variables[var_name] = val
        return var_name

    # span-based replacement — .replace(...,1) galat occurrence badal deta tha
    # jab same value body me pehle kisi aur jagah aayi ho
    spans = []
    for pat, name in DYNAMIC_PATTERNS:
        for m in pat.finditer(body):
            gi = m.lastindex  # value wala group
            if gi is None:
                continue
            spans.append((m.start(gi), m.end(gi), name, m.group(gi)))
    # overlapping spans drop karo (pehla match jeetega), right-to-left replace
    spans.sort()
    accepted, last_start = [], -1
    for s in spans:
        if s[0] >= last_start:
            accepted.append(s)
            last_start = s[1]
    for start, end, name, val in reversed(accepted):
        var_name = add_var(name, val)
        body = body[:start] + "${" + var_name + "}" + body[end:]

    # Bache hue long quoted strings jo random lagte hain
    def repl(m):
        val = m.group(2)
        if looks_dynamic(val) and "${" not in val:
            var_name = add_var("VALUE", val)
            return f'{m.group(1)}"${{{var_name}}}"'
        return m.group(0)

    body = re.sub(r'"([a-zA-Z_][\w]*)"\s*:\s*"([^"]{25,})"', repl, body)
    return body, variables


def generate_client(record: dict, out_name: str):
    url = record["url"]
    method = record["method"]
    headers = record.get("req_headers", {})
    body_raw = record.get("req_body", "")

    body_templated, variables = "", {}
    if body_raw.strip():
        try:
            json.loads(body_raw)
            body_templated, variables = extract_template(body_raw)
        except Exception:
            body_templated = body_raw  # form-data etc, as-is rakho

    hdr_lines = "\n".join(
        f'    "{k}": "{v}",'
        for k, v in headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
    )
    var_lines = "\n".join(f'    "{k}": "{v}",' for k, v in variables.items())

    code = f'''"""
Auto-generated API client from captured traffic
Source   : {url}
Method   : {method}
Generated: {datetime.now().isoformat()}

Usage:
    python {out_name}
    ya import karke: call(TIMESTAMP_0=str(int(time.time())))
"""
import json as _json
import time
import requests

VARIABLES = {{
{var_lines}
}}

HEADERS = {{
{hdr_lines}
}}

BODY = {_body_literal(body_templated)}

URL = "{url}"
METHOD = "{method}"


def call(**overrides):
    """Ek request fire karo. Dynamic values overrides se replace hoti hain."""
    payload = BODY
    for k, v in overrides.items():
        payload = payload.replace("${{" + k + "}}", str(v))
    r = requests.request(
        method=METHOD,
        url=URL,
        headers=HEADERS,
        data=payload.encode() if isinstance(payload, str) else payload,
        timeout=120,
    )
    print(f"[{{r.status_code}}] {{r.text[:500]}}")
    try:
        return r.json()
    except Exception:
        return r.text


if __name__ == "__main__":
    # auto-refresh: TIMESTAMP vars ki fresh values bhejo
    # (captured value ki length se pata chalta hai seconds 10-digit ya millis 13-digit)
    _auto = {{k: v for k, v in VARIABLES.items() if k.startswith("TIMESTAMP")}}
    for k in sorted(_auto):
        _ts = int(time.time() * 1000) if len(str(_auto[k])) >= 13 else int(time.time())
        _auto[k] = str(_ts)
    result = call(**_auto)
'''
    with open(out_name, "w") as f:
        f.write(code)
    print(f"[+] Generated: {out_name}")
    print(f"[+] Variables found ({len(variables)}): {list(variables.keys())}")


def _body_literal(body: str) -> str:
    """Body ko safe Python literal me daalo"""
    return repr(body)


def main():
    if len(sys.argv) < 2:
        print("usage: flow_to_api.py <session.jsonl>")
        sys.exit(1)

    records = []
    with open(sys.argv[1]) as f:
        for line in f:
            try:
                r = json.loads(line)
                records.append(r)
            except Exception:
                continue

    print(f"\n[+] {len(records)} captured flows:\n")
    for i, r in enumerate(records):
        has_body = "+" if r.get("req_body") else " "
        preview = r["url"][:85]
        print(f"  [{i:3d}] {has_body} {r['method']:6s} {r['status']} {preview}")

    idx = input("\nSelect index (ya 'all' sabke liye): ").strip()
    if idx.lower() == "all":
        for i, rec in enumerate(records):
            safe = re.sub(r"[^a-zA-Z0-9]", "_", rec["url"].split("/")[-1] or "endpoint")[:40]
            try:
                generate_client(rec, f"client_{i}_{safe}.py")
            except Exception as e:
                print(f"[-] flow {i} failed: {e}")
    else:
        rec = records[int(idx)]
        safe = re.sub(r"[^a-zA-Z0-9]", "_", rec["url"].split("/")[-1] or "endpoint")[:40]
        generate_client(rec, f"client_{safe}.py")


if __name__ == "__main__":
    main()

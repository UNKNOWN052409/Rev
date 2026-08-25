"""
Mobile App RE Plugin (mitmproxy addon)
=======================================
Koi bhi mobile app ki HTTP/HTTPS traffic intercept karo.
Auto-captures:
  - Saare API endpoints (host + path + method + status)  -> re_capture/endpoints.jsonl
  - Auth tokens / sensitive headers                      -> re_capture/tokens.txt
  - Full request/response bodies                         -> re_capture/session.jsonl

Usage:
    mitmdump -s mobile_re.py

Phone setup:
    1. WiFi proxy -> <laptop-ip>:8080
    2. Browser me http://mitm.it -> CA cert install karo
    3. Android 7+ pe user cert trust ke liye Frida/Magisk module chahiye hoga

Authorized testing only.
"""

import json
import os
import re
from datetime import datetime

from mitmproxy import http, ctx

OUT_DIR = "re_capture"
TOKENS_FILE = os.path.join(OUT_DIR, "tokens.txt")
ENDPOINTS_FILE = os.path.join(OUT_DIR, "endpoints.json")
HAR_LIKE_FILE = os.path.join(OUT_DIR, "session.jsonl")

# Noise kam karne ke liye - in hosts ko capture hi mat karo
IGNORE_HOSTS = [
    "googleapis.com", "gstatic.com", "crashlytics.com",
    "firebaseio.com", "doubleclick.net", "facebook.com",
    "app-measurement.com", "crashlytics", "sentry.io",
]

# Sensitive header patterns (token hunting ke liye)
TOKEN_HEADER_PATTERNS = [
    re.compile(r"^authorization$", re.I),
    re.compile(r"^x-api-key", re.I),
    re.compile(r"^x-auth", re.I),
    re.compile(r"^x-token", re.I),
    re.compile(r"^x-session", re.I),
    re.compile(r"^cookie$", re.I),
    re.compile(r"^set-cookie$", re.I),
]

# JSON body me chhupe tokens
TOKEN_BODY_PATTERNS = [
    re.compile(
        r'"(access_token|refresh_token|id_token|auth_token|api_key|session_id)"\s*:\s*"([^"]+)"',
        re.I,
    ),
]


def _is_ignored(host: str) -> bool:
    return any(p in host for p in IGNORE_HOSTS)


def _pretty_body(content: bytes, ctype: str) -> str:
    if not content:
        return ""
    if "json" in (ctype or ""):
        try:
            return json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except Exception:
            pass
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return repr(content[:500])


class MobileRE:
    def __init__(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.endpoints = {}
        self.flow_count = 0

    # ---------- request phase ----------
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if _is_ignored(host):
            flow.kill()

    # ---------- response phase ----------
    def response(self, flow: http.HTTPFlow) -> None:
        self.flow_count += 1
        req = flow.request
        res = flow.response
        host = req.pretty_host
        path = req.path
        method = req.method
        status = res.status_code
        ctype = res.headers.get("content-type", "")

        # ---- endpoint index maintain karo ----
        key = f"{method} {host}{path.split('?')[0]}"
        entry = self.endpoints.setdefault(key, {"count": 0, "statuses": set(), "params": set()})
        entry["count"] += 1
        entry["statuses"].add(status)
        if req.query:
            entry["params"].update(req.query.keys())

        record = {
            "ts": datetime.utcnow().isoformat(),
            "method": method,
            "url": req.pretty_url,
            "status": status,
            "req_headers": dict(req.headers),
            "req_body": _pretty_body(req.raw_content, req.headers.get("content-type", "")),
            "res_headers": dict(res.headers),
            "res_body": _pretty_body(res.raw_content, ctype),
        }

        # session log (full bodies, grep-able)
        with open(HAR_LIKE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # endpoint index rewrite
        serializable_endpoints = {
            k: {"count": v["count"],
                "statuses": sorted(v["statuses"]),
                "params": sorted(v["params"])}
            for k, v in self.endpoints.items()
        }
        with open(ENDPOINTS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable_endpoints, f, indent=2, ensure_ascii=False)

        # ---- token hunting ----
        found = []
        for h, v in req.headers.items():
            if any(p.match(h) for p in TOKEN_HEADER_PATTERNS):
                found.append((f"req.{h}", v))
        for pat in TOKEN_BODY_PATTERNS:
            for m in pat.finditer(record["req_body"]):
                found.append((f"req.body.{m.group(1)}", m.group(2)))
        for h, v in res.headers.items():
            if h.lower() == "authorization":
                found.append((f"res.{h}", v))
        for pat in TOKEN_BODY_PATTERNS:
            for m in pat.finditer(record["res_body"]):
                found.append((f"res.body.{m.group(1)}", m.group(2)))

        if found:
            ts = datetime.now().strftime("%H:%M:%S")
            with open(TOKENS_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] {method} {host}{path}\n")
                for name, val in found:
                    f.write(f"  {name} = {val}\n")
            ctx.log.info(f"[mobile_re] tokens captured from {method} {host}{path}")

    def done(self):
        ctx.log.info(f"[mobile_re] total flows captured : {self.flow_count}")
        ctx.log.info(f"[mobile_re] unique endpoints     : {len(self.endpoints)}")
        ctx.log.info(f"[mobile_re] output dir           : ./{OUT_DIR}/")


addons = [MobileRE()]

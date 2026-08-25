"""
Auto Pipeline — Capture Analysis -> Config -> Ready
====================================================
mobile_re.py ke capture se AUTOMATICALLY:
  1. Chat endpoint dhundta hai (scoring heuristic se)
  2. Auth headers extract karta hai
  3. Request body template banata hai
  4. Model IDs detect karta hai
  5. config.json likhta hai (app_to_api_server.py isko khud padhta hai)

Usage:
    # Capture hone ke BAAD:
    python auto_pipeline.py

    # LIVE watch mode — mitmdump chal raha hai, capture ka intezaar:
    python auto_pipeline.py --watch

Authorized personal use only.
"""

import argparse
import json
import os
import sys
import time

CAPTURE_DIR = "re_capture"
SESSION_FILE = os.path.join(CAPTURE_DIR, "session.jsonl")
CONFIG_OUT = "config.json"

# Chat endpoint detection ke liye body keys (score-based)
CHAT_BODY_HINTS = ["messages", "prompt", "input", "query", "contents", "text"]
CHAT_URL_HINTS = ["chat", "completion", "generate", "conversation",
                  "message", "inference", "ask"]
# Response side hints
RESP_HINTS = ["choices", "output", "content", "response", "answer"]

AUTH_HEADER_NAMES = [
    "authorization", "x-api-key", "x-auth-token", "x-token",
    "cookie", "token", "api-key", "x-session-token",
]


def load_flows():
    if not os.path.exists(SESSION_FILE):
        print(f"[!] {SESSION_FILE} not found. Pehle mitmdump -s mobile_re.py chalao")
        sys.exit(1)
    flows = []
    with open(SESSION_FILE) as f:
        for i, line in enumerate(f):
            try:
                flows.append(json.loads(line))
            except Exception:
                continue
    print(f"[+] {len(flows)} flows loaded")
    return flows


def score_chat_flow(flow):
    """Kitna likely ye flow 'chat completion' hai"""
    score = 0
    reasons = []

    if flow.get("method") == "POST":
        score += 2
        reasons.append("POST")

    # request body analysis
    body_raw = flow.get("req_body", "")
    body = None
    try:
        body = json.loads(body_raw)
    except Exception:
        pass

    if isinstance(body, dict):
        score += 2
        reasons.append("json-body")
        keys = set(k.lower() for k in body.keys())
        hits = [k for k in CHAT_BODY_HINTS if any(k in kb for kb in keys)]
        if hits:
            score += 3 * len(hits)
            reasons.append(f"body-hints:{hits}")

        # nested messages check (OpenAI style)
        if "messages" in keys and isinstance(body.get("messages"), list):
            score += 3
            reasons.append("messages-array")

    # URL hints
    url_l = flow.get("url", "").lower()
    url_hits = [h for h in CHAT_URL_HINTS if h in url_l]
    if url_hits:
        score += 2 * len(url_hits)
        reasons.append(f"url-hints:{url_hits}")

    # response hints
    res_body = flow.get("res_body", "")
    res_l = res_body.lower()[:2000]
    resp_hits = [h for h in RESP_HINTS if h in res_l]
    if resp_hits:
        score += min(2 * len(resp_hits), 6)
        reasons.append(f"resp-hints:{resp_hits[:3]}")
    if '"status"' in res_body and len(res_body) > 200:
        score += 1

    return score, reasons


def extract_auth_headers(headers):
    """Captured request se saare auth-relevant headers nikalo"""
    auth = {}
    for k, v in headers.items():
        if k.lower() in AUTH_HEADER_NAMES or k.lower().startswith("x-"):
            auth[k] = v
    # User-Agent bhi rakho (kai apps isko validate karti hain)
    keep_headers = ["User-Agent", "Content-Type", "Origin", "Referer",
                    "Accept", "X-FE-Version", "Bx-V", "Source"]
    for kh in keep_headers:
        for hk in headers:
            if hk.lower() == kh.lower():
                auth[hk] = headers[hk]
    return auth


def find_model_ids(body):
    models = []
    if isinstance(body, dict):
        m = body.get("model") or body.get("model_id") or body.get("modelId")
        if isinstance(m, str):
            models.append(m)
    return models


def analyze(flows):
    scored = []
    for fl in flows:
        s, r = score_chat_flow(fl)
        scored.append((s, r, fl))

    scored.sort(key=lambda x: x[0], reverse=True)

    print("\n[*] Top candidate chat endpoints:")
    for s, r, fl in scored[:5]:
        preview = fl["url"][:70]
        print(f"    {s:3d} pts | {fl['method']:4s} | {preview}")
        if s > 0:
            print(f"        ({', '.join(r[:5])})")

    best_s, best_r, best = scored[0]
    if best_s < 5:
        print("\n[!] Koi strong chat endpoint nahi mila (top score "
              f"{best_s} < 5). App me aur messages bhejo, phir dobara chalao.")
        return None

    print(f"\n[+] SELECTED: {best['url']}")

    headers = best.get("req_headers", {})
    auth_headers = extract_auth_headers(headers)
    print(f"[+] Auth headers extracted: {list(auth_headers.keys())}")
    for k in auth_headers:
        v = str(auth_headers[k])
        shown = v[:20] + "..." if len(v) > 23 else v
        print(f"      {k} = {shown}")

    # Body template banao — messages ko ${MESSAGES} se replace
    try:
        body = json.loads(best.get("req_body", "{}"))
    except Exception:
        body = {}
    template = dict(body)
    template_key_found = False
    for k in list(template.keys()):
        kl = k.lower()
        if any(h in kl for h in CHAT_BODY_HINTS) and isinstance(template[k], list):
            template[k] = "${MESSAGES}"
            template_key_found = True
            break
        if kl in ("prompt", "input", "query", "text") :
            template[k] = "${MESSAGES}"
            template_key_found = True
            break

    models_found = find_model_ids(body)
    print(f"[+] Model IDs detected: {models_found}")

    config = {
        "_meta": {
            "generated_by": "auto_pipeline.py",
            "confidence_score": best_s,
            "detection_reasons": best_r,
            "selected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "upstream_url": best["url"],
        "upstream_headers": auth_headers,
        "body_template": template,
        "template_messages_replaced": template_key_found,
        "model_map": {},
        "detected_models": models_found,
    }
    # pehle detected model ko default alias de do
    if models_found:
        config["model_map"]["qwen"] = models_found[0]

    with open(CONFIG_OUT, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"\n[+] config.json written!")
    print(f"[+] Ab chalao: python app_to_api_server.py")
    return config


def watch_mode(poll_s=3, timeout_s=600):
    print(f"[*] WATCH MODE — {SESSION_FILE} ka intezaar...")
    print(f"[*] Phone pe Qwen app me message bhej do jab ready ho.")
    start = time.time()
    last_size = os.path.getsize(SESSION_FILE) if os.path.exists(SESSION_FILE) else 0
    while time.time() - start < timeout_s:
        if os.path.exists(SESSION_FILE):
            size = os.path.getsize(SESSION_FILE)
            if size > last_size:
                print(f"[+] New traffic detected ({size - last_size} bytes)")
                flows = load_flows()
                cfg = analyze(flows)
                if cfg:
                    return True
                last_size = size
        time.sleep(poll_s)
    print("[!] Timeout — capture nahi mila")
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true",
                    help="live watch — capture ka intezaar karo")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    if args.watch:
        ok = watch_mode(timeout_s=args.timeout)
        sys.exit(0 if ok else 1)
    flows = load_flows()
    analyze(flows)

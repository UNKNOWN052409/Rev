"""
Flow Capture Tool — kisi bhi app ka AI endpoint MITM se capture karo
====================================================================
1. Browser khulta hai (persistent profile — login ek baar)
2. Tum app me jao, AI feature use karo (ek message bhej do)
3. Tool saare API requests capture karta hai (request+response)
4. AI endpoint choose karo -> connectors/<app>_flow.json ban jata hai
5. Universal server usko replay karta hai (MITM fetch se)

Usage:
    python3 capture_flow.py --app notion
    python3 capture_flow.py --app figma
    python3 capture_flow.py --app <kuch_bhi> --url https://app.example.com
"""

import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
"""
CONNECTORS_DIR = "connectors"
os.makedirs(CONNECTORS_DIR, exist_ok=True)

DEFAULT_URLS = {
    "qwen": "https://chat.qwen.ai",
    "notion": "https://www.notion.so",
    "figma": "https://www.figma.com/files",
}

# AI-looking request hints
AI_HINTS = ("ai", "chat", "completion", "generate", "assistant",
            "prompt", "inference", "ml", "llm", "stream")


def capture(app, url, pick_auto=False):
    captured = []

    with sync_playwright() as p:
        profile = os.path.join(CONNECTORS_DIR, f"profile_{app}")
        os.makedirs(profile, exist_ok=True)
        b = p.chromium.launch_persistent_context(
            profile, headless=False, user_agent=UA,
            viewport={"width": 1400, "height": 950},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        page = b.pages[0] if b.pages else b.new_page()
        page.add_init_script(STEALTH)

        def on_response(resp):
            try:
                req = resp.request
                u = resp.url
                if any(x in u for x in ("google", "sentry", "analytics",
                                        "doubleclick", "facebook")):
                    return
                post = req.post_data or ""
                low = u.lower()
                is_ai = (any(h in low for h in AI_HINTS)
                         and req.method in ("POST",)) or "stream" in (
                    resp.headers.get("content-type", "") +
                    resp.headers.get("x-accel-buffering", ""))
                if not is_ai and not post:
                    return
                # response body (SSE bhi)
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                captured.append({
                    "url": u,
                    "method": req.method,
                    "status": resp.status,
                    "req_headers": {k: v for k, v in req.headers.items()
                                    if k.lower() not in (
                                        "content-length", "host",
                                        "accept-encoding", "connection")},
                    "req_body": post[:20000],
                    "res_content_type": resp.headers.get("content-type", ""),
                    "res_body": body[:20000],
                    "ts": time.time(),
                })
            except Exception:
                pass

        page.on("response", on_response)

        print("=" * 56)
        print(f" {app.upper()} FLOW CAPTURE")
        print(f" Browser khul gaya: {url}")
        print("  1. Login karo (agar chahiye)")
        print("  2. AI feature dhundo (Notion AI / Figma AI etc.)")
        print("  3. EK AI message bhejo, reply ka wait karo")
        print("  4. Wapas yahan aake Enter maaro")
        print("=" * 56)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        input("\n[Enter jab AI message bhej ke reply mil jaye] > ")
        b.close()

    # ---- AI requests filter ----
    ai_reqs = [c for c in captured
               if c["req_body"] or "stream" in c["res_content_type"]
               or any(h in c["url"].lower() for h in AI_HINTS)]
    # dedupe by url
    seen, unique = set(), []
    for c in ai_reqs:
        key = (c["method"], c["url"].split("?")[0])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    print(f"\n[*] {len(unique)} unique AI-candidate requests:\n")
    for i, c in enumerate(unique):
        print(f"  [{i:2d}] {c['method']:4s} {c['status']} "
              f"{c['url'][:85]}")
        if c["req_body"]:
            print(f"       body: {c['req_body'][:100]}")
        print(f"       resp: [{c['res_content_type'][:30]}] "
              f"{c['res_body'][:80]}")

    if not unique:
        print("[!] Kuch capture nahi hua — AI feature use karke dekho")
        return False

    if pick_auto and len(unique) == 1:
        idx = 0
    else:
        idx = input("\nKaunsa AI endpoint hai? index likho: > ").strip()
        try:
            idx = int(idx)
        except ValueError:
            print("galat index")
            return False

    chosen = unique[idx]
    # body template — common prompt fields ko __PROMPT__ se replace
    template = {}
    try:
        template = json.loads(chosen["req_body"])
        _mark_prompt(template)
    except Exception:
        template = {"__RAW_BODY__": chosen["req_body"]}

    flow = {
        "_meta": {"app": app, "captured_at": time.strftime(
            "%Y-%m-%d %H:%M:%S"), "url": chosen["url"]},
        "url": chosen["url"],
        "method": chosen["method"],
        "headers": chosen["req_headers"],
        "body_template": template,
        "response_sample": chosen["res_body"][:2000],
        "response_content_type": chosen["res_content_type"],
    }
    out = os.path.join(CONNECTORS_DIR, f"{app}_flow.json")
    with open(out, "w") as f:
        json.dump(flow, f, indent=2)
    print(f"\n[+] FLOW SAVED: {out}")
    print(f"[+] Ab chalao: python3 universal_bridge.py --serve")
    print(f"    model=\"{app}\" se access hoga!")
    return True


def _mark_prompt(obj, depth=0):
    """Recursively prompt-jaisi string values ko __PROMPT__ se mark karo"""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, str) and any(
                    w in kl for w in ("prompt", "content", "text",
                                      "query", "input", "message")):
                if v and len(v) > 2 and "http" not in v[:10]:
                    obj[k] = "__PROMPT__"
            elif isinstance(v, (dict, list)):
                _mark_prompt(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _mark_prompt(item, depth + 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True,
                    help="notion / figma / kuch bhi naam")
    ap.add_argument("--url", default="",
                    help="custom start URL (optional)")
    ap.add_argument("--auto", action="store_true",
                    help="single candidate auto-pick")
    args = ap.parse_args()
    url = args.url or DEFAULT_URLS.get(args.app, "https://www.google.com")
    sys.exit(0 if capture(args.app, url, args.auto) else 1)

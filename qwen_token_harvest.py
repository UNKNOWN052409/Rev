"""
Qwen Token Harvester — REAL browser se LIVE token
==================================================
Headless/visible Chromium me chat.qwen.ai kholta hai:
  - Guest access allowed -> token seedha mil jata hai (zero login)
  - Login required       -> visible window me khud login karo, phir capture

Token kahan milta hai:
  - localStorage['token']          (Open WebUI standard — Qwen isi pe bana hai)
  - ya network calls ka Bearer     (fallback)

Usage:
    ./venv/bin/python qwen_token_harvest.py            # try guest (headless)
    ./venv/bin/python qwen_token_harvest.py --manual   # visible browser, khud login
"""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

CONFIG_FILE = "config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def harvest(manual=False):
    token = None
    bearer_seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not manual,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA,
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()

        # fallback: network se Bearer pakdo
        def on_request(req):
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer ") and len(auth) > 20:
                t = auth.split(" ", 1)[1]
                if "." in t and len(t) > 40:   # JWT-shaped only
                    bearer_seen.append(t)

        page.on("request", on_request)

        print("[*] chat.qwen.ai load ho raha hai...")
        page.goto("https://chat.qwen.ai", wait_until="domcontentloaded",
                  timeout=60000)
        page.wait_for_timeout(6000)   # WAF challenge + app boot

        if manual:
            print("=" * 50)
            print(" BROWSER WINDOW ME LOGIN KARO")
            print(" (Google OAuth bhi chalega)")
            print(" Login ke baad yahan automatic capture hoga...")
            print("=" * 50)
            deadline = __import__("time").time() + 600
            while __import__("time").time() < deadline:
                tok = _read_token(page)
                if tok:
                    break
                time.sleep(3)
            token = tok

        if not token:
            token = _read_token(page)

        if not token and bearer_seen:
            token = bearer_seen[-1]

        if token:
            print(f"[+] TOKEN MIL GAYA ({len(token)} chars): {token[:25]}...")
            # ek chhota verification call — /api/models with token
            ok = _verify(ctx.request, token)
            if ok:
                print("[+] Token VERIFIED (/api/models 200 with auth)")
            _save_config(token)
        else:
            print("[!] Token nahi mila.")
            print("    Guest access band hai -> '--manual' flag se chalao:")
            print("    ./venv/bin/python qwen_token_harvest.py --manual")

        browser.close()
        return token


def _read_token(page):
    """localStorage + cookies se Open WebUI style token nikalo"""
    candidates = []
    try:
        for key in ("token", "auth_token", "access_token", "jwt"):
            v = page.evaluate(f"localStorage.getItem('{key}')")
            if v and len(v) > 30 and "." in v:
                candidates.append(v)
    except Exception:
        pass
    return candidates[0] if candidates else None


def _verify(api_ctx, token):
    try:
        r = api_ctx.get("https://chat.qwen.ai/api/v1/auths/",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=15000)
        return r.status == 200
    except Exception:
        return False


def _save_config(token):
    cfg = json.load(open(CONFIG_FILE)) if __import__("os").path.exists(CONFIG_FILE) else {}
    hdrs = cfg.get("upstream_headers", {})
    hdrs["Authorization"] = f"Bearer {token}"
    cfg["upstream_headers"] = hdrs
    cfg.setdefault("upstream_url", "https://chat.qwen.ai/api/v1/chat/completions")
    cfg.setdefault("body_template", {
        "model": "${MODEL}", "messages": "${MESSAGES}", "stream": True,
        "chat_type": "t2t", "timestamp": "${TIMESTAMP}"})
    cfg.setdefault("model_map", {"qwen": "qwen3.8-max",
                                 "qwen-max": "qwen3.8-max",
                                 "qwen-plus": "qwen3.7-plus"})
    json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)
    print(f"[+] config.json updated with REAL token")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true",
                    help="visible browser — khud login karo")
    args = ap.parse_args()
    t = harvest(manual=args.manual)
    sys.exit(0 if t else 1)

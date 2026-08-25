"""
Qwen One-Shot Pipeline — creds/manual login -> captured API -> ready
====================================================================
Phone/emulator ki zaroorat nahi. Do modes:

  AUTO    (--email/--password ya QWEN_EMAIL/QWEN_PASSWORD env):
          headless browser khud login karta hai, token capture,
          config.json auto-generate, optional server start.

  MANUAL  (--manual):
          visible browser khulega — tum apne acc se ek baar login
          kar do (Google bot-checks bhi pass), script background me
          traffic capture karti rahegi. Token milte hi config banegi.

Flow:
  mitmdump (mobile_re.py) spawn -> playwright proxy ke through ->
  login -> re_capture/tokens.txt me token -> auto_pipeline.analyze()
  -> config.json -> (--serve diya toh adapter start)

Usage:
  # Auto:
  python qwen_pipeline.py --email you@gmail.com --pass 'secret' --serve

  # Manual (recommended agar Google OAuth use karna hai):
  python qwen_pipeline.py --manual --serve

Authorized personal use only.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from auto_pipeline import analyze, SESSION_FILE

PROXY_PORT = 8082
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
TOKENS_FILE = os.path.join("re_capture", "tokens.txt")
os.makedirs(SHOT_DIR, exist_ok=True)


# ================================================================
# mitmdump lifecycle
# ================================================================

def start_proxy():
    """mobile_re.py addon ke saath mitmdump spawn karo"""
    cmd = ["mitmdump", "-s", "mobile_re.py",
           "-p", str(PROXY_PORT),
           "--set", "block_global=false"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(2)
    if proc.poll() is not None:
        print("[!] mitmdump start nahi hua — 'pip install mitmproxy'")
        sys.exit(1)
    print(f"[+] mitmdump running on :{PROXY_PORT} (pid {proc.pid})")
    return proc


def wait_for_token(timeout_s=300, quiet_after=None):
    """
    tokens.txt me auth material ka intezaar.
    Returns: True agar kuch capture hua.
    quiet_after: itne sec tak naya capture na aaye toh maan lo login
    ho gaya (flows session.jsonl me honge).
    """
    print(f"[*] Token capture ka intezaar ({timeout_s}s max)...")
    start = time.time()
    last_size = os.path.getsize(TOKENS_FILE) if os.path.exists(TOKENS_FILE) else 0
    last_change = time.time()

    while time.time() - start < timeout_s:
        size = os.path.getsize(TOKENS_FILE) if os.path.exists(TOKENS_FILE) else 0
        if size > last_size:
            last_size = size
            last_change = time.time()
            print(f"[+] Auth material captured! ({size} bytes)")
            return True
        if quiet_after and (time.time() - last_change) > quiet_after \
                and os.path.exists(SESSION_FILE):
            print("[*] Naya auth nahi aaya lekin traffic capture hua hai")
            return True
        time.sleep(2)
    return False


# ================================================================
# Browser login
# ================================================================

def _launch_browser(p, headless):
    return p.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )


def _new_ctx(browser):
    return browser.new_context(
        proxy={"server": f"http://127.0.0.1:{PROXY_PORT}"},
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 900},
    )


def _shot(page, name):
    try:
        page.screenshot(path=f"{SHOT_DIR}/{name}.png")
    except Exception:
        pass


def auto_login(page, email, password):
    """Headless auto-login. Returns True agar logged-in state mile."""
    page.goto("https://chat.qwen.ai", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    _shot(page, "p01_landing")

    login_btn = page.locator("button:has-text('Log in'), a:has-text('Log in')").first
    if not (login_btn.count() > 0 and login_btn.is_visible()):
        print("[+] Already logged-in lagta hai")
        return True

    login_btn.click()
    page.wait_for_timeout(4000)
    _shot(page, "p02_login_page")

    if "accounts.google.com" in page.url:
        print("[*] Google OAuth flow detect hua — auto-fill try kar rahe hain")
        email_inp = page.locator("input[type='email']").first
        email_inp.wait_for(state="visible", timeout=15000)
        email_inp.fill(email)
        _shot(page, "p03_email_filled")
        page.locator("#identifierNext, button:has-text('Next')").first.click()

        pwd_inp = page.locator("input[type='password']").first
        try:
            pwd_inp.wait_for(state="visible", timeout=20000)
        except Exception:
            print("[!] Password field nahi mila — Google ne block kiya hoga.")
            print("    Screenshot dekho: p03_email_filled.png")
            print("    TIP: --manual mode use karo, wahan ye check pass hota hai")
            return False
        page.wait_for_timeout(1000)
        pwd_inp.fill(password)
        _shot(page, "p04_pwd_filled")
        page.locator("#passwordNext, button:has-text('Next')").first.click()
    else:
        # Native Qwen form
        filled = False
        for sel in ["input[type='email']", "input[name*='mail' i]"]:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(email)
                filled = True
                break
        if not filled:
            print("[!] Email field nahi mila — screenshot: p02_login_page.png")
            return False
        _shot(page, "p03_email_filled")
        for sel in ["button:has-text('Continue')", "button[type='submit']"]:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                break
        page.wait_for_timeout(3000)
        pwd_inp = page.locator("input[type='password']").first
        if pwd_inp.count() == 0:
            print("[!] Password field nahi mila — screenshot: p03_email_filled.png")
            return False
        pwd_inp.fill(password)
        _shot(page, "p04_pwd_filled")
        page.keyboard.press("Enter")

    page.wait_for_timeout(8000)
    _shot(page, "p05_after_submit")

    if "accounts.google.com" in page.url:
        content = ""
        try:
            content = page.content().lower()
        except Exception:
            pass
        if "denied" in content or "couldn't sign you in" in content:
            print("[!] Google ne block kiya ('couldn't sign you in')")
            print("    --manual mode use karo")
            return False

    print(f"[*] Post-login URL: {page.url}")
    return True


def manual_login(p):
    """Visible browser — user khud login kare, hum capture karte rahein"""
    browser = _launch_browser(p, headless=False)
    ctx = _new_ctx(browser)
    page = ctx.new_page()
    page.goto("https://chat.qwen.ai", wait_until="domcontentloaded")

    print()
    print("=" * 56)
    print(" MANUAL LOGIN MODE")
    print("   Browser window khul gaya hai.")
    print("   Apne acc se login kar do (Google OAuth chalega).")
    print("   Login ke baad yahan sab AUTOMATIC capture hoga.")
    print("=" * 56)
    print()

    # jab tak user chat page pe logged-in na ho jaye, ruko
    deadline = time.time() + 600  # 10 min
    while time.time() < deadline:
        try:
            btn = page.locator(
                "button:has-text('Log in'), a:has-text('Log in')").first
            still_out = btn.count() > 0 and btn.is_visible()
        except Exception:
            still_out = True
        if not still_out and "chat.qwen.ai" in page.url:
            print("[+] Logged-in detected!")
            # ek dummy message bhej do taki chat-completion request capture ho
            try:
                _shot(page, "m01_logged_in")
                box = None
                for sel in ["textarea", "[contenteditable='true']",
                            "div[role='textbox']"]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        box = loc
                        break
                if box:
                    box.click()
                    box.type("hi", delay=30)
                    page.keyboard.press("Enter")
                    print("[+] Test message bheja — completion request capture hogi")
            except Exception as e:
                print(f"[!] Message send skip ({e}) — flows phir bhi milenge")
            return True
        time.sleep(3)
    print("[!] 10 min me login nahi hua")
    return False


# ================================================================
# Main
# ================================================================

def main():
    ap = argparse.ArgumentParser(description="Qwen one-shot -> API pipeline")
    ap.add_argument("--email", default=os.environ.get("QWEN_EMAIL", ""))
    ap.add_argument("--pass", dest="password",
                    default=os.environ.get("QWEN_PASSWORD", ""))
    ap.add_argument("--manual", action="store_true",
                    help="visible browser me khud login karo "
                         "(recommended for Google OAuth)")
    ap.add_argument("--headed", action="store_true",
                    help="auto mode me bhi browser dikhe")
    ap.add_argument("--serve", action="store_true",
                    help="config.json banne ke baad adapter server start karo")
    ap.add_argument("--timeout", type=int, default=300,
                    help="token capture wait (sec)")
    args = ap.parse_args()

    if not args.manual and not (args.email and args.password):
        ap.error("creds do (--email/--pass) YA --manual use karo")

    proxy = start_proxy()
    ok = False
    try:
        with sync_playwright() as p:
            if args.manual:
                ok = manual_login(p)
            else:
                browser = _launch_browser(p, headless=not args.headed)
                ctx = _new_ctx(browser)
                page = ctx.new_page()
                page.set_default_timeout(30000)
                ok = auto_login(page, args.email, args.password)
                if ok:
                    # logged-in chat kholo aur ek test message maro taki
                    # completion endpoint capture ho jaye
                    try:
                        page.goto("https://chat.qwen.ai",
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(5000)
                        _shot(page, "p06_chat_check")
                        for sel in ["textarea", "[contenteditable='true']",
                                    "div[role='textbox']"]:
                            loc = page.locator(sel).first
                            if loc.count() > 0 and loc.is_visible():
                                loc.click()
                                loc.type("hi", delay=30)
                                page.keyboard.press("Enter")
                                print(f"[+] Test message sent via {sel}")
                                break
                    except Exception as e:
                        print(f"[!] Test message skip: {e}")
                browser.close()

        if not ok:
            print("\n[!] Login fail — shots/ dir ke screenshots dekho")
            sys.exit(2)

        if not wait_for_token(timeout_s=args.timeout, quiet_after=45):
            print("[!] Token capture nahi hua — kya traffic proxy se guzra?")
            sys.exit(3)

        # config.json generate (auto_pipeline ka analyzer reuse)
        flows = []
        with open(SESSION_FILE) as f:
            for line in f:
                try:
                    flows.append(json.loads(line))
                except Exception:
                    continue
        cfg = analyze(flows) if flows else None
        if not cfg:
            print("[!] Chat endpoint detect nahi hua — aur messages bhejo, "
                  "phir 'python auto_pipeline.py' chalao")
            sys.exit(4)

        print("\n[+] DONE! Standard OpenAI client se use karo:")
        print("    base_url = http://localhost:8001/v1")

        if args.serve:
            print("[*] Adapter server starting...\n")
            rc = subprocess.call([sys.executable, "app_to_api_server.py"])
            sys.exit(rc)
    finally:
        proxy.send_signal(signal.SIGTERM)


if __name__ == "__main__":
    import json  # late import — analyze ke flows parse ke liye
    main()

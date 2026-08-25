"""
Auto Login — creds se persistent login + token harvest + real test
==================================================================
1. Persistent profile (browser_profile/) me headless login
2. localStorage se Bearer token -> config.json
3. Browser me REAL test message -> reply verify
4. HTTP adapter ke liye sab ready

Usage:
    ./venv/bin/python login_auto.py --email <email> --pass <password>
"""

import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "browser_profile")
BASE_URL = "https://chat.qwen.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

SHOT = "shots"
os.makedirs(SHOT, exist_ok=True)


def shot(page, name):
    try:
        page.screenshot(path=f"{SHOT}/{name}.png")
    except Exception:
        pass


def dismiss_modals(page):
    """Welcome modal / promo popups band karo"""
    for _ in range(3):
        closed = False
        try:
            # ant-design close buttons (X)
            for sel in ["button[aria-label='Close']", "[aria-label='close']",
                        "button.close", "[class*='close-button']"]:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click()
                    closed = True
                    page.wait_for_timeout(800)
                    break
        except Exception:
            pass
        if not closed:
            break


def do_login(email, password):
    result = {"logged_in": False, "token": None, "test_reply": None}
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=True, user_agent=UA,
            viewport={"width": 1366, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.add_init_script(STEALTH)
        page.set_default_timeout(30000)

        print("[*] chat.qwen.ai load...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        shot(page, "l01_loaded")

        # already logged in?
        login_btn = page.locator("button:has-text('Log in'), "
                                 "a:has-text('Log in')").first
        if not (login_btn.count() > 0 and login_btn.is_visible()):
            print("[+] Pehle se logged in!")
            result["logged_in"] = True
        else:
            print("[*] Login page khol raha hoon...")
            login_btn.click()
            page.wait_for_timeout(5000)
            shot(page, "l02_login_page")

            # Google OAuth redirect?
            if "accounts.google.com" in page.url:
                print("[*] Google OAuth flow — email/password se try")
                page.locator("input[type='email']").first.fill(email)
                shot(page, "l03_g_email")
                page.locator("#identifierNext, "
                             "button:has-text('Next')").first.click()
                page.wait_for_timeout(3000)
                pwd = page.locator("input[type='password']").first
                pwd.wait_for(state="visible", timeout=20000)
                pwd.fill(password)
                shot(page, "l04_g_pwd")
                page.locator("#passwordNext, "
                             "button:has-text('Next')").first.click()
            else:
                # native Qwen form — email field dhundo
                print("[*] Native login form")
                email_inp = None
                for sel in ["input[type='email']", "input[name*='mail' i]",
                            "input[placeholder*='mail' i]"]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        email_inp = loc
                        break
                if email_inp is None:
                    # email/password toggle button ho sakta hai
                    for t in ("Email", "email", "Continue with email",
                              "Password"):
                        btn = page.locator(f"button:has-text('{t}'), "
                                           f"div:has-text('{t}')").first
                        if btn.count() > 0 and btn.is_visible():
                            btn.click()
                            page.wait_for_timeout(2500)
                            shot(page, "l02b_after_toggle")
                            break
                    for sel in ["input[type='email']", "input[name*='mail' i]",
                                "input[type='text']"]:
                        loc = page.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            email_inp = loc
                            break
                if email_inp is None:
                    print("[!] Email field nahi mila — l02_login_page.png dekho")
                    shot(page, "l_fail_no_email")
                    browser.close()
                    return result
                email_inp.fill(email)
                shot(page, "l05_email_filled")

                # password field — ISI page pe hai (dono fields ek saath)
                pwd = page.locator("input[type='password']").first
                try:
                    pwd.wait_for(state="visible", timeout=10000)
                except Exception:
                    pass
                if pwd.count() > 0 and pwd.is_visible():
                    pwd.fill(password)
                    shot(page, "l07_pwd_filled")
                    page.wait_for_timeout(500)

                # submit — ab dono fields bhari hain
                clicked = False
                for sel in ["button[type='submit']:not([disabled])",
                            "button:has-text('Sign in')",
                            "button:has-text('Log in')",
                            "button[type='submit']"]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        try:
                            loc.click(timeout=5000)
                            clicked = True
                            break
                        except Exception:
                            continue
                if not clicked:
                    page.keyboard.press("Enter")
                print("[*] Submit ho gaya, login ka intezaar...")
                page.wait_for_timeout(8000)
                shot(page, "l08_after_submit")

            # login detect (60s tak)
            ok = False
            for _ in range(20):
                page.wait_for_timeout(3000)
                lb = page.locator("button:has-text('Log in'), "
                                  "a:has-text('Log in')").first
                if not (lb.count() > 0 and lb.is_visible()):
                    ok = True
                    break
                shot(page, "l09_waiting")
            result["logged_in"] = ok
            if not ok:
                print("[!] Login fail — shots/l08_after_submit.png dekho")
                browser.close()
                return result

        print("[+] LOGIN OK!")
        shot(page, "l10_logged_in")

        # ---- token harvest ----
        page.wait_for_timeout(3000)
        token = None
        for key in ("token", "auth_token", "access_token", "jwt"):
            try:
                v = page.evaluate(f"localStorage.getItem('{key}')")
                if v and len(v) > 30 and "." in v:
                    token = v
                    break
            except Exception:
                pass
        result["token"] = token
        if token:
            print(f"[+] Token: {token[:30]}...")
        else:
            # koi API call trigger karke Bearer pakdo
            page.goto(f"{BASE_URL}/api/models", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(1500)
            for key in ("token", "auth_token"):
                v = page.evaluate(f"localStorage.getItem('{key}')")
                if v and len(v) > 30:
                    token = v
                    break
            result["token"] = token

        # ---- REAL completion test (browser me) ----
        print("[*] REAL test message bhej raha hoon...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        dismiss_modals(page)
        try:
            ta = page.locator("textarea").first
            ta.click()
            ta.type("Reply with exactly one word: BRIDGE-OK", delay=15)
            page.wait_for_timeout(400)
            send = page.locator("button.send-button[aria-label='Send']").first
            if send.count() > 0 and send.is_visible():
                send.click()
            else:
                page.keyboard.press("Enter")

            reply = ""
            stable = 0
            last = ""
            for _ in range(60):
                page.wait_for_timeout(1000)
                state = page.evaluate("""() => {
                    const stop = document.querySelector(
                        "button[aria-label='Stop'], [class*='stop']");
                    const blocks = Array.from(document.querySelectorAll(
                        '[class*="markdown"], [class*="assistant"],' +
                        ' [class*="answer"], [class*="message-content"]'))
                        .map(e => e.innerText).filter(t => t && t.trim());
                    return {stopping: !!stop, last: blocks.length ?
                            blocks[blocks.length-1] : ""};
                }""")
                cur = state["last"]
                if not state["stopping"] and cur and cur == last:
                    stable += 1
                    if stable >= 3:
                        reply = cur
                        break
                else:
                    stable = 0
                last = cur
            result["test_reply"] = reply[:200] if reply else None
            shot(page, "l11_test_reply")
            if reply:
                print(f"[+] REPLY MILA: {reply[:120]}")
            else:
                print("[!] Reply capture nahi hua (DOM selectors tweak chahiye "
                      "honge) — par login/token bhi kaafi hai")
        except Exception as e:
            print(f"[!] Test message fail: {str(e)[:150]}")
            shot(page, "l_fail_test")

        browser.close()

    # ---- config.json update ----
    if result["token"]:
        cfg = json.load(open("config.json")) if os.path.exists("config.json") else {}
        hdrs = cfg.get("upstream_headers", {})
        hdrs["Authorization"] = f"Bearer {result['token']}"
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("User-Agent", UA)
        hdrs.setdefault("Origin", "https://chat.qwen.ai")
        hdrs.setdefault("Referer", "https://chat.qwen.ai/")
        cfg["upstream_headers"] = hdrs
        cfg.setdefault("upstream_url",
                       "https://chat.qwen.ai/api/v1/chat/completions")
        cfg.setdefault("body_template", {
            "model": "${MODEL}", "messages": "${MESSAGES}", "stream": True,
            "chat_type": "t2t", "timestamp": "${TIMESTAMP}"})
        cfg.setdefault("model_map", {"qwen": "qwen3.8-max",
                                     "qwen-max": "qwen3.8-max",
                                     "qwen-plus": "qwen3.7-plus"})
        json.dump(cfg, open("config.json", "w"), indent=2)
        print("[+] config.json me REAL token save ho gaya")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--pass", dest="password", required=True)
    args = ap.parse_args()
    r = do_login(args.email, args.password)
    print("\n" + "=" * 40)
    print(f" logged_in : {r['logged_in']}")
    print(f" token     : {'YES' if r['token'] else 'NO'}")
    print(f" test_reply: {(r['test_reply'] or 'none')[:80]}")
    print("=" * 40)
    sys.exit(0 if r["logged_in"] else 1)

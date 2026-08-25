"""
Qwen auto-login + chat via mitmproxy capture
Headless browser -> proxy 8082 -> sab traffic re_capture me
"""

import json
import os
import sys
import time
from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:8082"
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
os.makedirs(SHOT_DIR, exist_ok=True)

EMAIL = os.environ.get("QWEN_EMAIL", "")
PASSWORD = os.environ.get("QWEN_PASSWORD", "")


def shot(page, name):
    page.screenshot(path=f"{SHOT_DIR}/{name}.png")
    print(f"  [shot] {name}.png")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            proxy={"server": PROXY},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        print("[1] Opening chat.qwen.ai ...")
        page.goto("https://chat.qwen.ai", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        shot(page, "01_landing")
        print(f"    title: {page.title()}")

        # Logged-in check: 'Log in' button visible = logged OUT
        login_btn = page.locator("button:has-text('Log in'), a:has-text('Log in')").first
        if login_btn.count() > 0 and login_btn.is_visible():
            print("[2] Logged out — clicking Log in...")
            login_btn.click()
            page.wait_for_timeout(4000)
            shot(page, "02_login_page")
            print(f"    now at: {page.url}")

            print("[3] Filling credentials (Google OAuth flow)...")
            on_google = "accounts.google.com" in page.url
            print(f"    on_google={on_google}, url={page.url}")

            if on_google:
                # --- Google sign-in: email page -> Next -> password page -> Next ---
                email_inp = page.locator("input[type='email']").first
                email_inp.wait_for(state="visible", timeout=15000)
                email_inp.fill(EMAIL)
                shot(page, "03_email_filled")

                page.locator("#identifierNext, button:has-text('Next')").first.click()
                print("    email bheja, Next dabaya")

                # password page alag step pe load hota hai
                pwd_inp = page.locator("input[type='password']").first
                pwd_inp.wait_for(state="visible", timeout=20000)
                page.wait_for_timeout(1000)
                pwd_inp.fill(PASSWORD)
                shot(page, "05_pwd_filled")

                page.locator("#passwordNext, button:has-text('Next')").first.click()
                print("    password bheja, Next dabaya")
            else:
                # --- native Qwen form fallback ---
                for sel in ["input[type='email']", "input[name*='mail' i]"]:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.fill(EMAIL)
                        break
                shot(page, "03_email_filled")
                for sel in ["button:has-text('Continue')", "button[type='submit']"]:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.click()
                        break
                page.wait_for_timeout(3000)
                pwd_inp = page.locator("input[type='password']").first
                if pwd_inp.count() > 0:
                    pwd_inp.fill(PASSWORD)
                    shot(page, "05_pwd_filled")
                    page.keyboard.press("Enter")

            page.wait_for_timeout(8000)
            shot(page, "06_after_submit")
            print(f"    now at: {page.url}")

            # Google 'unsafe browser' warning check
            if "accounts.google.com" in page.url and "denied" in page.content().lower() + "":
                print("    [!] Google ne block kiya lagta hai — screenshot dekho")

        print("[4] Checking chat interface...")
        page.goto("https://chat.qwen.ai", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        shot(page, "07_chat_check")

        # Chat input dhundo aur message bhejo
        msg_sent = False
        for sel in ["textarea", "#chat-input", "[contenteditable='true']",
                    "textarea[placeholder*='essage' i]", "div[role='textbox']"]:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                loc.type("Hello! Reply with just: CAPTURE_TEST_OK", delay=30)
                page.wait_for_timeout(500)
                page.keyboard.press("Enter")
                msg_sent = True
                print(f"[5] Message sent via {sel}!")
                break

        if not msg_sent:
            print("[!] Chat input nahi mila — screenshot dekho")

        # response ka intezaar + capture time
        print("[6] Waiting for response (capture ke liye)...")
        page.wait_for_timeout(15000)
        shot(page, "08_response")

        print("\n[+] Done! ab re_capture/session.jsonl check karo")
        browser.close()


if __name__ == "__main__":
    main()

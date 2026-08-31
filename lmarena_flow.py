"""LMArena full flow — interaction-triggered anonymous session (v2)"""
from playwright.sync_api import sync_playwright
import time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

chat_reqs = []
signup_done = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.add_init_script(STEALTH)

    def on_resp(resp):
        u = resp.url
        if "sign-up" in u:
            signup_done.append(resp.status)
            print(f"[SIGNUP] {resp.status}", flush=True)
        if "arena.ai" in u and "nextjs-api" in u and "sign-up" not in u \
                and "release-banner" not in u:
            req = resp.request
            body = b""
            try:
                body = req.post_data_buffer or b""
            except Exception:
                pass
            bs = body.decode("utf-8", errors="replace")[:1200]
            chat_reqs.append((req.method, u, bs))
            print(f"\n### {req.method} {u[:115]}", flush=True)
            for k, v in req.headers.items():
                if k.lower() in ("content-type", "authorization",
                                 "next-action"):
                    print(f"    {k}: {str(v)[:120]}", flush=True)
            print(f"    BODY: {bs[:700]}", flush=True)

    page.on("response", on_resp)
    page.goto("https://lmarena.ai", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_timeout(8000)

    try:
        loc = page.locator("button:has-text('Agree')").first
        if loc.count() > 0 and loc.is_visible():
            loc.click(force=True)
            print("[+] agreed", flush=True)
            page.wait_for_timeout(1500)
    except Exception:
        pass

    # REAL interactions — app ka user-interaction flag trigger
    page.mouse.move(300, 300)
    page.mouse.move(600, 400, steps=8)
    page.mouse.wheel(0, 300)
    print("[*] interacting...", flush=True)

    # app ke sign-up ka wait (25s)
    for i in range(12):
        if signup_done:
            break
        page.mouse.move(300 + i * 3, 350)
        time.sleep(2)
    print(f"[*] signups: {signup_done}", flush=True)
    page.wait_for_timeout(3000)

    # send message
    box = page.locator(
        "textarea:not([name='g-recaptcha-response'])").first
    box.click()
    box.type("Say ARENA-OK", delay=20)
    page.wait_for_timeout(500)
    page.evaluate("""() => {
        const tas = Array.from(document.querySelectorAll('textarea'))
            .filter(t => t.name !== 'g-recaptcha-response');
        const form = tas[0].closest('form');
        const btns = form.querySelectorAll('button');
        btns[btns.length - 1].click();
    }""")
    print("[*] send clicked — modal ka wait + agree...", flush=True)
    # send pe terms modal open hota hai — uska wait + agree
    try:
        loc = page.locator("button:has-text('Agree')").first
        loc.wait_for(state="visible", timeout=20000)
        loc.click(force=True)
        print("[+] modal agreed — ab send proceed karega!", flush=True)
    except Exception as e:
        print(f"[*] modal nahi aya: {str(e)[:60]}", flush=True)
    page.wait_for_timeout(45000)

    st = page.evaluate("""() => {
        const tas = Array.from(document.querySelectorAll('textarea'))
            .filter(t => t.name !== 'g-recaptcha-response');
        return {ta: tas[0] ? tas[0].value : null,
                text: document.body.innerText.slice(0, 250)};
    }""")
    print(f"\nta after: {st['ta']!r}", flush=True)
    print(f"page: {st['text'][:200]}", flush=True)
    page.screenshot(path="lmarena_chat15.png")
    b.close()
print("DONE")

"""
Qwen Browser Bridge — REAL logged-in browser -> OpenAI API
===========================================================
Architecture:
  - Chromium PERSISTENT profile (browser_profile/) — login EK baar,
    session weeks tak chalega (cookies persist)
  - POST /v1/chat/completions -> bridge browser me message type karke
    bhejta hai, reply DOM + network SSE dono se capture hota hai
  - WAF/slider ka jhanjhat nahi (real browser, real user session)

Usage:
    # Step 1 — login (visible window, EK baar):
    ./venv/bin/python qwen_browser_bridge.py --login

    # Step 2 — server chalu:
    ./venv/bin/python qwen_browser_bridge.py --serve
    # ya headless server:
    ./venv/bin/python qwen_browser_bridge.py --serve --headless
"""

import argparse
import json
import os
import sys
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

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
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
    p.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : origQuery(p);
"""


# ================================================================
# Browser session manager
# ================================================================

class BrowserSession:
    def __init__(self, headless=True):
        self.headless = headless
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.lock = threading.Lock()
        self._stream_cb = None
        self._sse_pieces = []
        # captured flow se device token (browser-bound, profile ke saath valid)
        try:
            flow = json.load(open("captured_v2_flow.json"))
            self._umid = flow["requests"][0]["headers"].get("bx-umidtoken", "")
        except Exception:
            self._umid = ""

    def start(self):
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=self.headless,
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        self.page = self.ctx.new_page() if not self.ctx.pages else self.ctx.pages[0]
        self.page.add_init_script(STEALTH)
        self._install_network_hook(self.page)

    def _install_network_hook(self, page):
        def on_response(resp):
            try:
                if "chat/completions" in resp.url and "punish" not in resp.url:
                    body = resp.text()   # stream complete hone tak wait
                    self.sse_capture[resp.url] = body
            except Exception:
                pass
        self._resp_hook = on_response
        page.on("response", on_response)

    def is_logged_in(self):
        try:
            self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(5000)
            # login button nahi dikha = logged in
            btn = self.page.locator("button:has-text('Log in'), "
                                    "a:has-text('Log in')").first
            return not (btn.count() > 0 and btn.is_visible())
        except Exception:
            return False

    def wait_manual_login(self, timeout_s=600):
        """Visible browser me user login kare, hum detect karte rahein"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_logged_in():
                return True
            time.sleep(3)
        return False

    # ------------------------------------------------------------
    # Core: TRUE MITM — page ke apne fetch se request, response
    # network layer se intercept (DOM scraping nahi!)
    # ------------------------------------------------------------
    def chat(self, messages, timeout_s=120, stream_cb=None):
        """OpenAI messages -> intercepted SSE chunks (network-layer)"""
        with self.lock:
            return self._chat_locked(messages, timeout_s, stream_cb)

    def _chat_locked(self, messages, timeout_s=120, stream_cb=None):
        prompt = render_prompt(messages)
        page = self.page
        self._sse_pieces = []
        self._stream_cb = stream_cb
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # JS helper inject karo — SSE chunks Python ko bhejega
        page.evaluate("""() => {
            if (!window.__sseBuffer) {
                window.__sseBuffer = [];
                window.__sseDone = false;
            }
            window.__sseBuffer = [];
            window.__sseDone = false;
        }""")
        try:
            page.expose_function("__pyChunk", lambda c: self._on_chunk(c))
        except Exception:
            pass  # already exposed

        self._stream_cb = stream_cb
        result = page.evaluate(
            """async (args) => {
                const [prompt, timeoutMs, umid] = args;
                const withTimeout = (p, ms) =>
                    Promise.race([p, new Promise((_, rej) =>
                        setTimeout(() => rej(new Error("timeout " + ms)), ms))]);
                const H = () => ({
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "X-Request-Id": crypto.randomUUID(),
                    "source": "web",
                    "version": "0.2.87",
                    "bx-v": "2.5.37",
                    "timezone": new Date().toString(),
                });
                try {
                const tsMs = Date.now();
                const ts = Math.floor(tsMs / 1000);
                const uuid = () => crypto.randomUUID();

                // STEP 1: chats/new — page ke apne context me
                const r1 = await withTimeout(fetch("/api/v2/chats/new", {
                    method: "POST",
                    headers: H(),
                    credentials: "include",
                    body: JSON.stringify({
                        chatId: "", models: ["qwen3.7-plus"],
                        project_id: "", timestamp: tsMs,
                        chat_type: "t2t", chat_mode: "normal"}),
                }), 20000);
                const j1 = await r1.json();
                const cid = j1?.data?.id;
                if (!cid) return {error: "chats/new fail: " + JSON.stringify(j1).slice(0, 200)};

                // STEP 2: completions — SSE stream intercept
                const h2 = H();
                h2["Accept"] = "application/json";
                h2["x-accel-buffering"] = "no";
                if (umid) h2["bx-umidtoken"] = umid;
                const payload = {
                    stream: true, version: "2.1",
                    incremental_output: true,
                    chatId: cid, parentId: "", chat_id: cid,
                    chat_mode: "normal", model: "qwen3.7-plus",
                    parent_id: null,
                    messages: [{
                        id: null, fid: uuid(), parentId: null,
                        childrenIds: [], role: "user", content: prompt,
                        user_action: "chat", files: [],
                        timestamp: ts, models: ["qwen3.7-plus"],
                        model: "", chat_type: "t2t",
                        feature_config: {
                            thinking_enabled: false,
                            output_schema: "phase",
                            research_mode: "normal",
                            auto_thinking: false,
                            thinking_mode: "Auto",
                            thinking_format: "summary",
                            auto_search: false},
                        extra: {meta: {subChatType: "t2t"}},
                        sub_chat_type: "t2t", parent_id: null}],
                    timestamp: ts,
                };
                const r2 = await withTimeout(fetch(
                    "/api/v2/chat/completions?chat_id=" + cid, {
                    method: "POST",
                    headers: h2,
                    credentials: "include",
                    body: JSON.stringify(payload),
                }), 20000);
                if (!r2.ok || !(r2.body)) {
                    const t = await r2.text().catch(() => "");
                    return {error: "completions " + r2.status + ": " + t.slice(0, 200)};
                }
                const ct = r2.headers.get("content-type") || "";
                if (ct.includes("text/html")) {
                    const t = await r2.text();
                    return {error: "WAF challenge: " + t.slice(0, 120)};
                }

                // SSE reader — chunks window.__pyChunk ko
                const reader = r2.body.getReader();
                const dec = new TextDecoder();
                let buf = "";
                const deadline = Date.now() + timeoutMs;
                while (true) {
                    if (Date.now() > deadline)
                        return {error: "stream deadline", ok: false};
                    const {done, value} = await Promise.race([
                        reader.read(),
                        new Promise((_, rej) => setTimeout(
                            () => rej(new Error("read stall")), 45000)),
                    ]);
                    if (done) break;
                    buf += dec.decode(value, {stream: true});
                    const lines = buf.split("\\n");
                    buf = lines.pop();
                    for (const line of lines) {
                        const L = line.trim();
                        if (L.startsWith("data:")) {
                            if (window.__pyChunk)
                                window.__pyChunk(L.slice(5).trim());
                        }
                    }
                }
                if (window.__pyChunk) window.__pyChunk("[[DONE]]");
                return {ok: true, chat_id: cid};
                } catch (e) {
                    return {error: String(e).slice(0, 250)};
                }
            }""",
            [prompt, timeout_s * 1000, self._umid])

        if result and result.get("error"):
            raise RuntimeError(result["error"][:300])

        # chunks se text banao (stream_cb pehle se hi feed ho chuka)
        pieces = []
        for raw in self._sse_pieces:
            p = self._parse_chunk(raw)
            if p:
                pieces.append(p)
        return "".join(pieces).strip()

    def _on_chunk(self, raw):
        """JS se SSE line aayi — live stream callback + buffer"""
        self._sse_pieces.append(raw)
        if stream_cb := getattr(self, "_stream_cb", None):
            if raw != "[[DONE]]":
                piece = self._parse_chunk(raw)
                if piece:
                    try:
                        stream_cb(piece)
                    except Exception:
                        pass

    @staticmethod
    def _parse_chunk(raw):
        """Single SSE data-line -> text piece"""
        if raw == "[DONE]":
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if "phase" in d:
            if d.get("phase") in ("answer", "continue", None) and d.get("content"):
                return d["content"]
            return None
        choices = d.get("choices") or []
        if choices:
            ch = choices[0]
            delta = ch.get("delta", {}) or {}
            piece = (delta.get("content")
                     or delta.get("reasoning_content")
                     or (ch.get("message", {}) or {}).get("content"))
            return piece
        out = (d.get("output", {}) or {}).get("text")
        return out

    def stop(self):
        try:
            if self.ctx:
                self.ctx.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass


# ================================================================
# Prompt rendering + SSE parsing
# ================================================================

def render_prompt(messages):
    """OpenAI messages -> single prompt (multi-turn transcript style)"""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return messages[0].get("content", "")
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[Instructions]: {content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant reply]: {content}")
        else:
            parts.append(f"[User]: {content}")
    parts.append("Answer the LAST [User] message above directly.")
    return "\n\n".join(parts)


def parse_sse_body(body):
    """SSE text se poora assistant content nikaalo
    (OpenAI delta / Qwen v1 / Qwen v2 phase — sab formats)"""
    pieces = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            d = json.loads(raw)
        except Exception:
            continue
        # Qwen v2 phase schema: {"phase":"answer","content":"..."}
        if "phase" in d and d.get("content"):
            if d.get("phase") in ("answer", "continue", None):
                pieces.append(d["content"])
            continue
        # Qwen v2 custom: {"choices":[...]} with phase inside choice
        choices = d.get("choices") or []
        if choices:
            ch = choices[0]
            delta = ch.get("delta", {}) or {}
            piece = (delta.get("content")
                     or delta.get("reasoning_content")
                     or (ch.get("message", {}) or {}).get("content"))
            if piece:
                pieces.append(piece)
            continue
        # v1 style: {"output":{"text":"..."}}
        out = (d.get("output", {}) or {}).get("text")
        if out:
            pieces.append(out)
    return "".join(pieces).strip() or None


# ================================================================
# FastAPI server
# ================================================================

WORKER: "BrowserWorker" = None
app = FastAPI(title="Qwen Browser Bridge")


@app.get("/health")
async def health():
    return {"status": "ok",
            "browser": WORKER is not None and WORKER.ready.is_set()}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [
        {"id": "qwen", "object": "model", "owned_by": "qwen-bridge"},
        {"id": "qwen-max", "object": "model", "owned_by": "qwen-bridge"},
        {"id": "qwen-plus", "object": "model", "owned_by": "qwen-bridge"},
    ]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    is_stream = body.get("stream", False)
    requested_model = body.get("model", "qwen")

    def make_response(content, finish="stop"):
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        }

    def sse_chunk(delta, finish=None):
        c = {"id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
             "object": "chat.completion.chunk",
             "created": int(time.time()),
             "model": requested_model,
             "choices": [{"index": 0, "delta": delta,
                          "finish_reason": finish}]}
        return f"data: {json.dumps(c)}\n\n"

    if not is_stream:
        try:
            status, reply = WORKER.submit("chat", messages=messages)
            if status != "ok":
                return JSONResponse({"error": {"message": reply,
                                               "type": "bridge_error"}},
                                    status_code=502)
        except Exception as e:
            return JSONResponse({"error": {"message": str(e)[:300],
                                           "type": "bridge_error"}},
                                status_code=502)
        return JSONResponse(make_response(reply))

    # ---- TRUE streaming: intercepted chunks live forward ----
    import queue as _q
    q = _q.Queue()
    END = object()

    def cb(piece):
        q.put(piece)

    def run_chat():
        try:
            status, reply = WORKER.submit("chat", messages=messages,
                                          stream_cb=cb)
            if status != "ok":
                q.put(Exception(reply))
        except Exception as e:
            q.put(Exception(str(e)[:300]))
        finally:
            q.put(END)

    threading.Thread(target=run_chat, daemon=True).start()

    def gen():
        yield sse_chunk({"role": "assistant"})
        while True:
            item = q.get()
            if item is END:
                break
            if isinstance(item, Exception):
                err = {"error": {"message": str(item),
                                 "type": "bridge_error"}}
                yield f"data: {json.dumps(err)}\n\n"
                break
            yield sse_chunk({"content": item})
        yield sse_chunk({}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ================================================================
# Browser worker thread (playwright apne thread me rehta hai)
# ================================================================

class BrowserWorker(threading.Thread):
    """Queue-based browser worker — FastAPI thread-safe use kar sake"""

    def __init__(self, headless=True):
        super().__init__(daemon=True)
        self.headless = headless
        self.jobs = __import__("queue").Queue()
        self.ready = threading.Event()
        self.login_ok = None

    def run(self):
        global SESSION
        SESSION = BrowserSession(headless=self.headless)
        SESSION.start()
        self.ready.set()
        while True:
            job = self.jobs.get()
            if job is None:
                break
            kind, payload = job
            try:
                if kind == "chat":
                    result = ("ok", SESSION.chat(payload["messages"],
                                                 payload.get("timeout", 120),
                                                 stream_cb=payload.get("stream_cb")))
                elif kind == "check_login":
                    result = ("ok", SESSION.is_logged_in())
                else:
                    result = ("err", f"unknown job {kind}")
            except Exception as e:
                result = ("err", str(e)[:400])
            payload["_result"] = result

    def submit(self, kind, **payload):
        ev = threading.Event()
        payload["_result"] = None
        self.jobs.put((kind, payload))
        # poll for result (payload dict mutate hota hai)
        while payload["_result"] is None:
            time.sleep(0.2)
        return payload["_result"]


# ================================================================
# CLI
# ================================================================

def do_login(headless=False):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    if not headless and not os.environ.get("DISPLAY"):
        print("[!] Display nahi mila (headless box). Options:")
        print("    a) Apne desktop terminal se chalao: python3 qwen_browser_bridge.py --login")
        print("    b) Ya xvfb install karo: sudo apt install xvfb && xvfb-run python3 qwen_browser_bridge.py --login")
        sys.exit(1)
    s = BrowserSession(headless=headless)
    s.start()
    s.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    print("=" * 52)
    print(" BROWSER WINDOW ME LOGIN KARO (Google OAuth chalega)")
    print(" Login ke baad ye AUTOMATIC detect karega aur profile")
    print(" save ho jayega (browser_profile/) — dobara nahi karna!")
    print("=" * 52)
    ok = s.wait_manual_login(timeout_s=600)
    if ok:
        print("[+] LOGIN DETECTED! Profile saved.")
        # sanity: ek chhota test
        try:
            r = s.chat([{"role": "user",
                         "content": "Reply with exactly: BRIDGE-OK"}],
                       timeout_s=90)
            print(f"[+] Test reply: {r[:120]}")
        except Exception as e:
            print(f"[!] Test message fail ({str(e)[:120]}) — par login "
                  f"save ho gaya, server phir bhi chal sakta hai")
    else:
        print("[!] 10 min me login nahi hua")
    s.stop()
    return ok


def do_serve(headless=True, port=8001):
    global WORKER
    if not os.path.exists(os.path.join(PROFILE_DIR, "Default")):
        print("[!] browser_profile/ khali — pehle login karo:")
        print("    ./venv/bin/python qwen_browser_bridge.py --login")
        sys.exit(1)
    WORKER = BrowserWorker(headless=headless)
    WORKER.start()
    WORKER.ready.wait(timeout=60)
    print("[*] login check...")
    status, logged = WORKER.submit("check_login")
    if status != "ok" or not logged:
        print("[!] session expire ho gaya — dobara --login chalao")
        sys.exit(2)
    print(f"[+] logged in! Server starting: http://localhost:{port}/v1")
    # uvicorn apne thread me (playwright main thread ka loop kha gaya tha)
    import threading as _t
    uvicorn_cfg = uvicorn.Config(app, host="0.0.0.0", port=port,
                                 log_level="info")
    server = uvicorn.Server(uvicorn_cfg)
    server.run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--headed", action="store_true",
                    help="server bhi visible browser me (WAF zyada strict ho toh)")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()

    if args.login:
        sys.exit(0 if do_login(headless=False) else 1)
    elif args.serve:
        do_serve(headless=not args.headed, port=args.port)
    else:
        ap.print_help()

"""
Universal MITM Server — Qwen + Notion + Figma AI as OpenAI API (M2M)
=====================================================================
Ek server, saare connectors:
    POST /v1/chat/completions   {"model": "qwen"|"notion"|"figma", ...}
    GET  /v1/models
    GET  /health

M2M access:
    - 0.0.0.0 pe bind — LAN ke sab devices direct maar sakte hain
    - API key auth (UNIVERSAL_API_KEY env ya default "m2m-key")
    - Internet se chahiye? cloudflared/ngrok tunnel upar se

Usage:
    python3 universal_bridge.py --serve
    python3 universal_bridge.py --serve --port 8000 --api-key mera-secret
    python3 universal_bridge.py --login notion    # connector login
    python3 universal_bridge.py --status
"""

import argparse
import json
import os
import queue
import socket
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from universal_bridge import (CONNECTOR_CLASSES, render_prompt,
                              CONNECTORS_DIR)

DEFAULT_KEY = os.environ.get("UNIVERSAL_API_KEY", "m2m-key")
WORKERS = {}          # name -> BrowserWorker
WORKER_LOCK = threading.Lock()

app = FastAPI(title="Universal MITM Bridge — Qwen/Notion/Figma as API")


# ================================================================
# Worker (connector apne thread me)
# ================================================================

class BrowserWorker(threading.Thread):
    def __init__(self, connector_cls):
        super().__init__(daemon=True)
        self.connector = connector_cls()
        self.jobs = queue.Queue()
        self.ready = threading.Event()

    def run(self):
        try:
            self.connector.start()
        except Exception as e:
            print(f"[!] {self.connector.name} start fail: {e}")
            self.ready.set()
            return
        self.ready.set()
        while True:
            job = self.jobs.get()
            if job is None:
                break
            kind, payload = job
            try:
                if kind == "chat":
                    r = self.connector.chat(payload["messages"],
                                            payload.get("timeout", 120),
                                            stream_cb=payload.get("stream_cb"))
                    result = ("ok", r)
                elif kind == "check":
                    result = ("ok", self.connector.is_logged_in())
                else:
                    result = ("err", "unknown job")
            except Exception as e:
                result = ("err", str(e)[:400])
            payload["_result"] = result

    def submit(self, kind, **payload):
        if not self.ready.is_set():
            self.ready.wait(timeout=60)
        ev_result = payload.get("_result")
        payload["_result"] = None
        payload["stream_cb"] = payload.get("stream_cb")
        self.jobs.put((kind, payload))
        while payload["_result"] is None:
            time.sleep(0.2)
        return payload["_result"]


def get_worker(name):
    with WORKER_LOCK:
        if name not in WORKERS:
            if name not in CONNECTOR_CLASSES:
                return None
            w = BrowserWorker(CONNECTOR_CLASSES[name])
            w.start()
            WORKERS[name] = w
        return WORKERS[name]


# ================================================================
# Auth
# ================================================================

def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    key = request.query_params.get("key", "")
    provided = auth.replace("Bearer ", "").strip() or key
    return provided == DEFAULT_KEY


# ================================================================
# Endpoints
# ================================================================

@app.get("/health")
async def health():
    return {"status": "ok",
            "connectors": {n: w.ready.is_set() for n, w in WORKERS.items()}}


@app.get("/v1/models")
async def models():
    data = []
    for name, cls in CONNECTOR_CLASSES.items():
        has_flow = os.path.exists(os.path.join(
            CONNECTORS_DIR, f"{name}_flow.json")) or name == "qwen"
        data.append({"id": name, "object": "model",
                     "owned_by": "universal-bridge",
                     "ready": name in WORKERS and has_flow})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": {"message": "invalid api key",
                                       "type": "auth"}},
                            status_code=401)
    body = await request.json()
    model = body.get("model", "qwen")
    messages = body.get("messages", [])
    is_stream = body.get("stream", False)

    worker = get_worker(model)
    if worker is None:
        return JSONResponse({"error": {"message":
            f"model '{model}' nahi hai. Available: "
            f"{list(CONNECTOR_CLASSES.keys())}", "type": "bad_model"}},
            status_code=400)

    def make_resp(content):
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        }

    def sse(delta, finish=None):
        c = {"id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
             "object": "chat.completion.chunk",
             "created": int(time.time()),
             "model": model,
             "choices": [{"index": 0, "delta": delta,
                          "finish_reason": finish}]}
        return f"data: {json.dumps(c)}\n\n"

    if not is_stream:
        status, reply = worker.submit("chat", messages=messages)
        if status != "ok":
            return JSONResponse({"error": {"message": reply,
                                           "type": "connector_error"}},
                                status_code=502)
        return JSONResponse(make_resp(reply))

    # streaming
    q = queue.Queue()
    END = object()

    def cb(piece):
        q.put(piece)

    def run():
        try:
            status, reply = worker.submit("chat", messages=messages,
                                          stream_cb=cb)
            if status != "ok":
                q.put(Exception(reply))
        except Exception as e:
            q.put(Exception(str(e)[:300]))
        finally:
            q.put(END)

    threading.Thread(target=run, daemon=True).start()

    def gen():
        yield sse({"role": "assistant"})
        while True:
            item = q.get()
            if item is END:
                break
            if isinstance(item, Exception):
                yield f"data: {json.dumps({'error': {'message': str(item)}})}\n\n"
                break
            yield sse({"content": item})
        yield sse({}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ================================================================
# LAN IP helper (M2M)
# ================================================================

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        out = os.popen("hostname -I").read().split()
        for ip in out:
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


# ================================================================
# CLI
# ================================================================

def do_login(app_name):
    cls = CONNECTOR_CLASSES.get(app_name)
    if not cls:
        print(f"[!] '{app_name}' connector nahi hai. Available: "
              f"{list(CONNECTOR_CLASSES.keys())}")
        return 1
    c = cls()
    if not os.environ.get("DISPLAY"):
        print("[!] Display nahi — apne desktop se chalao ya xvfb use karo")
        return 1
    c.start()
    # visible window me dobara kholo login ke liye
    c.ctx.close()
    c.pw.stop()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch_persistent_context(
            c.profile_dir, headless=False, user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1400, "height": 950},
            args=["--no-sandbox"])
        page = b.pages[0] if b.pages else b.new_page()
        page.goto(c.login_url, wait_until="domcontentloaded", timeout=60000)
        print("=" * 52)
        print(f" {app_name.upper()} LOGIN — browser me login karo")
        print(" Login hone ke baad yahan Enter maaro")
        print("=" * 52)
        input("[Enter after login] > ")
        b.close()
    # verify headless me
    c2 = cls()
    c2.start()
    ok = c2.is_logged_in()
    c2.stop()
    print(f"[+] {app_name} login {'OK — saved!' if ok else 'FAIL — dobara try karo'}")
    return 0 if ok else 1


def do_status():
    print(" Connectors:")
    for name, cls in CONNECTOR_CLASSES.items():
        profile = os.path.join(CONNECTORS_DIR, f"profile_{name}")
        flow = os.path.join(CONNECTORS_DIR, f"{name}_flow.json")
        state = "flow READY" if os.path.exists(flow) else (
            "profile hai, capture baki" if os.path.exists(
                os.path.join(profile, "Default")) else "setup pending")
        if name == "qwen":
            state = "READY (built-in)"
        print(f"   {name:8s} : {state}")
    print(f"\n API key : {DEFAULT_KEY}")
    print(f" LAN IPs : {lan_ips()}")
    print("\n M2M example (kisi bhi device se):")
    ip = lan_ips()[0] if lan_ips() else "YOUR_IP"
    print(f'   curl http://{ip}:8000/v1/chat/completions \\')
    print(f'     -H "Authorization: Bearer {DEFAULT_KEY}" \\')
    print(f'     -H "Content-Type: application/json" \\')
    print(f'     -d \'{{"model":"qwen","messages":[{{"role":"user",'
                          f'"content":"hi"}}]}}\'')


def main():
    global DEFAULT_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--login", metavar="APP", help="qwen/notion/figma")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--api-key", default=DEFAULT_KEY)
    args = ap.parse_args()

    DEFAULT_KEY = args.api_key

    if args.login:
        sys.exit(do_login(args.login))
    elif args.status:
        do_status()
    elif args.serve:
        ips = lan_ips()
        print("=" * 56)
        print(" UNIVERSAL MITM BRIDGE")
        print(f" local  : http://localhost:{args.port}/v1")
        for ip in ips:
            print(f" LAN/M2M: http://{ip}:{args.port}/v1")
        print(f" api-key: {DEFAULT_KEY}")
        print(f" models : {list(CONNECTOR_CLASSES.keys())}")
        print("=" * 56)
        uvicorn.run(app, host="0.0.0.0", port=args.port)
    else:
        ap.print_help()


if __name__ == "__main__":
    import sys
    main()

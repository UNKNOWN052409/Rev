"""
Universal MITM Bridge — ek server, saare apps ke AI
====================================================
Pattern (har connector same):
  1. Persistent browser profile -> login ek baar
  2. Page ke andar fetch() -> app ke apne AI endpoint pe
     (real TLS/cookies/fingerprint = WAF pass)
  3. SSE response packets network layer se intercept
  4. OpenAI-compatible API me convert

Connectors:
  - QwenConnector   : ready (chat.qwen.ai)
  - NotionConnector : capture_flow.py se flow capture karo, phir replay
  - FigmaConnector  : same capture approach

Server:
  python3 universal_bridge.py --serve
  -> http://0.0.0.0:8000/v1  (LAN/internet — M2M ready)
  model="qwen" | "notion" | "figma"
"""

import argparse
import json
import os
import queue
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
"""

CONNECTORS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "connectors")
os.makedirs(CONNECTORS_DIR, exist_ok=True)


# ================================================================
# Base connector — MITM fetch pattern (har app ke liye same)
# ================================================================

class BaseConnector:
    """Ek app ka bridge. Subclass: LOGIN_URL, build_fetch_js(), parse_chunk()"""
    name = "base"
    login_url = ""
    profile_dir = ""

    def __init__(self):
        self.pw = None
        self.ctx = None
        self.page = None
        self.lock = threading.Lock()
        self._chunks = []
        self._stream_cb = None

    # ---- lifecycle ----
    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            self.profile_dir, headless=True, user_agent=UA,
            viewport={"width": 1366, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.add_init_script(STEALTH)

    def stop(self):
        try:
            self.ctx.close()
            self.pw.stop()
        except Exception:
            pass

    def is_logged_in(self):
        """Subclass override karo — login state detect"""
        try:
            self.page.goto(self.login_url, wait_until="domcontentloaded",
                           timeout=45000)
            self.page.wait_for_timeout(4000)
            btn = self.page.locator(
                "button:has-text('Log in'), a:has-text('Log in'), "
                "button:has-text('Sign up')").first
            return not (btn.count() > 0 and btn.is_visible())
        except Exception:
            return False

    # ---- MITM core ----
    def chat(self, messages, timeout_s=120, stream_cb=None, model=None):
        """Default: captured flow replay (notion/figma ke liye).
        Qwen apna override karta hai. model param ignore hota hai."""
        with self.lock:
            self._chunks = []
            self._stream_cb = stream_cb
            flow = self.load_flow()
            if not flow:
                raise RuntimeError(
                    f"{self.name}: flow capture nahi hua — "
                    f"'python3 capture_flow.py --app {self.name}' chalao")
            return self._replay_flow(flow, render_prompt(messages),
                                     timeout_s)

    def _replay_flow(self, flow, prompt, timeout_s):
        """Captured flow ko page-context fetch se replay karo.
        flow: {url, method, headers, body_template} — body me
        __PROMPT__ placeholder replace hota hai."""
        js_body = json.dumps(flow["body_template"]).replace(
            "__PROMPT__", "\\__PROMPT__")  # placeholder safe
        # prompt inject — JSON string me replace karna risky, isliye
        # JS side pe placeholder string replace karte hain
        result = self.page.evaluate(
            """async (args) => {
                const [flowJson, prompt, timeoutMs] = args;
                const withTimeout = (p, ms) =>
                    Promise.race([p, new Promise((_, rej) =>
                        setTimeout(() => rej(new Error("timeout")), ms))]);
                try {
                const flow = JSON.parse(flowJson);
                let body = JSON.stringify(flow.body_template);
                body = body.split("__PROMPT__").join(prompt);
                const headers = Object.assign(
                    {"Content-Type": "application/json"},
                    flow.headers || {});
                const r = await withTimeout(fetch(flow.url, {
                    method: flow.method || "POST",
                    headers, credentials: "include", body,
                }), 25000);
                if (!r.ok) {
                    const t = await r.text().catch(() => "");
                    return {error: "status " + r.status + ": " + t.slice(0, 200)};
                }
                const ct = r.headers.get("content-type") || "";
                if (ct.includes("text/html"))
                    return {error: "WAF/challenge page"};
                // SSE ya JSON dono handle
                const raw = await withTimeout(r.text(), timeoutMs);
                window.__rawResponse = raw.slice(0, 50000);
                if (window.__pyChunk) window.__pyChunk("[[RAW]]" + raw.slice(0, 200000));
                return {ok: true, size: raw.length};
                } catch (e) {
                    return {error: String(e).slice(0, 250)};
                }
            }""",
            [json.dumps(flow), prompt, timeout_s * 1000])
        if result and result.get("error"):
            raise RuntimeError(result["error"][:300])
        # raw response parse
        raw = ""
        for c in self._chunks:
            if c.startswith("[[RAW]]"):
                raw = c[7:]
                break
        return self.parse_response(raw)

    # ---- helpers ----
    def load_flow(self):
        path = os.path.join(CONNECTORS_DIR, f"{self.name}_flow.json")
        if os.path.exists(path):
            return json.load(open(path))
        return None

    def save_flow(self, flow):
        with open(os.path.join(CONNECTORS_DIR,
                               f"{self.name}_flow.json"), "w") as f:
            json.dump(flow, f, indent=2)

    def parse_response(self, raw):
        """Subclass override — raw SSE/JSON -> text"""
        return raw[:500]

    def _parse_chunk(self, raw):
        return None

    def _on_chunk(self, raw):
        self._chunks.append(raw)
        if raw.startswith("[[RAW]]"):
            return
        if self._stream_cb:
            piece = self.parse_chunk(raw) if hasattr(
                self, "parse_chunk") else self._parse_chunk(raw)
            if piece:
                try:
                    self._stream_cb(piece)
                except Exception:
                    pass


def render_prompt(messages):
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


# ================================================================
# QWEN connector (working — proven MITM flow)
# ================================================================

class QwenConnector(BaseConnector):
    name = "qwen"
    login_url = "https://chat.qwen.ai"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_qwen")

    # alias -> real model id (chat.qwen.ai/api/models se)
    MODEL_ALIASES = {
        "qwen": "qwen3.7-plus",
        "qwen-plus": "qwen3.7-plus",
        "qwen-max": "qwen3.8-max",
        "qwen3.7-plus": "qwen3.7-plus",
        "qwen3.8-max": "qwen3.8-max",
    }

    def __init__(self):
        super().__init__()
        self._umid = ""
        try:
            flow = json.load(open("captured_v2_flow.json"))
            self._umid = flow["requests"][0]["headers"].get("bx-umidtoken", "")
        except Exception:
            pass

    def chat(self, messages, timeout_s=120, stream_cb=None, model="qwen"):
        real = self.MODEL_ALIASES.get(model, "qwen3.7-plus")
        with self.lock:
            self._chunks = []
            self._stream_cb = stream_cb
            return self._qwen_chat(render_prompt(messages), timeout_s, real)

    def _qwen_chat(self, prompt, timeout_s, model_id="qwen3.7-plus"):
        page = self.page
        page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        try:
            page.expose_function("__pyChunk", lambda c: self._on_chunk(c))
        except Exception:
            pass

        result = page.evaluate(
            """async (args) => {
                const [prompt, timeoutMs, umid, modelId] = args;
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
                const r1 = await withTimeout(fetch("/api/v2/chats/new", {
                    method: "POST", headers: H(), credentials: "include",
                    body: JSON.stringify({chatId: "",
                        models: [modelId], project_id: "",
                        timestamp: tsMs, chat_type: "t2t",
                        chat_mode: "normal"}),
                }), 20000);
                const j1 = await r1.json();
                const cid = j1?.data?.id;
                if (!cid) return {error: "chats/new: " + JSON.stringify(j1).slice(0,200)};
                const h2 = H();
                h2["Accept"] = "application/json";
                h2["x-accel-buffering"] = "no";
                if (umid) h2["bx-umidtoken"] = umid;
                const payload = {
                    stream: true, version: "2.1",
                    incremental_output: true,
                    chatId: cid, parentId: "", chat_id: cid,
                    chat_mode: "normal", model: modelId,
                    parent_id: null,
                    messages: [{id: null, fid: crypto.randomUUID(),
                        parentId: null, childrenIds: [], role: "user",
                        content: prompt, user_action: "chat", files: [],
                        timestamp: ts, models: [modelId],
                        model: "", chat_type: "t2t",
                        feature_config: {thinking_enabled: false,
                            output_schema: "phase",
                            research_mode: "normal",
                            auto_thinking: false,
                            thinking_mode: "Auto",
                            thinking_format: "summary",
                            auto_search: false},
                        extra: {meta: {subChatType: "t2t"}},
                        sub_chat_type: "t2t", parent_id: null}],
                    timestamp: ts};
                const r2 = await withTimeout(fetch(
                    "/api/v2/chat/completions?chat_id=" + cid, {
                    method: "POST", headers: h2, credentials: "include",
                    body: JSON.stringify(payload)}), 20000);
                if (!r2.ok || !r2.body) {
                    const t = await r2.text().catch(() => "");
                    return {error: "completions " + r2.status + ": " + t.slice(0,200)};
                }
                const ct = r2.headers.get("content-type") || "";
                if (ct.includes("text/html"))
                    return {error: "WAF challenge"};
                const reader = r2.body.getReader();
                const dec = new TextDecoder();
                let buf = "";
                const deadline = Date.now() + timeoutMs;
                while (true) {
                    if (Date.now() > deadline)
                        return {error: "stream deadline"};
                    const {done, value} = await Promise.race([
                        reader.read(),
                        new Promise((_, rej) => setTimeout(
                            () => rej(new Error("stall")), 45000))]);
                    if (done) break;
                    buf += dec.decode(value, {stream: true});
                    const lines = buf.split("\\n");
                    buf = lines.pop();
                    for (const L of lines) {
                        const t = L.trim();
                        if (t.startsWith("data:") && window.__pyChunk)
                            window.__pyChunk(t.slice(5).trim());
                    }
                }
                if (window.__pyChunk) window.__pyChunk("[[DONE]]");
                return {ok: true};
                } catch (e) {
                    return {error: String(e).slice(0, 250)};
                }
            }""",
            [prompt, timeout_s * 1000, self._umid, model_id])

        if result and result.get("error"):
            raise RuntimeError(result["error"][:300])
        pieces = []
        for raw in self._chunks:
            if raw in ("[[DONE]]",) or raw.startswith("[[RAW]]"):
                continue
            p = self.parse_chunk(raw)
            if p:
                pieces.append(p)
        text = "".join(pieces).strip()
        if not text:
            raise RuntimeError("empty reply")
        return text

    @staticmethod
    def parse_chunk(raw):
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
            return (delta.get("content")
                    or delta.get("reasoning_content")
                    or (ch.get("message", {}) or {}).get("content"))
        return (d.get("output", {}) or {}).get("text")

    parse_response = lambda self, raw: parse_sse_full(raw, QwenConnector.parse_chunk)


def parse_sse_full(raw, chunk_parser):
    pieces = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            p = chunk_parser(line[5:].strip())
            if p:
                pieces.append(p)
    return "".join(pieces).strip()


# ================================================================
# NOTION / FIGMA connectors — capture-based replay
# ================================================================

class NotionConnector(BaseConnector):
    name = "notion"
    login_url = "https://www.notion.so/login"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_notion")

    @staticmethod
    def parse_chunk(raw):
        try:
            d = json.loads(raw)
        except Exception:
            return None
        # Notion AI SSE shapes (capture ke baad adjust hoga)
        if isinstance(d, dict):
            for key in ("content", "text", "delta", "answer"):
                v = d.get(key)
                if isinstance(v, str):
                    return v
            ch = (d.get("choices") or [{}])[0]
            delta = ch.get("delta", {}) or {}
            if delta.get("content"):
                return delta["content"]
        return None

    parse_response = lambda self, raw: parse_sse_full(raw, NotionConnector.parse_chunk)


class FigmaConnector(BaseConnector):
    name = "figma"
    login_url = "https://www.figma.com/login"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_figma")

    @staticmethod
    def parse_chunk(raw):
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if isinstance(d, dict):
            for key in ("content", "text", "message", "delta"):
                v = d.get(key)
                if isinstance(v, str):
                    return v
            ch = (d.get("choices") or [{}])[0]
            delta = ch.get("delta", {}) or {}
            if delta.get("content"):
                return delta["content"]
        return None

    parse_response = lambda self, raw: parse_sse_full(raw, FigmaConnector.parse_chunk)


CONNECTOR_CLASSES = {
    "qwen": QwenConnector,
    "notion": NotionConnector,
    "figma": FigmaConnector,
}

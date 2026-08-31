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
import base64
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

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
    """Qwen Web Chat — TRUE MITM, pure HTTP (curl_cffi).
    No browser at runtime — sirf login ke baad token reuse.
    Captured: captured_v2_flow.json
    Flow: login → chats/new → chat/completions → SSE.
    Auth: cookie-based (token=JWT)."""
    name = "qwen"
    login_url = "https://chat.qwen.ai"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_qwen")
    api = "https://chat.qwen.ai/api/v2"

    MODEL_ALIASES = {
        "qwen": "qwen3.7-plus",
        "qwen-plus": "qwen3.7-plus",
        "qwen-max": "qwen3.8-max",
        "qwen3.7-plus": "qwen3.7-plus",
        "qwen3.8-max": "qwen3.8-max",
    }

    HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "source": "web",
        "version": "0.2.87",
        "bx-v": "2.5.37",
        "timezone": "Mon Aug 24 2026 18:05:53 GMT+0000",
        "bx-ua": "default_not_value",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", '
                     '"Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.0.0 Safari/537.36"),
        "accept-language": "en-US,en;q=0.9",
    }

    def __init__(self):
        super().__init__()
        self._token = ""
        self._umid = ""

    def start(self):
        self._load_token()

    def _load_token(self):
        token_path = os.path.join(
            os.path.dirname(CONNECTORS_DIR), "qwen_token.json")
        if os.path.exists(token_path):
            with open(token_path) as f:
                data = json.load(f)
                self._token = data.get("token", "")
                self._umid = data.get("umid", "")

    def _save_token(self):
        token_path = os.path.join(
            os.path.dirname(CONNECTORS_DIR), "qwen_token.json")
        with open(token_path, "w") as f:
            json.dump({"token": self._token, "umid": self._umid}, f)

    def is_logged_in(self):
        return bool(self._token)

    def _ensure_login(self):
        if self._token:
            return
        self.login_with_browser()

    def login_with_browser(self):
        """Browser se token harvest — profile use karo.
        ghostrise optional; nahi to plain playwright persistent context.
        Qwen guest access allow karta hai — token bina login milta hai."""
        try:
            from ghostrise.engine import GhostSession
        except ImportError:
            GhostSession = None
        if GhostSession is None:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch_persistent_context(
                    self.profile_dir, headless=True, user_agent=UA,
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = b.pages[0] if b.pages else b.new_page()
                page.goto("https://chat.qwen.ai",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                self._token = page.evaluate(
                    "() => localStorage.getItem('token') || ''") or ""
                if not self._token:
                    cookies = page.evaluate("() => document.cookie")
                    m = re.search(r'token=([^;]+)', cookies or "")
                    if m:
                        self._token = m.group(1)
                self._umid = page.evaluate("""() => {
                    const m = document.cookie.match(/bx-umidtoken=([^;]+)/);
                    return m ? m[1] : '';
                }""")
                b.close()
            if self._token:
                self._save_token()
                print("[+] qwen: token saved", flush=True)
                return
            raise RuntimeError("qwen: token nahi mila")
        with GhostSession(profile="ds_login_qwen", humanize=True) as s:
            page = s.browser.new_page()
            page.goto("https://chat.qwen.ai",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(20000)

            # Extract token from localStorage
            ls = page.evaluate("""() => {
                const keys = Object.keys(localStorage);
                for (const k of keys) {
                    const v = localStorage[k];
                    if (v && v.length > 50 && v.includes('.')) {
                        try {
                            const d = JSON.parse(v);
                            if (d.value && d.value.token)
                                return d.value.token;
                        } catch(e) {}
                    }
                }
                // Try direct token key
                const t = localStorage.getItem('token');
                if (t) return t;
                return '';
            }""")
            if ls:
                self._token = ls

            # Extract bx-umidtoken from page
            self._umid = page.evaluate("""() => {
                // It's in cookies
                const m = document.cookie.match(
                    /bx-umidtoken=([^;]+)/);
                return m ? m[1] : '';
            }""")

            if not self._token:
                # Try cookie extraction
                cookies = page.evaluate("() => document.cookie")
                m = re.search(r'token=([^;]+)', cookies)
                if m:
                    self._token = m.group(1)

            if self._token:
                self._save_token()
                print("[+] qwen: token saved", flush=True)
            else:
                raise RuntimeError("qwen: token nahi mila")

    def _http_session(self):
        """curl_cffi session — WAF warmup (landing GET) ke saath.
        Live-proven recipe (26 Aug): Session + landing GET ->
        acw_tc cookies -> token cookie -> chats/new 200."""
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome131")
        s.cookies.set("token", self._token, domain=".qwen.ai")
        try:
            s.get("https://chat.qwen.ai/", headers={
                "user-agent": self.HEADERS["user-agent"],
                "accept": ("text/html,application/xhtml+xml,"
                           "application/xml;q=0.9,*/*;q=0.8"),
                "accept-language": "en-US,en;q=0.9"}, timeout=20)
        except Exception:
            pass
        return s

    def chat(self, messages, timeout_s=120, stream_cb=None,
             model="qwen"):
        with self.lock:
            self._ensure_login()
            prompt = render_prompt(messages)
            real_model = self.MODEL_ALIASES.get(model, "qwen3.7-plus")

            s = self._http_session()
            headers = {
                **self.HEADERS,
                "referer": "https://chat.qwen.ai/c/new-chat",
                "origin": "https://chat.qwen.ai",
                "X-Request-Id": str(uuid.uuid4()),
                "bx-umidtoken": self._umid,
            }

            # 1. Create new chat
            r1 = s.post(
                self.api + "/chats/new",
                headers=headers,
                json={"chatId": "", "models": [real_model],
                      "project_id": "", "timestamp": int(time.time()),
                      "chat_type": "t2t", "chat_mode": "normal"},
                timeout=15)
            if r1.status_code != 200:
                raise RuntimeError("chats/new: HTTP " +
                                   str(r1.status_code))
            cd = r1.json()
            chat_id = cd.get("data", {}).get("id", "")
            if not chat_id:
                raise RuntimeError("chats/new: no id — " +
                                   r1.text[:200])

            # 2. Send message (SSE)
            body = {
                "stream": True,
                "version": "2.1",
                "incremental_output": True,
                "chatId": chat_id,
                "parentId": "",
                "chat_id": chat_id,
                "chat_mode": "normal",
                "model": real_model,
                "parent_id": None,
                "messages": [{
                    "id": None,
                    "fid": str(uuid.uuid4()),
                    "parentId": None,
                    "childrenIds": [str(uuid.uuid4())],
                    "role": "user",
                    "content": prompt,
                    "user_action": "chat",
                    "files": [],
                    "timestamp": int(time.time()),
                    "models": [real_model],
                    "model": "",
                    "chat_type": "t2t",
                    "feature_config": {
                        "thinking_enabled": True,
                        "output_schema": "phase",
                        "research_mode": "normal",
                        "auto_thinking": True,
                        "thinking_mode": "Auto",
                        "thinking_format": "summary",
                        "auto_search": True,
                    },
                    "extra": {"meta": {"subChatType": "t2t"}},
                    "sub_chat_type": "t2t",
                    "parent_id": None,
                }],
                "timestamp": int(time.time()),
            }
            headers2 = {
                **headers,
                "x-accel-buffering": "no",
                "Accept": "application/json",
                "X-Request-Id": str(uuid.uuid4()),
            }
            r2 = s.post(
                self.api + "/chat/completions?chat_id=" + chat_id,
                headers=headers2, json=body,
                timeout=(15, timeout_s), stream=True)
            if r2.status_code != 200:
                raise RuntimeError("completions: HTTP " +
                                   str(r2.status_code) +
                                   " " + r2.text[:200])

            # 3. Parse SSE
            pieces = []
            for line in r2.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "ignore")
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                piece = self.parse_chunk(data)
                if piece:
                    pieces.append(piece)
                    if stream_cb:
                        try:
                            stream_cb(piece)
                        except Exception:
                            pass
            text = "".join(pieces).strip()
            if not text:
                raise RuntimeError("qwen: empty reply")
            return text

    @staticmethod
    def parse_chunk(raw):
        if raw in ("[DONE]", "null", ""):
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if "phase" in d:
            if d.get("phase") in ("answer", "continue", None) \
                    and d.get("content"):
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

    parse_response = lambda self, raw: parse_sse_full(
        raw, QwenConnector.parse_chunk)


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
# NOTION connector — PURE HTTP (runInferenceTranscript, NDJSON)
# ================================================================

NOTION_MODELS = {
    "sonnet-4.6": "almond-croissant-low",
    "sonnet-5": "angel-cake-high",
    "opus-4.6": "avocado-froyo-medium",
    "opus-4.7": "apricot-sorbet-high",
    "opus-4.8": "ambrosia-tart-high",
    "opus-5": "agave-flan",
    "gpt-5.2": "oatmeal-cookie",
    "gpt-5.4": "oval-kumquat-medium",
    "gpt-5.5": "opal-quince-medium",
    "gpt-5.6-luna": "olive-jellyroll",
    "gpt-5.6-terra": "orchid-muffin",
    "gpt-5.6-sol": "orange-mousse",
    "gpt-5.4-mini": "oregon-grape-medium",
    "gpt-5.4-nano": "otaheite-apple-medium",
    "grok-4.6": "soursop-shortcake",
    "grok-4.3": "xigua-mochi-medium",
    "gemini-3.5-flash": "vertex-gemini-3.5-flash",
    "gemini-3.6-flash": "vertex-gemini-3.6-flash",
    "gemini-3.7-flash": "grapefruit-zeppole",
    "kimi-k2.6": "fireworks-kimi-k2.6",
    "kimi-k2.7-code": "fireworks-kimi-k2.7",
    "kimi-k3": "fireworks-kimi-k3",
    "deepseek-v4-pro": "baseten-deepseek-v4-pro",
    "deepseek-v4-flash": "baseten-deepseek-v4-flash",
    "glm-5.2": "baseten-glm-5.2",
}


class NotionConnector(BaseConnector):
    """Notion AI — pure HTTP, no browser needed.
    Auth: token_v2 cookie (login_otp flow se milta hai).
    Flow intel: connectors/notion_ai_flow.json"""
    name = "notion"
    login_url = "https://www.notion.so/login"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_notion")
    api = "https://www.notion.so/api/v3"
    default_model = "almond-croissant-low"  # Sonnet 4.6

    MODEL_ALIASES = {
        "notion": default_model,
        "notion-ai": default_model,
        "notion-sonnet": default_model,
        "notion-sonnet-4-6": default_model,
        "notion-sonnet-5": "angel-cake-high",
        "notion-opus": "avocado-froyo-medium",
        "notion-opus-4-6": "avocado-froyo-medium",
        "notion-opus-4-7": "apricot-sorbet-high",
        "notion-opus-4-8": "ambrosia-tart-high",
        "notion-opus-5": "agave-flan",
        "notion-claude-opus-5": "agave-flan",
        "notion-gpt": "oatmeal-cookie",
        "notion-gpt-5-2": "oatmeal-cookie",
        "notion-gpt-5-6-terra": "orchid-muffin",
        "notion-grok": "soursop-shortcake",
        "notion-gemini": "vertex-gemini-3.5-flash",
        "notion-kimi": "fireworks-kimi-k2.6",
        "notion-deepseek": "baseten-deepseek-v4-pro",
    }

    def __init__(self):
        super().__init__()
        self.auth = None

    # playwright skip — pure HTTP hai
    def start(self):
        self._load_auth()

    def _load_auth(self):
        if self.auth:
            return
        auth = {}
        tv2_path = os.path.join(os.path.dirname(CONNECTORS_DIR),
                                "notion_token_v2.txt")
        if os.path.exists(tv2_path):
            with open(tv2_path) as f:
                auth["token_v2"] = f.read().strip()
        flow_p = os.path.join(CONNECTORS_DIR, "notion_ai_flow.json")
        if os.path.exists(flow_p):
            with open(flow_p) as f:
                fl = json.load(f)
            b = fl.get("body", {})
            auth["space_id"] = b.get("spaceId", "")
            tr = b.get("transcript", [])
            for blk in tr:
                if blk.get("type") == "context":
                    v = blk.get("value", {})
                    auth.update({k: v.get(k) for k in
                                 ("userId", "userEmail", "userName",
                                  "spaceName", "spaceViewId", "timezone")})
        if auth.get("token_v2") and auth.get("space_id"):
            self.auth = auth
        else:
            raise RuntimeError(
                "notion: auth nahi mila — notion_token_v2.txt + "
                "connectors/notion_ai_flow.json chahiye")

    def is_logged_in(self):
        try:
            self._load_auth()
            return True
        except Exception:
            return False

    def resolve_model(self, model):
        m = (model or "").strip()
        if m in self.MODEL_ALIASES:
            return self.MODEL_ALIASES[m]
        short = m.replace("notion-", "").replace("notion_", "")
        if short in NOTION_MODELS:
            return NOTION_MODELS[short]
        if short in NOTION_MODELS.values():
            return short
        return self.default_model

    REPLACE_MARK = "\x00RPL\x00"

    @staticmethod
    def parse_chunk(raw):
        """Ek NDJSON line -> text piece (ya None).
        Do patterns handle karta hai:
        - append: {"o":"a", v:{type:text, content}} -> delta
        - replace: {"o":"p", p:.../content, v:"full text"} -> REPLACE_MARK+full
        - agent-inference blocks ke andar ke text bhi pakdo
        """
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if not isinstance(d, dict) or d.get("type") != "patch":
            return None
        out = []
        for op in d.get("v", []):
            o = op.get("o")
            p = op.get("p", "")
            v = op.get("v")
            if o == "p" and isinstance(p, str) and p.endswith("/content") \
                    and isinstance(v, str):
                out.append(NotionConnector.REPLACE_MARK + v)
            elif isinstance(v, dict):
                if v.get("type") == "text" and isinstance(v.get("content"), str) \
                        and v["content"]:
                    out.append(v["content"])
                elif v.get("type") == "agent-inference":
                    for item in v.get("value", []) or []:
                        if isinstance(item, dict) and item.get("type") == "text" \
                                and isinstance(item.get("content"), str) \
                                and item["content"]:
                            out.append(item["content"])
        return "".join(out) if out else None

    def chat(self, messages, timeout_s=120, stream_cb=None, model="notion"):
        from curl_cffi import requests as cr
        with self.lock:
            self._load_auth()
            a = self.auth
            prompt = render_prompt(messages)
            mid = self.resolve_model(model)
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist).isoformat(timespec="milliseconds")
            uid = a.get("userId", "")
            sid = a["space_id"]
            transcript = [
                {"id": str(uuid.uuid4()), "type": "config",
                 "value": {"type": "workflow", "modelFromUser": True,
                           "model": mid,
                           "useWebSearch": True, "internetAccess": False,
                           "isHipaa": False, "useReadOnlyMode": False,
                           "writerMode": False, "isCustomAgent": False,
                           "isMobile": False, "availableConnectors": [],
                           "customConnectorInfo": [],
                           "searchScopes": [{"type": "everything"}]}},
                {"id": str(uuid.uuid4()), "type": "context",
                 "value": {"timezone": a.get("timezone", "Asia/Kolkata"),
                           "userName": a.get("userName", "user"),
                           "userId": uid,
                           "userEmail": a.get("userEmail", ""),
                           "spaceName": a.get("spaceName", ""),
                           "spaceId": sid,
                           "spaceViewId": a.get("spaceViewId", ""),
                           "currentDatetime": now,
                           "surface": "ai_module"}},
                {"id": str(uuid.uuid4()), "type": "user",
                 "value": [[prompt]], "userId": uid, "createdAt": now},
            ]
            body = {
                "traceId": str(uuid.uuid4()), "spaceId": sid,
                "transcript": transcript,
                "threadId": str(uuid.uuid4()),
                "createThread": True, "isPartialTranscript": False,
                "generateTitle": False, "saveAllThreadOperations": False,
                "setUnreadState": False, "threadType": "workflow",
                "asPatchResponse": True, "patchResponseVersion": 2,
                "hasHeartbeat": False, "createdSource": "ai_module",
                "isUserInAnySalesAssistedSpace": False,
                "isSpaceSalesAssisted": False,
                "threadParentPointer": {"table": "space", "id": sid,
                                        "spaceId": sid},
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
                "Origin": "https://www.notion.so",
                "Referer": "https://www.notion.so/ai",
                "User-Agent": UA,
                "Cookie": ("token_v2=" + a["token_v2"] +
                           "; notion_user_id=" + uid),
                "x-notion-active-user-header": uid,
                "x-notion-space-id": sid,
                "notion-audit-log-platform": "web",
                "notion-client-version": "23.13.20260825.1237",
            }
            r = cr.post(self.api + "/runInferenceTranscript", json=body,
                        headers=headers, impersonate="chrome131",
                        timeout=(15, timeout_s), stream=True)
            if r.status_code in (401, 403):
                raise RuntimeError(
                    "notion: auth fail (" + str(r.status_code) +
                    ") — token_v2 expire, naya OTP login chahiye")
            if r.status_code != 200:
                raise RuntimeError("notion: HTTP " + str(r.status_code) +
                                   " " + r.text[:200])
            pieces = []
            final = []
            for line in r.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "ignore")
                if not line:
                    continue
                piece = self.parse_chunk(line)
                if not piece:
                    continue
                pieces.append(piece)
                if self.REPLACE_MARK in piece:
                    # replace semantics: buffer reset + full text
                    full = piece.split(self.REPLACE_MARK, 1)[1]
                    final = [full]
                    if stream_cb:
                        try:
                            stream_cb("\n" + full)
                        except Exception:
                            pass
                else:
                    final.append(piece)
                    if stream_cb:
                        try:
                            stream_cb(piece)
                        except Exception:
                            pass
            text = "".join(final).strip()
            if not text:
                raise RuntimeError(
                    "notion: empty stream — credits khatam ya format badla")
            return text


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


class DeepSeekChatConnector(BaseConnector):
    """DeepSeek Web Chat — TRUE MITM, pure HTTP (curl_cffi).
    No browser at runtime — sirf login ke baad token reuse.
    Captured: connectors/ds_chat_capture.json
    Flow: login → create_pow_challenge → solve → chat/completion → SSE.
    PoW: x-ds-pow-response header (base64 JSON)."""
    name = "deepseek"
    login_url = "https://chat.deepseek.com/sign_in"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_ds_chat")
    api = "https://chat.deepseek.com/api/v0"

    MODEL_ALIASES = {
        "deepseek": "default",
        "deepseek-instant": "default",
        "deepseek-expert": "expert",
        "deepseek-deep-think": "deep_think",
        "deepseek-think": "deep_think",
        "deepseek-search": "search",
        "deepseek-vision": "vision",
    }

    HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-client-locale": "en_US",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-version": "2.4.0",
        "x-client-platform": "web",
        "x-client-timezone-offset": "0",
        "Referer": "https://chat.deepseek.com/",
    }

    def __init__(self):
        super().__init__()
        self._token = ""
        self._uid = ""

    def start(self):
        self._load_token()

    def _load_token(self):
        token_path = os.path.join(
            os.path.dirname(CONNECTORS_DIR), "deepseek_token.txt")
        if os.path.exists(token_path):
            with open(token_path) as f:
                data = json.load(f)
                self._token = data.get("token", "")
                self._uid = data.get("uid", "")

    def _save_token(self):
        token_path = os.path.join(
            os.path.dirname(CONNECTORS_DIR), "deepseek_token.txt")
        with open(token_path, "w") as f:
            json.dump({"token": self._token, "uid": self._uid}, f)

    def is_logged_in(self):
        return bool(self._token)

    def _ensure_login(self):
        if self._token:
            return
        # Browser se ek baar login (WAF ke liye)
        self.login_with_browser()

    def login_with_browser(self):
        """Browser page-context fetch se login — WAF-cooked session me
        request jaati hai (Cloudflare-style 202 challenge bypass).
        Credentials env se: DS_EMAIL / DS_PASSWORD.
        Token capture + save. ghostrise optional hai."""
        email = os.environ.get("DS_EMAIL", "")
        password = os.environ.get("DS_PASSWORD", "")
        if not (email and password):
            raise RuntimeError(
                "deepseek: DS_EMAIL/DS_PASSWORD env set karo, "
                "phir 'rev login deepseek' chalao")
        try:
            from ghostrise.engine import GhostSession
        except ImportError:
            GhostSession = None
        if GhostSession is None:
            from playwright.sync_api import sync_playwright
            ctx = p_chromium = None
            with sync_playwright() as p:
                b = p.chromium.launch_persistent_context(
                    self.profile_dir, headless=True, user_agent=UA,
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = b.pages[0] if b.pages else b.new_page()
                page.goto(self.login_url,
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                self._page_login_fetch(page, email, password)
                b.close()
            return
        with GhostSession(profile="ds_login", humanize=True) as s:
            page = s.browser.new_page()
            page.goto(self.login_url, wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(8000)
            self._page_login_fetch(page, email, password)

    def _page_login_fetch(self, page, email, password):
        """Page ke andar fetch() se login API call — WAF cookies ke
        saath request jaati hai."""
        lr = page.evaluate("""async (args) => {
            const [api, email, password, deviceId] = args;
            const r = await fetch(api + '/users/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    email: email, password: password,
                    locale: 'en_US', device_id: deviceId,
                    os: 'Windows'})
            });
            const t = await r.text();
            return {status: r.status, text: t.slice(0, 600)};
        }""", [self.api, email, password, str(uuid.uuid4())])
        if lr.get("status") != 200:
            raise RuntimeError("deepseek: login HTTP " +
                               str(lr.get("status")) + " — WAF/login fail")
        ld = json.loads(lr["text"])
        user = (ld.get("data", {}).get("biz_data", {})
                .get("user", {}))
        self._token = user.get("token", "")
        self._uid = user.get("id", "")
        if not self._token:
            raise RuntimeError("deepseek: login fail — " +
                               lr["text"][:200])
        self._save_token()

    def _solve_pow(self, s, target_path="/api/v0/chat/completion"):
        """PoW challenge solve — DeepSeekHashV1 (SHA-256 brute force).
        s: warm curl_cffi session."""
        r = s.post(
            self.api + "/chat/create_pow_challenge",
            headers={**self.HEADERS,
                     "Authorization": "Bearer " + self._token},
            json={"target_path": target_path},
            timeout=15)
        if r.status_code != 200:
            raise RuntimeError("pow: HTTP " + str(r.status_code))
        bd = r.json().get("data", {}).get("biz_data", {}).get(
            "challenge", {})
        challenge = bd.get("challenge", "")
        salt = bd.get("salt", "")
        signature = bd.get("signature", "")
        algo = bd.get("algorithm", "DeepSeekHashV1")
        # Difficulty: challenge string ke end me "bits" hota hai
        # (DeepSeek format: <random>_<target_bits>) — leading zeros
        # count = bits // 4. Fallback: 1 hex zero.
        bits = 0
        if "_" in challenge:
            try:
                bits = int(challenge.rsplit("_", 1)[1])
            except ValueError:
                bits = 0
        zeros = max(1, bits // 4)
        prefix = "0" * zeros
        # Brute force nonce (main thread me ~1-2s typical)
        for nonce in range(100_000_000):
            h = hashlib.sha256(
                (salt + challenge + str(nonce)).encode()).hexdigest()
            if h.startswith(prefix):
                solution = {
                    "algorithm": algo,
                    "challenge": challenge,
                    "salt": salt,
                    "answer": nonce,
                    "signature": signature,
                    "target_path": target_path,
                }
                return base64.b64encode(
                    json.dumps(solution).encode()).decode()
        raise RuntimeError("pow: solve failed (zeros=" +
                           str(zeros) + ")")

    def _http_session(self):
        """curl_cffi session — landing warmup ke saath (WAF cookies)."""
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome131")
        s.cookies.set("userToken", self._token, domain=".deepseek.com")
        try:
            s.get("https://chat.deepseek.com/", headers={
                "user-agent": self.HEADERS.get(
                    "user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36")},
                timeout=20)
        except Exception:
            pass
        return s

    def chat(self, messages, timeout_s=120, stream_cb=None,
             model="deepseek"):
        with self.lock:
            self._ensure_login()
            prompt = render_prompt(messages)
            model_type = self.MODEL_ALIASES.get(model, "default")

            s = self._http_session()
            # 1. Create session
            r1 = s.post(
                self.api + "/chat_session/create",
                headers={**self.HEADERS,
                         "Authorization": "Bearer " + self._token},
                json={},
                timeout=15)
            if r1.status_code != 200:
                raise RuntimeError("session: HTTP " +
                                   str(r1.status_code))
            sid = (r1.json().get("data", {}).get("biz_data", {})
                   .get("chat_session", {}).get("id", ""))
            if not sid:
                raise RuntimeError("session: no id — " +
                                   r1.text[:200])

            # 2. PoW solve
            pow_header = self._solve_pow(s)

            # 3. Chat completion (SSE)
            body = {
                "chat_session_id": sid,
                "parent_message_id": None,
                "model_type": model_type,
                "prompt": prompt,
                "ref_file_ids": [],
                "thinking_enabled": model_type == "deep_think",
                "search_enabled": True,
                "action": None,
                "preempt": False,
            }
            headers = {
                **self.HEADERS,
                "Authorization": "Bearer " + self._token,
                "x-ds-pow-response": pow_header,
                "Referer": ("https://chat.deepseek.com/"
                            "a/chat/s/" + sid),
            }
            r2 = s.post(
                self.api + "/chat/completion",
                headers=headers, json=body,
                timeout=(15, timeout_s), stream=True)
            if r2.status_code != 200:
                raise RuntimeError("completion: HTTP " +
                                   str(r2.status_code) +
                                   " " + r2.text[:200])

            # 4. Parse SSE
            pieces = []
            for line in r2.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "ignore")
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    if "close" in line:
                        break
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    piece = self.parse_chunk(data)
                    if piece:
                        pieces.append(piece)
                        if stream_cb:
                            try:
                                stream_cb(piece)
                            except Exception:
                                pass
            text = "".join(pieces).strip()
            if not text:
                raise RuntimeError("deepseek: empty reply")
            return text

    @staticmethod
    def parse_chunk(raw):
        if raw in ("[DONE]", "null", ""):
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if isinstance(d, dict):
            v = d.get("v")
            if isinstance(v, dict):
                resp = v.get("response")
                if isinstance(resp, dict):
                    frags = resp.get("fragments", [])
                    if frags:
                        return frags[0].get("content")
                if v.get("o") == "APPEND" and v.get("v"):
                    return v["v"]
            if d.get("content"):
                return d["content"]
        return None

    parse_response = lambda self, raw: parse_sse_full(
        raw, DeepSeekChatConnector.parse_chunk)




# ================================================================
# CHATGPT connector (chatgpt.com) — TRUE MITM, pure HTTP replay
# ================================================================

class ChatGPTConnector(BaseConnector):
    """ChatGPT Web — MITM via captured session token.
    Runtime: pure HTTP (curl_cffi). Browser sirf token harvest ke liye.
    Flow: GET /backend-api/models -> POST /backend-api/conversation -> SSE.
    Auth: __Secure-next-auth.session-token cookie.
    Token file: chatgpt_session.json {token: ...}"""
    name = "chatgpt"
    login_url = "https://chatgpt.com/auth/login"
    profile_dir = os.path.join(CONNECTORS_DIR, "profile_chatgpt")
    api = "https://chatgpt.com/backend-api"

    MODEL_ALIASES = {
        "chatgpt": "gpt-5.2",
        "chatgpt-auto": "auto",
        "gpt-4.1": "gpt-4.1",
        "gpt-4o": "gpt-4o",
        "gpt-5": "gpt-5.2",
        "gpt-5.2": "gpt-5.2",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-5-nano": "gpt-5-nano",
        "gpt-5.5": "gpt-5.5",
        "gpt-5.5-chat-latest": "gpt-5.5-chat-latest",
    }

    HEADERS = {
        "Accept": "text/event-stream",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "accept-language": "en-US,en;q=0.9",
        "OAI-Device-Id": "",
        "OAI-Client-Version": "prod-2812efc862",
    }

    def __init__(self):
        super().__init__()
        self._session_token = ""
        self._access_token = ""
        self._device_id = ""

    def start(self):
        self._load_token()

    def _token_path(self):
        return os.path.join(os.path.dirname(CONNECTORS_DIR),
                            "chatgpt_session.json")

    def _load_token(self):
        p = self._token_path()
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            self._session_token = d.get("session_token", "")
            self._access_token = d.get("access_token", "")
            self._device_id = d.get("device_id", "") or str(uuid.uuid4())

    def _save_token(self):
        with open(self._token_path(), "w") as f:
            json.dump({"session_token": self._session_token,
                       "access_token": self._access_token,
                       "device_id": self._device_id}, f)

    def is_logged_in(self):
        return bool(self._session_token)

    def _ensure_login(self):
        if self._session_token:
            return
        self.login_with_browser()

    def login_with_browser(self):
        """Browser se session token harvest — login/profile use karo.
        Visible browser me khud login karo, tool cookie pakad leta hai."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch_persistent_context(
                self.profile_dir, headless=True, user_agent=UA,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = b.pages[0] if b.pages else b.new_page()
            page.goto("https://chatgpt.com", wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(6000)
            tok = None
            for c in b.cookies("https://chatgpt.com"):
                if c["name"] == "__Secure-next-auth.session-token":
                    tok = c["value"]
                    break
            if tok:
                self._session_token = tok
                self._save_token()
                print("[+] chatgpt: session token saved", flush=True)
            else:
                raise RuntimeError(
                    "chatgpt: session-token cookie nahi mila — "
                    "pehle visible browser me login karo: "
                    "universal_server.py --login chatgpt")
            b.close()

    def _http_session(self):
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome131")
        s.cookies.set("__Secure-next-auth.session-token",
                      self._session_token, domain="chatgpt.com")
        try:
            s.get("https://chatgpt.com/", headers={
                "user-agent": self.HEADERS["user-agent"],
                "accept": "text/html,*/*",
                "accept-language": "en-US,en;q=0.9"}, timeout=20)
        except Exception:
            pass
        return s

    def _ensure_access_token(self, s):
        """session-token -> Bearer access_token (auth/api/v1/session)."""
        if self._access_token:
            return
        r = s.get("https://chatgpt.com/api/auth/session",
                  headers={"user-agent": self.HEADERS["user-agent"]},
                  timeout=15)
        if r.status_code != 200:
            raise RuntimeError("chatgpt: session fetch HTTP " +
                               str(r.status_code) + " " + r.text[:150])
        at = r.json().get("accessToken", "")
        if not at:
            raise RuntimeError(
                "chatgpt: accessToken nahi mila — session token expire")
        self._access_token = at
        self._save_token()

    def _chat_requirements(self, s):
        """POST /backend-api/.../chat-requirements -> openai-sentinel
        headers (proof-of-work token)."""
        r = s.post(
            self.api + "/sentinel/chat-requirements",
            headers={**self.HEADERS,
                     "Authorization": "Bearer " + self._access_token,
                     "Content-Type": "application/json",
                     "OAI-Device-Id": self._device_id},
            json={"p": self._device_id},
            timeout=15)
        if r.status_code != 200:
            raise RuntimeError("chatgpt: chat-requirements HTTP " +
                               str(r.status_code) + " " + r.text[:150])
        return r.json()

    def chat(self, messages, timeout_s=120, stream_cb=None,
             model="chatgpt"):
        with self.lock:
            self._ensure_login()
            prompt = render_prompt(messages)
            real_model = self.MODEL_ALIASES.get(model, "gpt-5.2")

            s = self._http_session()
            self._ensure_access_token(s)
            req = self._chat_requirements(s)
            pow_token = req.get("token", "")  # openai-sentinel-proof
            arkose = req.get("arkose", {}) or {}
            arkose_token = arkose.get("value", "")

            headers = {
                **self.HEADERS,
                "Authorization": "Bearer " + self._access_token,
                "Content-Type": "application/json",
                "OAI-Device-Id": self._device_id,
                "openai-sentinel-proof-token": pow_token,
            }
            if arkose_token:
                headers["openai-sentinel-arkose-token"] = arkose_token

            # conversation body — ChatGPT web shape
            msg_id = str(uuid.uuid4())
            body = {
                "action": "next",
                "messages": [{
                    "id": msg_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text",
                                "parts": [prompt]},
                    "metadata": {},
                }],
                "parent_message_id": str(uuid.uuid4()),
                "model": real_model,
                "timezone_offset_min": 0,
                "suggestions": [],
                "history_and_training_disabled": False,
                "conversation_mode": {"kind": "primary_assistant"},
                "force_paragen": False,
                "force_paragen_model_slug": "",
                "force_nulligen": False,
                "force_rate_limit": False,
                "reset_rate_limits": False,
                "websocket_request_id": str(uuid.uuid4()),
                "system_hints": [],
                "supported_encodings": ["v1"],
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 47,
                    "page_height": 690,
                    "page_width": 1275,
                    "screen_height": 818,
                    "screen_width": 1296,
                    "scroll_x": 0, "scroll_y": 0,
                },
                "paragen_streamed_response_cot_only": False,
                "paragen_cot_summary_display_override": "allow",
                "supports_buffering": True,
            }
            r = s.post(self.api + "/conversation", headers=headers,
                       json=body, timeout=(15, timeout_s), stream=True)
            if r.status_code in (401, 403):
                # access token stale — ek baar refresh retry
                self._access_token = ""
                self._ensure_access_token(s)
                headers["Authorization"] = (
                    "Bearer " + self._access_token)
                r = s.post(self.api + "/conversation",
                           headers=headers, json=body,
                           timeout=(15, timeout_s), stream=True)
            if r.status_code == 428 or "arkose" in r.text[:300].lower():
                raise RuntimeError(
                    "chatgpt: arkose/proof-of-work challenge — "
                    "device token stale, dobara login karo")
            if r.status_code != 200:
                raise RuntimeError("chatgpt: conversation HTTP " +
                                   str(r.status_code) + " " +
                                   r.text[:200])
            pieces = []
            for line in r.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "ignore")
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                piece = self.parse_chunk(data)
                if piece:
                    pieces.append(piece)
                    if stream_cb:
                        try:
                            stream_cb(piece)
                        except Exception:
                            pass
            text = "".join(pieces).strip()
            if not text:
                raise RuntimeError("chatgpt: empty reply")
            return text

    @staticmethod
    def parse_chunk(raw):
        """ChatGPT SSE frames: data: {"v": ..., "o": "append", ...}
        ya legacy {message: {content: {parts: [...]}}}."""
        if raw in ("[DONE]", "null", ""):
            return None
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        # v2 frames (o = op, v = value)
        v = d.get("v")
        if isinstance(v, dict):
            msg = v.get("message") or {}
            content = (msg.get("content") or {})
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list) and parts:
                    return parts[-1]
            recipient = msg.get("recipient")
            if recipient == "all" and isinstance(v.get("delta"), str):
                return v["delta"]
        # legacy frames
        msg = d.get("message") or {}
        content = msg.get("content") or {}
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list) and parts:
                return parts[-1]
        return None


CONNECTOR_CLASSES = {
    "qwen": QwenConnector,
    "notion": NotionConnector,
    "deepseek": DeepSeekChatConnector,
    "chatgpt": ChatGPTConnector,
    "figma": FigmaConnector,
}

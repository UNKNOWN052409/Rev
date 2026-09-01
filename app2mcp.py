#!/usr/bin/env python3
"""
app2mcp — kisi bhi app ko MCP + API me convert karo
====================================================
Rev Kit ka general converter. Mobile app / web app / desktop app —
koi bhi app jiska koi official API/MCP nahi, usko capture karo aur
ye tool uska:

  1. REST API      : FastAPI server (OpenAI-compatible /v1 endpoints)
  2. MCP server    : FastMCP SSE server (AI agents ke liye tools)
  3. Rust engine   : proxy.git engine ka GenericFlowAdapter config
                     (re_capture/flows/<app>.config.json)

sab kuch auto-generate kar deta hai — capture se.

Pipeline:
  mitm_start (mobile_re.py)          [phone/web app traffic]
      |
  app2mcp analyze                    [endpoints + flows score]
      |
  app2mcp build <app> [--from-flow]  [API + MCP server + engine config]
      |
  app2mcp serve <app>                [REST :<port> + MCP :<mcp_port>]

Articles/content apps bhi same pipeline: capture me jo bhi content
endpoint mila (GET/POST, JSON/HTML), uska MCP tool ban jaata hai
(read_article, search, list — jo bhi endpoint capture hua).

Usage:
    python3 app2mcp.py analyze [--session re_capture/session.jsonl]
    python3 app2mcp.py build <app> [--endpoint "POST /api/chat"]
                                [--all]   # top endpoints, ek multi-tool server
    python3 app2mcp.py serve <app> [--port 8000] [--mcp-port 9880]
    python3 app2mcp.py list
    python3 app2mcp.py android     # phone capture quickstart (proxy+cert+frida)

Android apps: mitmdump -s mobile_re.py chalao, phone proxy point karo,
app browse karo, phir analyze → build --all → serve. Koi bhi app
(articles, feed, chat — Android/web/desktop) isi pipeline se MCP+API
ban jaata hai — uska koi official API nahi hona chahiye.

Authorized personal use only — apne hi accounts, apna hi traffic.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REV_ROOT = os.path.dirname(HERE)
CAPTURE_DIR = os.path.join(REV_ROOT, "re_capture")
SESSION_FILE = os.path.join(CAPTURE_DIR, "session.jsonl")
FLOWS_DIR = os.path.join(CAPTURE_DIR, "flows")
OUT_DIR = os.path.join(REV_ROOT, "app2mcp_servers")

# auto_pipeline ke hi heuristics — same scoring, same language
CHAT_BODY_HINTS = ["messages", "prompt", "input", "query", "contents", "text"]
CHAT_URL_HINTS = ["chat", "completion", "generate", "conversation",
                  "message", "inference", "ask"]
RESP_HINTS = ["choices", "output", "content", "response", "answer"]

AUTH_HEADER_NAMES = [
    "authorization", "x-api-key", "x-auth-token", "x-token",
    "cookie", "token", "api-key", "x-session-token",
]
KEEP_HEADERS = ["user-agent", "content-type", "origin", "referer",
                "accept", "x-fe-version", "bx-v", "source",
                "x-client-version", "x-client-platform", "x-client-locale",
                "x-client-bundle-id", "x-client-timezone-offset"]


def load_flows(session_file=None):
    sf = session_file or SESSION_FILE
    if not os.path.exists(sf):
        print(f"[!] {sf} nahi mila. Pehle capture karo:")
        print("    mitmdump -s mobile_re.py   (phone proxy point karo)")
        sys.exit(1)
    flows = []
    with open(sf, encoding="utf-8") as f:
        for line in f:
            try:
                flows.append(json.loads(line))
            except Exception:
                continue
    return flows


# ================================================================
# 1. ANALYZE — capture se candidate endpoints nikaalo
# ================================================================

def score_chat_flow(flow):
    score, reasons = 0, []
    if flow.get("method") == "POST":
        score += 2
        reasons.append("POST")
    body_raw = flow.get("req_body", "")
    try:
        body = json.loads(body_raw)
    except Exception:
        body = None
    if isinstance(body, dict):
        score += 2
        reasons.append("json-body")
        keys = {k.lower() for k in body}
        hits = [k for k in CHAT_BODY_HINTS if any(k in kb for kb in keys)]
        if hits:
            score += 3 * len(hits)
            reasons.append(f"body:{hits}")
        if "messages" in keys and isinstance(body.get("messages"), list):
            score += 3
            reasons.append("messages-array")

    url_l = flow.get("url", "").lower()
    url_hits = [h for h in CHAT_URL_HINTS if h in url_l]
    if url_hits:
        score += 2 * len(url_hits)
        reasons.append(f"url:{url_hits}")

    res_l = (flow.get("res_body", "") or "")[:2000].lower()
    resp_hits = [h for h in RESP_HINTS if h in res_l]
    if resp_hits:
        score += min(2 * len(resp_hits), 6)
        reasons.append(f"resp:{resp_hits[:3]}")
    return score, reasons


def score_content_flow(flow):
    """Articles/content endpoints — chat nahi, par readable content."""
    score, reasons = 0, []
    res = flow.get("res_body", "") or ""
    # readable text signals
    text_len = len(re.sub(r"<[^>]+>|\s", "", res))
    if text_len > 800:
        score += 3
        reasons.append(f"readable:{text_len}ch")
    url_l = flow.get("url", "").lower()
    for h in ["article", "post", "story", "feed", "news", "content",
              "read", "item", "detail", "page", "search", "list"]:
        if h in url_l:
            score += 1
            reasons.append(f"url:{h}")
    ctype = (flow.get("res_headers") or {}).get("content-type", "")
    if "json" in ctype:
        score += 1
        reasons.append("json")
    if flow.get("method") in ("GET", "POST"):
        score += 1
    return score, reasons


def extract_auth(headers):
    auth = {}
    for k, v in (headers or {}).items():
        kl = k.lower()
        if kl in AUTH_HEADER_NAMES or kl.startswith("x-"):
            auth[k] = v
        elif kl in KEEP_HEADERS:
            auth[k] = v
    return auth


def json_paths_with_text(v, path="", out=None, depth=0):
    """Response body me jin JSON paths pe text content hai."""
    if out is None:
        out = []
    if depth > 5 or len(out) >= 12:
        return out
    if isinstance(v, dict):
        for k, val in v.items():
            p = f"{path}/{k}"
            if isinstance(val, str) and len(val) > 40 and not val.startswith("http"):
                out.append(p)
            elif isinstance(val, (dict, list)):
                json_paths_with_text(val, p, out, depth + 1)
    elif isinstance(v, list) and v:
        json_paths_with_text(v[0], f"{path}/0", out, depth + 1)
    return out


def cmd_analyze(args):
    flows = load_flows(args.session)
    print(f"[+] {len(flows)} flows loaded")

    scored_chat, scored_content = [], []
    for fl in flows:
        s, r = score_chat_flow(fl)
        if s >= 5:
            scored_chat.append((s, r, fl))
        s2, r2 = score_content_flow(fl)
        if s2 >= 4:
            scored_content.append((s2, r2, fl))

    scored_chat.sort(key=lambda x: -x[0])
    scored_content.sort(key=lambda x: -x[0])

    print("\n=== CHAT/AI endpoints (chat apps) ===")
    for s, r, fl in scored_chat[:5]:
        print(f"  {s:3d} | {fl['method']:4s} | {fl['url'][:70]}")
        print(f"       ({', '.join(r[:4])})")

    print("\n=== CONTENT endpoints (articles/feed apps) ===")
    for s, r, fl in scored_content[:8]:
        print(f"  {s:3d} | {fl['method']:4s} | {fl['url'][:70]}")
        print(f"       ({', '.join(r[:4])})")

    if not scored_chat and not scored_content:
        print("[!] kuch nahi mila — app me thoda browse karo, phir analyze")

    # save candidates
    cand = {
        "chat": [
            {"url": fl["url"], "method": fl["method"], "score": s,
             "reasons": r[:6]}
            for s, r, fl in scored_chat[:5]
        ],
        "content": [
            {"url": fl["url"], "method": fl["method"], "score": s,
             "reasons": r[:6]}
            for s, r, fl in scored_content[:10]
        ],
        "analyzed_at": time.time(),
        "flow_count": len(flows),
    }
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    out = os.path.join(CAPTURE_DIR, "app2mcp_candidates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cand, f, indent=2, ensure_ascii=False)
    print(f"\n[+] saved -> {out}")
    print("[*] ab: python3 app2mcp.py build <appname> [flags]")


# ================================================================
# 2. BUILD — app ka API + MCP + engine config generate
# ================================================================

def pick_flow(app, args, kind="chat"):
    """Best flow pick: --endpoint override ya candidates file se."""
    flows = load_flows(args.session)
    if args.endpoint:
        method, _, path = args.endpoint.partition(" ")
        path = path.strip()
        for fl in flows:
            if fl.get("method", "GET").upper() == method.upper() and \
               path.rstrip("/") in fl.get("url", ""):
                return fl, kind
        print(f"[!] --endpoint '{args.endpoint}' capture me nahi mila")
        sys.exit(1)
    # auto: score se
    best, best_s, best_kind = None, -1, kind
    for fl in flows:
        for k, scorer in (("chat", score_chat_flow),
                          ("content", score_content_flow)):
            s, _ = scorer(fl)
            if s > best_s:
                best, best_s, best_kind = fl, s, k
    if best is None or best_s < 4:
        print("[!] koi eligible flow nahi mila — analyze chalao pehle")
        sys.exit(1)
    print(f"[+] picked {best_kind} flow (score {best_s}): "
          f"{best['method']} {best['url'][:70]}")
    return best, best_kind


def body_template_for(flow, kind):
    """Captured body -> template (${MESSAGES}/${QUERY} placeholders)."""
    try:
        body = json.loads(flow.get("req_body", "") or "{}")
    except Exception:
        return {}, {}
    template = dict(body)
    variables = {}
    for k in list(template.keys()):
        kl = k.lower()
        if isinstance(template[k], list) and any(
                h in kl for h in CHAT_BODY_HINTS):
            template[k] = "${MESSAGES}"
        elif isinstance(template[k], str) and kl in (
                "prompt", "input", "query", "text", "q", "search",
                "keyword", "content"):
            variables["QUERY"] = template[k]
            template[k] = "${QUERY}"
    return template, variables


def response_paths_for(flow, kind):
    """Response se content extraction paths (engine ke liye)."""
    try:
        res = json.loads(flow.get("res_body", "") or "null")
    except Exception:
        return ["/text"] if kind == "content" else \
            ["/choices/0/message/content", "/content"]
    paths = json_paths_with_text(res)
    if paths:
        return ["/choices/0/message/content"] + paths[:6] \
            if kind == "chat" else paths[:8]
    return ["/text"]


def url_query_vars(url):
    """URL query params -> template vars (search/filter apps)."""
    out = {}
    if "?" in url:
        for pair in url.split("?", 1)[1].split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if re.fullmatch(r"[A-Za-z0-9_\-]{6,}", v) or v.isdigit():
                    out[k] = v
    return out


def cmd_build(args):
    app = args.app
    if getattr(args, "all_eps", False):
        build_all(app, args)
        return
    flow, kind = pick_flow(app, args)
    url = flow["url"]
    method = flow.get("method", "POST").upper()
    auth_headers = extract_auth(flow.get("req_headers", {}))
    body_tpl, body_vars = body_template_for(flow, kind)
    resp_paths = response_paths_for(flow, kind)
    url_vars = url_query_vars(url)
    is_sse = "event-stream" in (
        (flow.get("res_headers") or {}).get("content-type", ""))

    print(f"[+] app={app} kind={kind} sse={is_sse}")
    print(f"    endpoint: {method} {url[:80]}")
    print(f"    auth headers: {len(auth_headers)} | "
          f"body vars: {list(body_vars)} | url vars: {list(url_vars)}")

    # ---- 1. engine config (GenericFlowAdapter plug) ----
    os.makedirs(FLOWS_DIR, exist_ok=True)
    engine_cfg = {
        "name": app,
        "upstream_url": url,
        "method": method,
        "upstream_headers": auth_headers,
        "body_template": body_tpl,
        "model_map": {app: app},
        "response_paths": resp_paths,
        "is_sse": is_sse,
        "_meta": {"kind": kind, "built_at": time.time(),
                  "source": "app2mcp"},
    }
    eng_path = os.path.join(FLOWS_DIR, f"{app}.config.json")
    with open(eng_path, "w", encoding="utf-8") as f:
        json.dump(engine_cfg, f, indent=2, ensure_ascii=False)
    print(f"[+] engine config -> {eng_path}")
    print("    (revd start karo to model list me "
          f"'{app}' aa jaayega — FLOW_CONFIG_DIR={FLOWS_DIR})")

    # ---- 2. Python REST + MCP server generate ----
    os.makedirs(OUT_DIR, exist_ok=True)
    srv = generate_server(app, engine_cfg, kind, url_vars)
    srv_path = os.path.join(OUT_DIR, f"{app}_server.py")
    with open(srv_path, "w", encoding="utf-8") as f:
        f.write(srv)
    print(f"[+] REST + MCP server -> {srv_path}")
    print(f"    run: python3 {srv_path} [--port 8000] [--mcp-port 9880]")

    # ---- 3. README for the person converting the app ----
    doc = generate_readme(app, engine_cfg, srv_path, eng_path)
    with open(os.path.join(OUT_DIR, f"{app}_README.md"), "w",
              encoding="utf-8") as f:
        f.write(doc)
    print(f"[+] docs -> {os.path.join(OUT_DIR, f'{app}_README.md')}")
    print(f"\n[done] {app} ab ek API + MCP hai. serve karke test karo.")


# ================================================================
# 3. GENERATED SERVER TEMPLATE (articles + chat dono)
# ================================================================

def generate_server(app, cfg, kind, url_vars):
    """Clean, self-contained FastAPI + MCP server source for the app."""
    h = json.dumps(cfg["upstream_headers"], indent=4)
    b = json.dumps(cfg["body_template"], indent=4)
    p = json.dumps(cfg["response_paths"], indent=4)
    u = json.dumps(cfg["upstream_url"])
    m = json.dumps(cfg["method"])
    s = "True" if cfg["is_sse"] else "False"
    uv = json.dumps(url_vars, indent=4)

    return f'''#!/usr/bin/env python3
"""
AUTO-GENERATED by Rev Kit app2mcp — app: {app} (kind: {kind})
==============================================================
{app} app ka captured internal endpoint, ab ek public API + MCP.

REST:
    GET  /health
    GET  /v1/models
    POST /v1/chat/completions   (OpenAI-compatible, SSE streaming)
    POST /invoke                 (raw passthrough)
    GET  /read?query=...         (content/article apps)

MCP (AI agents, --mcp-port, default 9880):
    tools: {app}_read, {app}_search, {app}_chat

Authorized personal use only — captured session apna hai.
"""

import json
import threading
import time
import uuid

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

APP = {json.dumps(app)}
KIND = {json.dumps(kind)}
UPSTREAM_URL = {u}
UPSTREAM_METHOD = {m}
UPSTREAM_HEADERS = {h}
BODY_TEMPLATE = {b}
RESPONSE_PATHS = {p}
IS_SSE = {s}
URL_VARS = {uv}

api = FastAPI(title=f"{{APP}} API (Rev Kit app2mcp)")
_lock = threading.Lock()
_query = {{}}  # URL var overrides per request


def extract_text(data):
    if isinstance(data, str):
        return data
    for p in RESPONSE_PATHS:
        try:
            v = data
            for part in p.strip("/").split("/"):
                if isinstance(v, list):
                    v = v[int(part)] if part.isdigit() and int(part) < len(v) else v
                elif isinstance(v, dict):
                    v = v.get(part)
                else:
                    break
            if isinstance(v, str) and v.strip():
                return v
        except Exception:
            continue
    return ""


def render_body(query, model):
    body = json.loads(json.dumps(BODY_TEMPLATE))
    def walk(v):
        if isinstance(v, str):
            return (v.replace("${{MESSAGES}}", query)
                     .replace("${{QUERY}}", query)
                     .replace("${{MODEL}}", model or APP))
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, dict):
            return {{k2: walk(x) for k2, x in v.items()}}
        return v
    return walk(body)


def render_url():
    url = UPSTREAM_URL
    for k, v in URL_VARS.items():
        url = url.replace(v, str(_query.get(k, v)))
    return url


def upstream_call(query, model=None, stream_cb=None, raw_body=None):
    with _lock:
        try:
            body = raw_body or render_body(query, model)
            r = requests.request(
                UPSTREAM_METHOD, render_url(), headers=UPSTREAM_HEADERS,
                json=body if isinstance(body, dict) else None,
                data=None if isinstance(body, dict) else body,
                stream=IS_SSE, timeout=180)
            if r.status_code != 200:
                raise RuntimeError(f"upstream HTTP {{r.status_code}}")
            if IS_SSE and stream_cb:
                out = []
                for line in r.iter_lines():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "ignore")
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d in ("[DONE]", ""):
                        continue
                    try:
                        piece = extract_text(json.loads(d))
                    except Exception:
                        piece = ""
                    if piece:
                        out.append(piece)
                        stream_cb(piece)
                return "".join(out)
            text = r.text
            try:
                return extract_text(json.loads(text))
            except Exception:
                return text
        finally:
            _query.clear()


def sse_frame(model, delta, finish=None):
    return ("data: " + json.dumps({{
        "id": f"chatcmpl-{{uuid.uuid4().hex[:12]}}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{{"index": 0, "delta": delta,
                    "finish_reason": finish}}],
    }}) + "\\n\\n")


def render_prompt_msgs(msgs):
    parts = []
    for mm in msgs:
        r = mm.get("role", "user")
        if r == "system":
            parts.append(f"[Instructions]: {{mm.get('content', '')}}")
        elif r == "assistant":
            parts.append(f"[Previous reply]: {{mm.get('content', '')}}")
        else:
            parts.append(f"[User]: {{mm.get('content', '')}}")
    parts.append("Answer the LAST [User] message directly.")
    return "\\n\\n".join(parts)


@api.get("/health")
def health():
    return {{"app": APP, "kind": KIND, "ok": True}}


@api.get("/v1/models")
def models():
    return {{"object": "list", "data": [
        {{"id": APP, "object": "model", "owned_by": "rev-app2mcp"}}]}}


@api.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = json.loads(await req.body())
    msgs = body.get("messages", [])
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        prompt = msgs[0].get("content", "")
    else:
        prompt = render_prompt_msgs(msgs)
    model = body.get("model", APP)
    max_tokens = body.get("max_tokens") or 0
    if max_tokens:
        prompt += (f"\\n\\n[Output budget: complete, thorough answer of "
                   f"roughly {{max_tokens}} tokens. Do not stop early.]")
    batch = body.get("batch") or []
    if batch:
        numbered = "\\n".join(f"{{i+1}}. {{s}}" for i, s in enumerate(batch))
        prompt += (f"\\n\\n[BATCH MODE — answer EVERY item in order, "
                   f"separated by === <number> === headers.]\\n{{numbered}}")
    for k in URL_VARS:
        _query[k] = prompt

    if body.get("stream"):
        def gen():
            buf = []
            done = []
            def cb(p):
                buf.append(p)
            def run():
                try:
                    upstream_call(prompt, model, cb)
                finally:
                    done.append(True)
            threading.Thread(target=run, daemon=True).start()
            sent = 0
            deadline = time.time() + 180
            while time.time() < deadline:
                if sent < len(buf):
                    for p in buf[sent:]:
                        yield sse_frame(model, {{"content": p}})
                    sent = len(buf)
                if done and sent >= len(buf):
                    yield sse_frame(model, {{}}, finish="stop")
                    yield "data: [DONE]\\n\\n"
                    return
                time.sleep(0.05)
        return StreamingResponse(gen(),
                                  media_type="text/event-stream")

    result = upstream_call(prompt, model)
    return {{
        "id": f"chatcmpl-{{uuid.uuid4().hex[:12]}}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{{"index": 0,
                     "message": {{"role": "assistant", "content": result}},
                     "finish_reason": "stop"}}],
        "usage": {{"prompt_tokens": len(prompt) // 4,
                  "completion_tokens": len(result) // 4,
                  "total_tokens": (len(prompt) + len(result)) // 4}},
    }}


@api.post("/invoke")
async def invoke(req: Request):
    bd = json.loads(await req.body())
    query = bd.get("query") or bd.get("prompt") or bd.get("q") or ""
    for k in URL_VARS:
        _query[k] = query
    result = upstream_call(query, bd.get("model"),
                           raw_body=bd.get("raw_body"))
    return {{"app": APP, "result": result}}


@api.get("/read")
def read(query: str = ""):
    for k in URL_VARS:
        _query[k] = query
    result = upstream_call(query or "latest")
    return {{"app": APP, "query": query, "content": result}}


def run_mcp(port):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("[!] pip install mcp — MCP face off, REST chalu hai")
        return
    mcp = FastMCP(APP, host="0.0.0.0", port=port)

    @mcp.tool(name=f"{{APP}}_read")
    def read_tool(query: str = "") -> str:
        """{{APP}} se content/article padho."""
        try:
            for k in URL_VARS:
                _query[k] = query
            r = upstream_call(query or "latest")
            return r[:8000] if r else "[empty]"
        except Exception as e:
            return f"[error] {{e}}"

    @mcp.tool(name=f"{{APP}}_search")
    def search_tool(query: str) -> str:
        """{{APP}} me search karo."""
        try:
            for k in URL_VARS:
                _query[k] = query
            r = upstream_call(query)
            return r[:8000] if r else "[empty]"
        except Exception as e:
            return f"[error] {{e}}"

    @mcp.tool(name=f"{{APP}}_chat")
    def chat_tool(prompt: str) -> str:
        """{{APP}} ka AI/chat endpoint."""
        try:
            r = upstream_call(prompt)
            return r[:8000] if r else "[empty]"
        except Exception as e:
            return f"[error] {{e}}"

    mcp.run(transport="sse")


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--port", type=int, default=8000)
    _p.add_argument("--mcp-port", type=int, default=9880)
    _p.add_argument("--no-mcp", action="store_true")
    _a = _p.parse_args()
    if not _a.no_mcp:
        threading.Thread(target=run_mcp, args=(_a.mcp_port,),
                         daemon=True).start()
    print(f"[{{APP}}-api] REST :{{_a.port}} | MCP :{{_a.mcp_port}}")
    uvicorn.run(api, host="0.0.0.0", port=_a.port)
'''


def generate_readme(app, cfg, srv_path, eng_path):
    return f"""# {app} — Rev Kit app2mcp conversion

App: **{app}** (kind: `{cfg['_meta']['kind']}`)
Endpoint captured: `{cfg['method']} {cfg['upstream_url']}`

## Kya bana (3 outputs)

1. **REST API** — `{srv_path}`
   - `GET /health`, `GET /v1/models`
   - `POST /v1/chat/completions` (OpenAI-compatible, SSE streaming)
   - `POST /{app}/invoke` (raw passthrough)
   - `GET /{app}/read?query=...` (content reads)

2. **MCP server** — same file, `--mcp-port` (default 9880)
   - Tools: `{app}_read`, `{app}_search`, `{app}_chat`
   - Koi bhi AI agent (Hermes, Claude, Cursor) in tools ko call kar
     sakta hai — app ka koi official API nahi tha, ab hai.

3. **Rust engine config** — `{eng_path}`
   - proxy.git `revd` engine ka GenericFlowAdapter plug
   - `FLOW_CONFIG_DIR=re_capture/flows revd` start karo, `{app}`
     model list me aa jaata hai (sk-fabri- keys se auth).

## Run

```bash
# REST only
python3 {srv_path} --no-mcp --port 8000

# REST + MCP
python3 {srv_path} --port 8000 --mcp-port 9880
```

## Notes

- Capture apna hai (mitmdump -s mobile_re.py), session replay
  authorized personal use hai.
- Token expire hua to dobara capture karo: `python3 app2mcp.py build {app}`
  (ya naya session capture karke build again).
- Engine wale path pe streaming SSE + max_tokens + batch mode
  bhi milta hai (revd completions).
"""


# ================================================================
# 2b. MULTI-ENDPOINT BUILD (--all) — ek app, saare top endpoints,
#     ek multi-tool MCP + multi-model API. Android/any-app mode.
# ================================================================

def slug_for(url):
    """URL -> short tool-safe slug."""
    from urllib.parse import urlparse
    path = urlparse(url).path.strip("/")
    segs = [s for s in path.split("/") if s and not s.isdigit()]
    if not segs:
        q = urlparse(url).query
        segs = [q.split("=")[0]] if q else ["root"]
    base = "-".join(segs[-2 if len(segs) > 1 else 1:]).lower()
    slug = re.sub(r"[^a-z0-9\-]+", "", base.replace("_", "-")) or "root"
    return slug[:40].strip("-") or "root"


def collect_endpoints(args, limit=8):
    """Capture se top unique endpoints — chat + content mix, dedupe."""
    flows = load_flows(args.session)
    scored = []
    for fl in flows:
        for kind, scorer in (("chat", score_chat_flow),
                             ("content", score_content_flow)):
            s, reasons = scorer(fl)
            if s >= 4:
                scored.append((s, kind, reasons, fl))
    scored.sort(key=lambda x: -x[0])
    picked, seen = [], set()
    for s, kind, reasons, fl in scored:
        key = (fl.get("method", "GET").upper() + " "
               + fl.get("url", "").split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        picked.append((s, kind, reasons, fl))
        if len(picked) >= limit:
            break
    return picked


def endpoint_config(app, flow, kind, name=None):
    """Ek captured flow -> engine/adapter config (single build wale
    jitna hi, naam explicit)."""
    url = flow["url"]
    method = flow.get("method", "POST").upper()
    auth_headers = extract_auth(flow.get("req_headers", {}))
    body_tpl, _ = body_template_for(flow, kind)
    resp_paths = response_paths_for(flow, kind)
    is_sse = "event-stream" in (
        (flow.get("res_headers") or {}).get("content-type", ""))
    name = name or f"{app}-{slug_for(url)}"
    cfg = {
        "name": name,
        "upstream_url": url,
        "method": method,
        "upstream_headers": auth_headers,
        "body_template": body_tpl,
        "model_map": {name: name},
        "response_paths": resp_paths,
        "is_sse": is_sse,
        "_meta": {"kind": kind, "built_at": time.time(),
                  "source": "app2mcp-multi"},
    }
    return cfg, url_query_vars(url)


def build_all(app, args, limit=8):
    picked = collect_endpoints(args, limit)
    if not picked:
        print("[!] koi eligible endpoint nahi — app browse karke "
              "analyze chalao")
        sys.exit(1)
    print(f"[+] {len(picked)} endpoints selected (multi-mode):")
    endpoints, url_vars_all = {}, {}
    for s, kind, reasons, fl in picked:
        cfg, uv = endpoint_config(app, fl, kind)
        endpoints[cfg["name"]] = cfg
        url_vars_all[cfg["name"]] = uv
        print(f"    {s:3d} {cfg['method']:4s} {cfg['name']} <- "
              f"{fl['url'][:60]} ({', '.join(reasons[:3])})")

    # 1. engine config — apps array (GenericFlowAdapter multi-app form)
    os.makedirs(FLOWS_DIR, exist_ok=True)
    eng_path = os.path.join(FLOWS_DIR, f"{app}.config.json")
    with open(eng_path, "w", encoding="utf-8") as f:
        json.dump({"apps": list(endpoints.values())}, f, indent=2,
                  ensure_ascii=False)
    print(f"[+] engine config (multi-app) -> {eng_path}")
    print("    (revd me har endpoint = models list ka alag model)")

    # 2. multi REST + MCP server
    os.makedirs(OUT_DIR, exist_ok=True)
    srv = generate_server_multi(app, endpoints, url_vars_all)
    srv_path = os.path.join(OUT_DIR, f"{app}_server.py")
    with open(srv_path, "w", encoding="utf-8") as f:
        f.write(srv)
    print(f"[+] multi REST + MCP server -> {srv_path}")

    # 3. README
    doc = generate_readme_multi(app, endpoints, srv_path, eng_path)
    with open(os.path.join(OUT_DIR, f"{app}_README.md"), "w",
              encoding="utf-8") as f:
        f.write(doc)
    print(f"[+] docs -> {os.path.join(OUT_DIR, f'{app}_README.md')}")
    print(f"\n[done] {app}: {len(endpoints)} endpoints -> "
          f"{len(endpoints)} MCP tools + {len(endpoints)} API models")


def generate_readme_multi(app, endpoints, srv_path, eng_path):
    lines = [f"# {app} — Rev Kit app2mcp --all (multi-endpoint)", "",
             f"{len(endpoints)} captured endpoints, ek hi server:", ""]
    for n, c in endpoints.items():
        lines.append(f"- `{n}` — {c['method']} "
                     f"{c['upstream_url'][:90]} "
                     f"({'SSE' if c['is_sse'] else 'JSON'})")
    lines += [
        "", "## Outputs", "",
        f"1. **REST API** — `{srv_path}`",
        "   - `GET /endpoints`, `GET /read?endpoint=<name>&query=...`",
        "   - `POST /invoke` `{\"endpoint\": ..., \"query\": ...}`",
        "   - `POST /v1/chat/completions` (model = endpoint name, "
        "SSE streaming, max_tokens + batch supported)",
        f"2. **MCP server** — same file, `--mcp-port` (default 9880)",
        "   - Tools: " + ", ".join(f"`{app}_{n}_read`"
                                  for n in endpoints),
        f"3. **Engine config** — `{eng_path}`",
        "   - apps-array form — har endpoint `revd` models list me "
        "alag model ban jaata hai.",
        "", "## Run", "", "```bash",
        f"python3 {srv_path} --port 8000 --mcp-port 9880", "```", "",
        "Token expire ho jaye → dobara capture "
        "(`mitmdump -s mobile_re.py`) + `build <app> --all`.", ""]
    return "\n".join(lines)


def generate_server_multi(app, endpoints, url_vars_all):
    """Multi-endpoint server: har captured endpoint ka tool + model."""
    u = json.dumps({n: c["upstream_url"] for n, c in endpoints.items()},
                   indent=4)
    m = json.dumps({n: c["method"] for n, c in endpoints.items()},
                   indent=4)
    h = json.dumps({n: c["upstream_headers"] for n, c in
                    endpoints.items()}, indent=4)
    b = json.dumps({n: c["body_template"] for n, c in endpoints.items()},
                   indent=4)
    p = json.dumps({n: c["response_paths"] for n, c in
                    endpoints.items()}, indent=4)
    s = json.dumps({n: "True" if c["is_sse"] else "False"
                    for n, c in endpoints.items()}, indent=4)
    uv = json.dumps(url_vars_all, indent=4)

    return f'''#!/usr/bin/env python3
"""
AUTO-GENERATED by Rev Kit app2mcp --all — app: {app}
====================================================
Multi-endpoint: capture ke top endpoints, ek hi server me.

REST:
    GET  /health | /endpoints | /v1/models
    GET  /read?endpoint=<name>&query=...
    POST /invoke {{"endpoint": ..., "query": ...}}
    POST /v1/chat/completions   (model = endpoint name, SSE streaming)

MCP (--mcp-port, default 9880):
    tools: {app}_<endpoint>_read (har endpoint ka ek tool)

Authorized personal use only — captured session apna hai.
"""

import json
import threading
import time
import uuid

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

APP = {json.dumps(app)}
EP_URL = {u}
EP_METHOD = {m}
EP_HEADERS = {h}
EP_BODY = {b}
EP_PATHS = {p}
EP_SSE = {s}
EP_URLVARS = {uv}

api = FastAPI(title=f"{{APP}} multi-endpoint API (Rev Kit app2mcp)")
_lock = threading.Lock()
_query = {{}}  # endpoint -> {{param: override}}


def extract_text(data, paths):
    if isinstance(data, str):
        return data
    for p in paths:
        try:
            v = data
            for part in p.strip("/").split("/"):
                if isinstance(v, list):
                    v = v[int(part)] if part.isdigit() \\
                        and int(part) < len(v) else v
                elif isinstance(v, dict):
                    v = v.get(part)
                else:
                    break
            if isinstance(v, str) and v.strip():
                return v
        except Exception:
            continue
    return ""


def render_body(name, query, model):
    body = json.loads(json.dumps(EP_BODY[name]))
    def walk(v):
        if isinstance(v, str):
            return (v.replace("${{MESSAGES}}", query)
                     .replace("${{QUERY}}", query)
                     .replace("${{MODEL}}", model or name))
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, dict):
            return {{k2: walk(x) for k2, x in v.items()}}
        return v
    return walk(body)


def render_url(name):
    url = EP_URL[name]
    for k, v in EP_URLVARS.get(name, {{}}).items():
        url = url.replace(v, str(_query.get(name, {{}}).get(k, v)))
    return url


def upstream_call(name, query, model=None, stream_cb=None,
                  raw_body=None):
    with _lock:
        try:
            body = raw_body or render_body(name, query, model)
            r = requests.request(
                EP_METHOD[name], render_url(name),
                headers=EP_HEADERS[name],
                json=body if isinstance(body, dict) else None,
                data=None if isinstance(body, dict) else body,
                stream=EP_SSE[name], timeout=180)
            if r.status_code != 200:
                raise RuntimeError(f"upstream HTTP {{r.status_code}}")
            if (EP_SSE[name] and stream_cb
                    and "event-stream" in r.headers.get(
                        "content-type", "")):
                out = []
                for line in r.iter_lines():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "ignore")
                    line = (line or "").strip()
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d in ("[DONE]", ""):
                        continue
                    try:
                        piece = extract_text(json.loads(d),
                                             EP_PATHS[name])
                    except Exception:
                        piece = ""
                    if piece:
                        out.append(piece)
                        stream_cb(piece)
                return "".join(out)
            text = r.text
            try:
                return extract_text(json.loads(text), EP_PATHS[name])
            except Exception:
                return text
        finally:
            pass


def render_prompt_msgs(msgs):
    parts = []
    for mm in msgs:
        r = mm.get("role", "user")
        if r == "system":
            parts.append(f"[Instructions]: {{mm.get('content', '')}}")
        elif r == "assistant":
            parts.append(f"[Previous reply]: {{mm.get('content', '')}}")
        else:
            parts.append(f"[User]: {{mm.get('content', '')}}")
    parts.append("Answer the LAST [User] message directly.")
    return "\\n\\n".join(parts)


def sse_frame(model, delta, finish=None):
    return ("data: " + json.dumps({{
        "id": f"chatcmpl-{{uuid.uuid4().hex[:12]}}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{{"index": 0, "delta": delta,
                    "finish_reason": finish}}],
    }}) + "\\n\\n")


@api.get("/health")
def health():
    return {{"app": APP, "endpoints": list(EP_URL), "ok": True}}


@api.get("/endpoints")
def endpoints_route():
    return {{"app": APP,
             "endpoints": [{{"name": n, "method": EP_METHOD[n],
                            "url": EP_URL[n][:120],
                            "kind": "sse" if EP_SSE[n] else "json"}}
                          for n in EP_URL]}}


@api.get("/v1/models")
def models():
    return {{"object": "list", "data": [
        {{"id": n, "object": "model", "owned_by": "rev-app2mcp"}}
        for n in EP_URL]}}


@api.get("/read")
def read(endpoint: str = "", query: str = ""):
    name = endpoint if endpoint in EP_URL else next(iter(EP_URL))
    _query[name] = {{k: query for k in EP_URLVARS.get(name, {{}})}}
    result = upstream_call(name, query or "latest")
    return {{"app": APP, "endpoint": name, "query": query,
            "content": result}}


@api.post("/invoke")
async def invoke(req: Request):
    bd = json.loads(await req.body())
    name = bd.get("endpoint") or next(iter(EP_URL))
    if name not in EP_URL:
        return {{"error": f"unknown endpoint {{name}}"}}
    query = bd.get("query") or bd.get("prompt") or bd.get("q") or ""
    _query[name] = {{k: query for k in EP_URLVARS.get(name, {{}})}}
    result = upstream_call(name, bd.get("model"),
                           raw_body=bd.get("raw_body"))
    return {{"app": APP, "endpoint": name, "result": result}}


@api.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = json.loads(await req.body())
    model = body.get("model", "")
    name = model if model in EP_URL else next(iter(EP_URL))
    msgs = body.get("messages", [])
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        prompt = msgs[0].get("content", "")
    else:
        prompt = render_prompt_msgs(msgs)
    # token-efficiency: kam requests, max output (engine behaviour)
    max_tokens = body.get("max_tokens") or 0
    if max_tokens:
        prompt += ("\\n\\n[Output budget: complete, thorough answer "
                   f"of roughly {{max_tokens}} tokens. "
                   "Do not stop early.]")
    batch = body.get("batch") or []
    if batch:
        numbered = "\\n".join(f"{{i + 1}}. {{s}}"
                             for i, s in enumerate(batch))
        prompt += ("\\n\\n[BATCH MODE — answer EVERY item in order, "
                   "separated by === <number> === headers.]\\n"
                   f"{{numbered}}")
    _query[name] = {{k: prompt for k in EP_URLVARS.get(name, {{}})}}

    if body.get("stream"):
        def gen():
            buf, done = [], []
            def cb(p):
                buf.append(p)
            def run():
                try:
                    upstream_call(name, prompt, name, cb)
                finally:
                    done.append(True)
            threading.Thread(target=run, daemon=True).start()
            sent = 0
            deadline = time.time() + 180
            while time.time() < deadline:
                if sent < len(buf):
                    for p in buf[sent:]:
                        yield sse_frame(name, {{"content": p}})
                    sent = len(buf)
                if done and sent >= len(buf):
                    yield sse_frame(name, {{}}, finish="stop")
                    yield "data: [DONE]\\n\\n"
                    return
                time.sleep(0.05)
        return StreamingResponse(gen(),
                                 media_type="text/event-stream")

    result = upstream_call(name, prompt, name)
    return {{
        "id": f"chatcmpl-{{uuid.uuid4().hex[:12]}}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": name,
        "choices": [{{"index": 0,
                     "message": {{"role": "assistant",
                                 "content": result}},
                     "finish_reason": "stop"}}],
        "usage": {{"prompt_tokens": len(prompt) // 4,
                  "completion_tokens": len(result) // 4,
                  "total_tokens": (len(prompt) + len(result)) // 4}},
    }}


def run_mcp(port):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("[!] pip install mcp — MCP face off, REST chalu hai")
        return
    mcp = FastMCP(f"{{APP}}-app2mcp", host="0.0.0.0", port=port)

    def make_read(ep):
        def read_tool(query: str = "") -> str:
            try:
                _query[ep] = {{k: query
                              for k in EP_URLVARS.get(ep, {{}})}}
                r = upstream_call(ep, query or "latest")
                return r[:8000] if r else "[empty]"
            except Exception as e:
                return f"[error] {{e}}"
        read_tool.__doc__ = f"{{ep}} endpoint ({{APP}}) se content padho."
        mcp.tool(name=f"{{APP}}_{{ep}}_read")(read_tool)

    for ep in EP_URL:
        make_read(ep)
    mcp.run(transport="sse")


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--port", type=int, default=8000)
    _p.add_argument("--mcp-port", type=int, default=9880)
    _p.add_argument("--no-mcp", action="store_true")
    _a = _p.parse_args()
    if not _a.no_mcp:
        threading.Thread(target=run_mcp, args=(_a.mcp_port,),
                         daemon=True).start()
    print(f"[{{APP}}-multi] REST :{{_a.port}} | MCP :{{_a.mcp_port}} "
          f"| endpoints: {{len(EP_URL)}}")
    uvicorn.run(api, host="0.0.0.0", port=_a.port)
'''


ANDROID_GUIDE = """
================================================================
 Rev Kit app2mcp — Android App Capture Quickstart
================================================================
Koi bhi Android app (articles/feed/news/chat — jiska koi official
API/MCP nahi) ko MCP + API me convert karo:

1) Proxy start karo (laptop pe):
     cd ~/Rev && mitmdump -s mobile_re.py
   -> re_capture/session.jsonl me flows likhe jaayenge

2) Phone WiFi proxy -> <laptop-ip>:8080

3) CA cert: phone browser me http://mitm.it kholo -> install
   (Android 7+ apps user certs trust nahi karte — isliye step 4)

4) Pinned/HTTPS app traffic ke liye Frida unpin:
     frida -U -f com.target.app -l ssl_unpin.js --no-pause
   (ssl_unpin.js: OkHttp3 + TrustManagerImpl + SSLContext +
    WebView. Flutter apps: reFlutter ya apktool
    network_security_config patch)

5) App kholo, browse karo — articles padho, search karo, chat karo

6) Endpoints nikalo:
     python3 app2mcp.py analyze

7) Multi-endpoint build (saare top endpoints, ek multi-tool MCP):
     python3 app2mcp.py build <appname> --all

8) Serve:
     python3 app2mcp.py serve <appname> --port 8000 --mcp-port 9880

Frida check: frida-ps -U | grep <package>
Web apps bhi same pipeline: browser proxy -> capture -> analyze
-> build --all. Desktop apps: system proxy point karo mitmdump pe.
"""


def cmd_android(_):
    print(ANDROID_GUIDE)
    print("Files: mobile_re.py (mitmproxy addon), ssl_unpin.js (Frida), "
          "capture_flow.py (browser-side), app2mcp.py (ye tool)")
    print("Python venv: ~/Rev/venv (mitmproxy + frida tools installed)")


def cmd_list(_):
    if not os.path.isdir(OUT_DIR):
        print("[!] koi converted app nahi")
        return
    for f in sorted(os.listdir(OUT_DIR)):
        if f.endswith("_server.py"):
            app = f[:-len("_server.py")]
            eng = os.path.join(FLOWS_DIR, f"{app}.config.json")
            print(f"  {app:20s} server=app2mcp_servers/{f} "
                  f"engine={'yes' if os.path.exists(eng) else 'no'}")


def cmd_serve(args):
    app = args.app
    srv = os.path.join(OUT_DIR, f"{app}_server.py")
    if not os.path.exists(srv):
        print(f"[!] {srv} nahi — pehle: build {app}")
        sys.exit(1)
    cmd = [sys.executable, srv]
    if args.port:
        cmd += ["--port", str(args.port)]
    if args.mcp_port:
        cmd += ["--mcp-port", str(args.mcp_port)]
    os.execvp(sys.executable, cmd)


def main():
    p = argparse.ArgumentParser(
        description="Rev Kit app2mcp — koi bhi app ko MCP+API me convert")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="capture se endpoints nikaalo")
    a.add_argument("--session", default=None)
    a.set_defaults(fn=cmd_analyze)

    b = sub.add_parser("build", help="app ka API+MCP+engine config banao")
    b.add_argument("app", help="app ka naam (jo tum doge)")
    b.add_argument("--endpoint", default=None,
                   help='"METHOD url-substring" force pick')
    b.add_argument("--all", dest="all_eps", action="store_true",
                   help="multi-endpoint: top endpoints, ek multi-tool "
                        "server (Android/any-app mode)")
    b.add_argument("--session", default=None)
    b.set_defaults(fn=cmd_build)

    ag = sub.add_parser("android", help="phone capture quickstart guide")
    ag.set_defaults(fn=cmd_android)

    l = sub.add_parser("list", help="converted apps dikhaao")
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("serve", help="converted app serve karo")
    s.add_argument("app")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--mcp-port", type=int, default=0)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

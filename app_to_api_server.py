"""
App -> OpenAI-Compatible API Adapter Server
============================================
Kisi bhi mobile/web app ke captured internal API ko standard
OpenAI-compatible API me convert karta hai:
    POST /v1/chat/completions   (streaming supported)
    GET  /v1/models
    Tool calling SHIM (function defs prompt me inject hoti hain)

Setup:
    1. mobile_re.py se Qwen app ka traffic capture karo
    2. Neeche CONFIG section me apne captured values bharo
       (endpoint URL, headers, request body template)
    3. pip install fastapi uvicorn && python app_to_api_server.py

Authorized personal use only.
"""

import json
import os
import re
import time
import uuid
from typing import Optional

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# ================================================================
# CONFIG LOADING — auto_pipeline.py se config.json aayega toh wo use
# hoga, warna neeche ke manual defaults.
# ================================================================

CONFIG = {
    # App ka internal chat endpoint (capture se milega)
    "upstream_url": "https://chat.qwen.ai/api/chat/completions",

    # Capture hue headers — token.txt se copy karo
    "upstream_headers": {
        "Authorization": "Bearer <APNA_TOKEN_YAHAN>",
        "Content-Type": "application/json",
        "User-Agent": "<APP_KA_USER_AGENT_CAPTURE_SE>",
    },

    # Upstream body template — ${VARIABLE} placeholders support
    "body_template": {
        "model": "${MODEL}",
        "messages": "${MESSAGES}",
        "stream": True,
    },

    # Model name mapping: jo naam tum dena chahte ho -> app ka internal id
    "model_map": {
        "qwen": "qwen-max-latest",
        "qwen-turbo": "qwen-turbo-latest",
    },
}

# Auto-generated config override
if os.path.exists("config.json"):
    with open("config.json") as _f:
        _auto = json.load(_f)
    for _k in ("upstream_url", "upstream_headers", "body_template", "model_map"):
        if _auto.get(_k):
            CONFIG[_k] = _auto[_k]
    _meta = _auto.get("_meta", {})
    print(f"[*] config.json loaded (confidence: {_meta.get('confidence_score')}, "
          f"captured: {_meta.get('selected_at')})")
else:
    print("[!] config.json nahi mila — manual defaults use ho rahe hain.")
    print("    Run: python auto_pipeline.py  (capture ke baad)")

app = FastAPI(title="App->OpenAI Adapter")


# ================================================================
# TOOL CALLING SHIM
# ================================================================

def inject_tools_into_messages(messages, tools):
    """
    Agar upstream native tool-calling support nahi karta toh
    functions ko system prompt me serialize kar dete hain.
    Response me model structured JSON dalega, hum parse karke
    OpenAI-format 'tool_calls' me convert karenge.
    """
    if not tools:
        return messages, False

    tool_descriptions = []
    for t in tools:
        fn = t.get("function", {})
        tool_descriptions.append(
            f"- {fn.get('name')}: {fn.get('description', '')}\n"
            f"  Parameters: {json.dumps(fn.get('parameters', {}))}"
        )

    tool_system = (
        "\n\nYou have access to these tools:\n"
        + "\n".join(tool_descriptions)
        + "\n\nWhen you need to call a tool, respond ONLY with JSON:\n"
          '{"tool_call": {"name": "<tool_name>", "arguments": {...}}}\n'
          "Do NOT add any other text when calling a tool."
    )

    new_msgs = list(messages)
    if new_msgs and new_msgs[0].get("role") == "system":
        new_msgs[0]["content"] += tool_system
    else:
        new_msgs.insert(0, {"role": "system", "content": tool_system.strip()})
    return new_msgs, True


def parse_tool_call(text):
    """Model ke response se tool call JSON nikalo"""
    try:
        m = re.search(r'\{[\s\S]*"tool_call"[\s\S]*\}', text)
        if m:
            data = json.loads(m.group(0))
            tc = data["tool_call"]
            return {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc.get("arguments", {})),
                },
            }
    except Exception:
        pass
    return None


# ================================================================
# UPSTREAM CALL BUILDER
# ================================================================

def build_upstream_payload(openai_body):
    """OpenAI-format body -> app ke internal format me translate"""
    msgs, tools_shimmed = inject_tools_into_messages(
        openai_body.get("messages", []), openai_body.get("tools"))

    payload = {}
    for k, v in CONFIG["body_template"].items():
        if v == "${MESSAGES}":
            payload[k] = msgs
        elif v == "${MODEL}":
            requested = openai_body.get("model", "qwen")
            payload[k] = CONFIG["model_map"].get(requested, requested)
        elif v == "${TIMESTAMP}":
            payload[k] = int(time.time())
        elif isinstance(v, str) and v.startswith("${"):
            key = v.strip("${}")
            if key.lower() == "stream":
                payload[k] = bool(openai_body.get("stream", False))
            elif key.upper() == "MESSAGES":
                payload[k] = msgs
            else:
                payload[k] = openai_body.get(key.lower())
        else:
            payload[k] = v
    # stream flag hamesha sync rakho agar template me hai
    if "stream" in payload:
        payload["stream"] = bool(openai_body.get("stream", False))
    # kuch upstreams extra fields reject karti hain — safe defaults
    payload.setdefault("stream", bool(openai_body.get("stream", False)))
    return payload


def sse_openai_chunk(model, delta_content=None, finish=None, role=None):
    """Standard OpenAI SSE chunk format"""
    delta = {}
    if role:
        delta["role"] = role
    elif delta_content is not None:
        delta["content"] = delta_content
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


# ================================================================
# ENDPOINTS
# ================================================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    is_stream = body.get("stream", False)
    requested_model = body.get("model", "qwen")

    payload = build_upstream_payload(body)

    if not is_stream:
        try:
            r = requests.post(CONFIG["upstream_url"],
                              headers=CONFIG["upstream_headers"],
                              json=payload, timeout=180)
        except requests.RequestException as e:
            return JSONResponse({"error": f"upstream unreachable: {e}"},
                                status_code=502)
        # token expire handling — watchdog is flag ko dekhta hai
        if r.status_code in (401, 403):
            os.makedirs("re_capture", exist_ok=True)
            with open("re_capture/token_expired.flag", "w") as _f:
                _f.write(f"{int(time.time())} {r.status_code}\n")
            return JSONResponse(
                {"error": {"message": "upstream auth expired — token refresh "
                                       "chahiye (start.sh --refresh chalao)",
                           "type": "auth_expired",
                           "code": r.status_code}},
                status_code=401)
        r.raise_for_status()
        try:
            up = r.json()
            # Common upstream formats handle karo — apne capture ke hisaab se adjust
            content = None
            choices = up.get("choices")
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content") or msg.get("text")
            if content is None:
                content = up.get("output", {}).get("text") or json.dumps(up)[:500]

            tool_call = parse_tool_call(content) if body.get("tools") else None
            message = {"role": "assistant"}
            if tool_call:
                message["content"] = None
                message["tool_calls"] = [tool_call]
            else:
                message["content"] = content

            return JSONResponse({
                "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if tool_call else "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0},
            })
        except Exception:
            return JSONResponse({"error": r.text}, status_code=502)

    # ---- streaming mode ----
    def stream_gen():
        buffer = ""
        with requests.post(CONFIG["upstream_url"],
                           headers=CONFIG["upstream_headers"],
                           json=payload, stream=True, timeout=300) as r:
            if r.status_code in (401, 403):
                os.makedirs("re_capture", exist_ok=True)
                with open("re_capture/token_expired.flag", "w") as _f:
                    _f.write(f"{int(time.time())} {r.status_code}\n")
                err_chunk = {"error": {"message": "upstream auth expired",
                                       "type": "auth_expired",
                                       "code": r.status_code}}
                yield f"data: {json.dumps(err_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return
            yield sse_openai_chunk(requested_model, role="assistant")
            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode(errors="replace")
                # App SSE format: 'data: {...}' — apne capture ke hisaab se adjust
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        d = json.loads(raw)
                        # common shapes: choices[0].delta.content | .text
                        piece = None
                        ch = (d.get("choices") or [{}])[0]
                        piece = (ch.get("delta", {}).get("content")
                                 or ch.get("delta", {}).get("reasoning_content"))
                        if piece is None:
                            piece = d.get("output", {}).get("text")
                        if piece:
                            buffer += piece
                            yield sse_openai_chunk(requested_model, piece)
                    except json.JSONDecodeError:
                        continue

        # Stream ke end me tool-call check
        if body.get("tools"):
            tc = parse_tool_call(buffer)
            if tc:
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": requested_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [tc]},
                        "finish_reason": "tool_calls"}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
        yield sse_openai_chunk(requested_model, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_gen(),
                             media_type="text/event-stream")


@app.get("/v1/models")
async def models():
    # live passthrough — chat.qwen.ai/api/models bina auth khulta hai
    try:
        r = requests.get("https://chat.qwen.ai/api/models",
                         headers={"User-Agent": CONFIG["upstream_headers"].get(
                             "User-Agent", "Mozilla/5.0")}, timeout=10)
        live = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        live = []
    ids = list(CONFIG["model_map"].keys())
    for m in live:
        if m not in ids:
            ids.append(m)
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "qwen-adapter"}
                 for m in ids],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    print("=" * 56)
    print(" App->OpenAI Adapter Server")
    print(f"   upstream : {CONFIG['upstream_url']}")
    print(f"   listening: http://localhost:8001/v1")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8001)

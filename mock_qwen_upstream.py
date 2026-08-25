"""
Mock Qwen Upstream Server (testing only)
=========================================
chat.qwen.ai ke internal API ka replica — poori pipeline ko
end-to-end verify karne ke liye bina real account/token ke.

Behavior:
  - Authorization header missing/galat -> 401 (watchdog test ke liye)
  - stream=true  -> SSE chunks (OpenAI delta style)
  - stream=false -> single JSON response
  - Prompt me 'weather' ho -> tool_call JSON (shim parse test)

Usage:
    python mock_qwen_upstream.py          # port 9999
"""

import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

VALID_TOKEN = "TESTTOKEN_valid_abc123"

app = FastAPI(title="Mock Qwen Upstream")


def _auth_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    cookie = request.headers.get("cookie", "")
    token = request.headers.get("x-auth-token", "")
    return VALID_TOKEN in auth or VALID_TOKEN in cookie or VALID_TOKEN == token


@app.post("/api/chat/completions")
async def completions(request: Request):
    if not _auth_ok(request):
        return JSONResponse(
            {"code": "401", "message": "Invalid or expired token",
             "error": "Unauthorized"},
            status_code=401,
        )

    body = await request.json()
    is_stream = bool(body.get("stream", False))
    model = body.get("model", "qwen-max-latest")
    user_text = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            user_text = str(m.get("content", ""))
            break

    wants_tool = "weather" in user_text.lower()

    def make_reply_text():
        if wants_tool:
            return json.dumps({
                "tool_call": {
                    "name": "get_weather",
                    "arguments": {"city": "Delhi"},
                }
            })
        return f"Mock reply: tumne kaha '{user_text}'. Sab kaam kar raha hai."

    if not is_stream:
        content = make_reply_text()
        finish = "tool_calls" if wants_tool else "stop"
        # Real Qwen OpenAI-style JSON deta hai
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
                "logprobs": None,
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 20,
                      "total_tokens": 32},
        })

    # ---- streaming SSE ----
    async def sse():
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        text = make_reply_text()
        # chhote pieces me stream karo jaise real server karta hai
        piece = ""
        for i in range(0, len(text), 7):
            piece = text[i:i + 7]
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": piece},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await _sleep()
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {},
                         "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    import asyncio
    async def _sleep():
        await asyncio.sleep(0.02)

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"status": "mock-ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9999)

"""
Synthetic capture generator — mobile_re.py ke output format me
fake Qwen traffic banata hai taki pura pipeline bina phone/login
ke test ho sake.

Output: re_capture/session.jsonl (+ endpoints.json + tokens.txt)
"""

import json
import os

os.makedirs("re_capture", exist_ok=True)

BASE = "http://127.0.0.1:9999"          # mock upstream
CHAT_URL = f"{BASE}/api/chat/completions"
TOKEN = "Bearer TESTTOKEN_valid_abc123"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

chat_headers = {
    "Host": "127.0.0.1:9999",
    "Authorization": TOKEN,
    "Content-Type": "application/json",
    "User-Agent": UA,
    "Accept": "text/event-stream",
}

noise = [
    {
        "ts": "2026-08-24T14:00:01",
        "method": "GET",
        "url": f"{BASE}/static/app.js",
        "status": 200,
        "req_headers": {"User-Agent": UA},
        "req_body": "",
        "res_headers": {"content-type": "application/javascript"},
        "res_body": "console.log('bundle');",
    },
    {
        "ts": "2026-08-24T14:00:02",
        "method": "GET",
        "url": f"{BASE}/api/models/list",
        "status": 200,
        "req_headers": dict(chat_headers),
        "req_body": "",
        "res_headers": {"content-type": "application/json"},
        "res_body": json.dumps({"models": ["qwen-max-latest", "qwen-turbo-latest"]}),
    },
]

chat_flow = {
    "ts": "2026-08-24T14:00:05",
    "method": "POST",
    "url": CHAT_URL,
    "status": 200,
    "req_headers": chat_headers,
    "req_body": json.dumps({
        "model": "qwen-max-latest",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
        ],
        "stream": True,
        "chat_id": "c-8f3a2b",
    }),
    "res_headers": {"content-type": "text/event-stream"},
    "res_body": (
        'data: {"choices":[{"delta":{"content":"Mock"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" reply"}}]}\n\n'
        "data: [DONE]\n\n"
    ),
}

with open("re_capture/session.jsonl", "w") as f:
    for rec in noise + [chat_flow]:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

# endpoints.json jaisa mobile_re.py banata hai
endpoints = {
    "GET 127.0.0.1/static/app.js": {"count": 1, "statuses": [200], "params": []},
    "GET 127.0.0.1/api/models/list": {"count": 1, "statuses": [200], "params": []},
    "POST 127.0.0.1/api/chat/completions": {"count": 1, "statuses": [200],
                                             "params": []},
}
with open("re_capture/endpoints.json", "w") as f:
    json.dump(endpoints, f, indent=2)

with open("re_capture/tokens.txt", "w") as f:
    f.write(f"\n[14:00:05] POST {CHAT_URL}\n")
    f.write(f"  req.Authorization = {TOKEN}\n")

print("[+] synthetic capture written -> re_capture/")

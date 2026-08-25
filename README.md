# RE Toolkit — Universal MITM Bridge (Qwen + Notion + Figma as OpenAI API)

Mobile app reverse engineering + LLM API forensics + **universal app-AI bridge**.

## QUICK START — Universal Server (M2M)

```bash
# 1. Setup (ek baar):
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt openai curl_cffi
./venv/bin/python -m playwright install chromium

# 2. Qwen login (ek baar — persistent profile):
./start.sh --login

# 3. Universal server (qwen + notion + figma):
./start.sh --universal
```

```python
from openai import OpenAI
# LAN pe koi bhi device (M2M):
client = OpenAI(base_url="http://<kali-ip>:8000/v1", api_key="m2m-key")
r = client.chat.completions.create(
    model="qwen",   # qwen | notion | figma
    messages=[{"role": "user", "content": "hello"}],
)
print(r.choices[0].message.content)
```

## Architecture

```
Koi bhi device (phone/laptop/PC)
    |
    v
http://<ip>:8000/v1  {model: "qwen"|"notion"|"figma"}  [Bearer m2m-key]
    |
[Universal Router]  — model se connector
    |--- QwenConnector   : in-page fetch + SSE intercept (WAF-proof)
    |--- NotionConnector : pure HTTP (loginWithEmail API + token_v2)
    |--- FigmaConnector  : capture-based replay
    |
Har connector: persistent profile -> login EK baar -> 0% interaction
Response packets NETWORK LAYER se intercept (DOM scraping nahi)
```

## Notion (Pure HTTP — ZERO browser)

```bash
./venv/bin/python notion_pure.py --email <email> --pass <password>
# loginWithEmail API -> token_v2 -> AI endpoint probe -> live test
# Notion AI internally Claude use karta hai — effectively Claude-as-API
```

## Naya App Add Karna (Burp-style capture)

```bash
./start.sh --login-app <app>    # ek baar login (visible browser)
./start.sh --capture <app>      # AI feature manually use karo —
                                # tool POST/GET/headers/body sab capture karega
./start.sh --universal          # model="<app>" ready
```

Capture tool AI-hint wale requests filter karta hai (chat/completion/
generate/stream), list dikhata hai, tu endpoint choose karta hai —
`connectors/<app>_flow.json` ban jata hai.

## Files

| File | Kaam |
|------|------|
| `universal_server.py` | EK server — model routing + API key + M2M (LAN) |
| `universal_bridge.py` | Connector framework + Qwen/Notion/Figma |
| `capture_flow.py` | Burp-style AI endpoint capture (kisi bhi app) |
| `notion_pure.py` | Notion pure-HTTP client (zero browser) |
| `qwen_browser_bridge.py` | Qwen-only bridge (MITM fetch + SSE intercept) |
| `login_auto.py` | Qwen auto-login (creds se, persistent profile) |
| `app_to_api_server.py` | HTTP replay adapter (captured token se) |
| `mobile_re.py` | mitmproxy addon — capture, endpoint map, token hunting |
| `auto_pipeline.py` | Captured flows -> config.json |
| `flow_to_api.py` | Captured flow -> replayable Python client |
| `qwen_token_harvest.py` | localStorage/network se Bearer token |
| `ssl_unpin.js` | Frida universal SSL pinning bypass |
| `api_forensics_v2.py` | LLM API forensics — spoof detection, tokenizer fingerprint |
| `mock_qwen_upstream.py` | Test-only mock (self-test ke liye) |
| `run_full_test.py` | 8-point self-test chain |
| `start.sh` | Master control |

## Real Qwen API Surface (probed 2026-08)

- Models (no auth): `GET /api/models` -> `qwen3.7-plus`, `qwen3.8-max`
- Guest flow: `/api/v2/chats/new` -> `/api/v2/chat/completions?chat_id=` (SSE)
- Auth: cookie-based (web), Bearer JWT (localStorage)
- **Aliyun WAF**: completions pe JS-generated x5sec maangta hai —
  pure HTTP replay impossible (RGV587 punish). Isliye in-page fetch
  + network-layer intercept = reliable path.

## Commands

```
./start.sh --universal    EK server: qwen+notion+figma (M2M)
./start.sh --capture      app ka AI endpoint capture
./start.sh --login-app    connector login
./start.sh --login        qwen login
./start.sh --serve        qwen-only server
./start.sh --serve-http   HTTP replay mode
./start.sh --mitm         phone/Burp capture mode
./start.sh --local        offline GGUF backup
./start.sh --test         self-test (8 checks)
./start.sh --status       status
```

## SECURITY

`config.json`, `browser_profile/`, `connectors/` me **live tokens/cookies**
hote hain — `.gitignore` me sab blocked hai. Kabhi manually add mat karna.

## Authorized testing only.

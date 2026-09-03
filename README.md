# Rev: A Machine-to-Machine Bridge for Unified AI Model Access

**Abstract.** We present *Rev*, a self-hosted machine-to-machine (M2M) bridge that exposes multiple consumer AI applications (Qwen, Notion AI, DeepSeek) through a single OpenAI-compatible API. Rev employs man-in-the-middle (MITM) traffic capture to reverse-engineer proprietary web chat protocols, then replays them via pure HTTP connectors. The system features an OAuth-inspired token management layer, a CLI interface, and a self-hosted Firecrawl instance for web intelligence.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Rev M2M Bridge                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐    ┌──────────────┐    ┌─────────────────────┐   │
│  │  Rev CLI │───▶│ Token Store  │───▶│  Provider Connector │   │
│  │  (rev)   │    │ (OAuth-like) │    │  (pure HTTP)        │   │
│  └──────────┘    └──────────────┘    └──────────┬──────────┘   │
│                                                  │               │
│  ┌──────────────┐    ┌──────────────┐           │               │
│  │  Universal   │◀───│  Firecrawl   │           │               │
│  │  Server      │    │  (self-host) │           │               │
│  │  :8000/v1    │    │  :3002       │           │               │
│  └──────────────┘    └──────────────┘           │               │
│                                                  │               │
└──────────────────────────────────────────────────┼───────────────┘
                                                   │
                    ┌──────────────────────────────┼──────────────┐
                    │         Upstream AI Providers              │
                    ├──────────────┬───────────────┼──────────────┤
                    │  chat.qwen.ai│ notion.so/api │chat.deepseek │
                    │  (SSE)       │ (NDJSON)      │(SSE + PoW)   │
                    └──────────────┴───────────────┴──────────────┘
```

## 2. Threat Model & Methodology

### 2.1 Traffic Capture (MITM)

**revkit CLI (naya, Sep 2026)** — Rev Kit ka MITM ab ek command:

```bash
python3 revkit.py map https://www.netflix.com --watch 30   # auto 30s
python3 revkit.py map https://app.example.com --headed     # login + manual use
python3 revkit.py report captures/netflix_com_map.json     # intent report
python3 revkit.py endpoints captures/netflix_com_map.json  # sirf URLs
```

URL do → stealth Chromium (persistent profile — login yaad rehta hai)
→ **saare HTTPS requests capture** (headers, body, response, SSE bhi)
→ **endpoint inventory** (dedupe, asset/CDN noise filter) → **user-intent
classification** (login/search/play/graphql/generate-ai/purchase/wall-captcha
signatures se score) → JSON map + human report. "User ne konsa manga?"
ka jawab intent summary me — top intents star ke saath.

Live-proof (Netflix, Sep 2026): 50 reqs -> 42 unique endpoints,
`POST /graphql` 9-star (MembershipStatus persisted-query), reCAPTCHA
Enterprise anchor wall detect (sitekey extract), assets auto-hidden.
MITM capture wall bhi bata deta hai — kaunsa endpoint captcha hai.

Rev captures API traffic using browser-level network interception:

| Method | Tool | Use Case |
|--------|------|----------|
| CDP Interception | CloakBrowser + Playwright | Initial capture, WAF bypass |
| Proxy MITM | Burp Suite / mitmproxy | Production capture, request modification |
| Pure HTTP Replay | curl_cffi (TLS impersonation) | Runtime connector (no browser) |

### 2.2 Authentication Flow

```
┌─────────┐     ┌─────────────┐     ┌──────────────┐
│  User   │────▶│  Rev CLI    │────▶│  Provider    │
│         │     │  (login)    │     │  (upstream)  │
└─────────┘     └──────┬──────┘     └──────┬───────┘
                       │                    │
                       │  1. Credentials    │
                       │  (email/pass/      │
                       │   cookies/OAuth)   │
                       │                    │
                       │  2. Token Exchange │
                       │◀───────────────────│
                       │  (access_token,    │
                       │   refresh_token,   │
                       │   expires_in)      │
                       │                    │
                       │  3. Store          │
                       │  (~/.rev/tokens)   │
                       ▼                    │
                ┌─────────────┐             │
                │ Token Store │             │
                │ (encrypted) │             │
                └─────────────┘             │
                       │                    │
                       │  4. API Call       │
                       │  (Bearer token)    │
                       │───────────────────▶│
                       │                    │
                       │  5. Response       │
                       │◀───────────────────│
                       │  (SSE/NDJSON)      │
```

## 3. Provider Connectors

### 3.1 Qwen (chat.qwen.ai)

| Component | Detail |
|-----------|--------|
| Auth | Cookie-based (`token=JWT`) |
| Device Fingerprint | `bx-umidtoken` header |
| Flow | `POST /api/v2/chats/new` → `POST /api/v2/chat/completions` |
| Response | SSE (`data: {...}`) |
| Models | `qwen3.7-plus`, `qwen3.8-max` |
| Limits | **Unlimited** (free web chat) |

### 3.2 Notion AI (notion.so)

| Component | Detail |
|-----------|--------|
| Auth | `token_v2` cookie |
| Flow | `POST /api/v3/runInferenceTranscript` |
| Response | NDJSON (patch operations) |
| Models | 31 models (Sonnet, Opus, GPT, Gemini, etc.) |
| Limits | 75 credits/space (free tier) |

### 3.3 DeepSeek (chat.deepseek.com)

| Component | Detail |
|-----------|--------|
| Auth | Bearer token (login API) |
| PoW | `x-ds-pow-response` header (SHA-256 brute force) |
| Flow | `POST /api/v0/chat_session/create` → `POST /api/v0/chat/completion` |
| Response | SSE (fragment patches) |
| Models | `default`, `expert`, `deep_think`, `search`, `vision` |
| Limits | Rate-limited (free web chat) |

## 4. CLI Reference

```bash
# Authentication
rev login qwen --email user@x.com --pass secret
rev login notion --cookies cookies.json
rev login deepseek --google

# Chat
rev chat qwen "write a function" --model qwen3.8-max --stream
rev chat deepseek "explain quantum computing" --model deep_think

# Server (M2M)
rev serve --port 8000 --api-key my-secret

# Web Intelligence (Firecrawl)
rev search "latest AI papers" --limit 5
rev scrape https://arxiv.org/abs/2401.04088 --format markdown

# Management
rev models
rev status
rev token qwen
rev revoke notion
```

### 4a. app2mcp — koi bhi app ko MCP + API me convert

Koi bhi app (Android/web/desktop) jiska koi official API/MCP nahi — capture karo, convert karo:

```bash
# 1. Capture (phone: mitmdump -s mobile_re.py, proxy+cert; Frida unpin agar pinned)
# 2. Analyze — capture se chat+content endpoints
python3 app2mcp.py analyze
# 3. Build
python3 app2mcp.py build <app>            # single best endpoint
python3 app2mcp.py build <app> --all       # multi-endpoint: top N endpoints,
                                           # ek multi-tool MCP + multi-model API
# 4. Serve
python3 app2mcp.py serve <app> --port 8000 --mcp-port 9880
# Android quickstart guide
python3 app2mcp.py android
```

Outputs (per build): REST API (OpenAI-compat `/v1/chat/completions` SSE + `/read` + `/invoke` + `/endpoints`), FastMCP SSE server (multi mode me har endpoint ka `<app>_<endpoint>_read` tool), aur proxy.git engine ka `GenericFlowAdapter` config (`apps` array — har endpoint alag model). Token-efficiency (`max_tokens`/`batch`) generated servers me built-in.

## 5. M2M API (OpenAI-Compatible)

```bash
# Chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer m2m-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'

# List models
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer m2m-key"
```

## 6. Firecrawl (Self-Hosted)

```bash
# Start Firecrawl
docker compose up -d

# Verify
curl http://localhost:3002/health

# Search
curl http://localhost:3002/v1/search \
  -H "Authorization: Bearer fc-self-hosted" \
  -H "Content-Type: application/json" \
  -d '{"query": "AI research", "limit": 5}'

# Scrape
curl http://localhost:3002/v1/scrape \
  -H "Authorization: Bearer fc-self-hosted" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "formats": ["markdown"]}'
```

## 7. Security Considerations

| Concern | Mitigation |
|---------|------------|
| Token storage | XOR-obfuscated, `chmod 600` |
| TLS fingerprinting | `curl_cffi impersonate="chrome131"` |
| WAF bypass | CloakBrowser (anti-detect Chromium) |
| PoW challenges | SHA-256 brute force solver |
| Rate limiting | Exponential backoff, request queuing |

## 8. File Structure (v2 — capture-only split)

Rev.git ab **browser capture + reverse-engineering kit** hai:

```
Rev/                          # MITM capture + RE tooling
├── capture_flow.py           # kisi bhi app ka AI flow capture
├── qwen_auto.py              # mitmproxy-driven auto-login capture
├── qwen_pipeline.py          # proxy -> login -> token pipeline
├── qwen_token_harvest.py     # browser se token harvest
├── login_auto.py             # login automation
├── mobile_re.py              # mitmdump addon (phone capture)
├── ssl_unpin.js              # frida cert-unpin
├── notion_pure.py            # Notion pure-HTTP RE probe
├── lmarena_flow.py           # LMArena flow RE
├── api_forensics_v2.py       # tokenizer forensics
├── auto_pipeline.py          # capture -> flow template auto-analysis
├── make_test_capture.py      # synthetic capture generator
├── start.sh                  # --login / --mitm / --capture
└── connectors/               # profiles + captured flows
```

**Serving layer ab proxy.git me hai:**

```
proxy.git
├── engine/        # revd — Rust OpenAI-compatible server (live-proven)
└── rev-serving/   # Python serving (universal_server, adapters,
                   #  rev_cli, firecrawl, docker-compose)
```

**Handoff:** Rev.git capture karke token/flow files banata hai
(qwen_token.json, notion_token_v2.txt, connectors/*_flow.json) —
wo proxy.git/rev-serving/ me copy hote hain, wahan se API serve hoti hai.

## 9. References

1. OAuth 2.0 Authorization Framework — RFC 6749
2. Firecrawl Self-Host Documentation — https://docs.firecrawl.dev/contributing/self-host
3. curl_cffi TLS Impersonation — https://curl-cffi.readthedocs.io/
4. CloakBrowser Anti-Detect — https://github.com/CloakHQ/CloakBrowser

---

*Rev is a research project for educational purposes. All provider interactions should comply with their respective Terms of Service.*

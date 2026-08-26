# LMArena (arena.ai) Recon Notes — 2026-08-25

## Domain & Infrastructure
- `lmarena.ai` -> redirect -> `arena.ai` (Cloudflare fronted)
- API surface: `arena.ai/nextjs-api/*` (Next.js API routes)
- Unknown paths -> Cloudflare Worker: 403 `{"error":"Route not allowed"}`
- Telemetry: PostHog (`/rpc/flags/`), Datadog RUM, GA4

## Auth Flow (DISCOVERED)
```
1. reCAPTCHA Enterprise (sitekey: 6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0)
   -> protobuf POST google.com/recaptcha/enterprise/reload
2. POST arena.ai/nextjs-api/sign-up
   body: {"recaptchaToken": "0cAFcWeA..."}
   response 200:
   {
     "access_token": "eyJhbGciOiJFUzI1NiIs...",  <- SUPABASE JWT
     ... iss: https://huogzoeqzcrdvkwtvodi.supabase.co/auth/v1
     ... exp: +1 hour
   }
3. Chat requests -> Bearer <access_token>
```
- Terms modal gates UI (har naye anonymous session pe)
- `provisional_user_id` cookie (UUID) set on first visit

## Chat Endpoint (PENDING)
- Headless me recaptcha low-score -> anonymous session blocked ->
  chat request fire hi nahi hoti (0 network calls on send)
- Chat endpoint + payload capture karne ke liye:
  HEADED browser me ek manual message chahiye:
    python3 capture_flow.py --app lmarena --url https://lmarena.ai
  (apne desktop pe chalao — real display = recaptcha pass)

## Known API Routes
| Route | Method | Status |
|---|---|---|
| /nextjs-api/sign-up | POST | 200 (recaptcha token chahiye) |
| /nextjs-api/autoeval/release-banner | GET | 200 (null) |
| /api/health, /api/models, /api/chat/completions | * | 403 (CF worker) |

## Connector Plan
1. capture_flow.py se chat endpoint + payload capture (headed, ek baar)
2. LMArenaConnector: sign-up (recaptcha token browser se) -> JWT ->
   chat replay (in-page fetch ya pure HTTP with Bearer)
3. JWT 1hr expiry -> auto-refresh sign-up se

# ViMa — Engineering Notes for Claude Code

ViMa is an AI career coach for Indian corporate professionals, available on
**web (primary)** and WhatsApp. This file is the orientation map for any agent
working in this repo. Read it before editing.

---

## Stack

| Layer | Tech |
|---|---|
| API | Python 3.11+, FastAPI, Uvicorn |
| AI | Anthropic Claude (`claude-sonnet-4-6`), prompt caching on persona system block |
| WhatsApp | Gupshup Business API |
| Web frontend | Jinja2 + Alpine.js + Tailwind CDN (no build step); WebSocket real-time chat |
| DB / Storage | Supabase (Postgres + Storage); sync `supabase-py` wrapped in `asyncio.to_thread` |
| Payments (WhatsApp) | Razorpay payment links |
| Payments (web) | Razorpay Checkout JS (`/api/payment/create-order` → `/api/payment/verify`) |
| Auth | Google Sign-In only — no OTP, no phone at sign-in. Phone collected at Razorpay checkout step only. Session stored as a signed JWT in an httponly cookie. |
| PDF / DOCX | WeasyPrint (PDF) + python-docx (DOCX); render Jinja2 HTML templates in `templates/` |
| Voice | AWS Transcribe (OGG/OPU for WhatsApp, WebM/Opus for web); boto3 in `asyncio.to_thread` |

**Design system (all web templates):**
- Background `#F8F4EB` · Primary `#2A2A2A` · Accent `#B8841F`
- Headings: Fraunces (serif, Google Fonts) · Body: Inter (sans, Google Fonts)

---

## Project layout

```
main.py              FastAPI app — all HTTP routes, WebSocket /ws/chat, Gupshup/Razorpay webhooks
config.py            Pydantic Settings loaded from .env. Import `settings`, never os.environ.
flows/
  router.py          Dispatches inbound messages by intent + saved state.
                     route_message()     — WhatsApp (Gupshup webhook)
                     route_web_message() — web (WebSocket)
                     route_web_document()— web file upload (pre-extracted text)
  resume.py          Multi-step resume flow handler
  interview.py       Multi-step interview-prep + mock interview flow handler
services/
  messenger.py       Messenger abstract base + WhatsAppMessenger + get_messenger() factory
  web_messenger.py   WebMessenger — per-user asyncio.Queue, drained by /ws/chat
  claude_service.py  Claude API wrapper with persona prompt caching
  whatsapp_service.py Gupshup HTTP client (send text, document, interactive buttons)
  payment_service.py  Razorpay payment-link creation + webhook signature verification
  storage_service.py  Supabase Storage: upload(), create_signed_url()
  auth_service.py     Google OAuth + JWT session management (create/validate/logout)
  document_parser.py  3-layer document text extraction (pdfplumber → PyPDF2 → OCR)
  voice_service.py    AWS Transcribe pipeline.
                      transcribe_voice_note() — WhatsApp (downloads from Gupshup URL)
                      transcribe_bytes()      — web (raw bytes from MediaRecorder)
models/
  database.py        All Supabase reads/writes. Add helpers here; don't call the client elsewhere.
prompts/
  persona.txt        ViMa persona system prompt (no emojis unless user uses them, mirror Hinglish)
  resume.txt         Resume flow system prompt
  interview.txt      Interview prep system prompt
templates/
  landing.html       Public landing page (/)
  login.html         Google Sign-In page (/login)
  chat.html          Full-page chat UI (/chat) — Alpine.js, WebSocket, Razorpay Checkout JS
  dashboard.html     User dashboard (/dashboard) — subscription status + artifact downloads
  privacy.html       Privacy policy (/privacy)
  resume_*.html      Jinja2 templates rendered by WeasyPrint to generate the PDF/DOCX resume
schema.sql           Supabase schema — run in the SQL editor. Fully idempotent (safe to re-run).
```

---

## How a message flows

### Web channel
1. Browser opens `GET /chat` — session cookie validated, chat.html returned.
2. Alpine.js `init()` fetches `/api/user/history` (last 50 turns) and `/api/user/state`
   (current flow + progress label), renders history bubbles, then opens WebSocket to `/ws/chat`.
3. First-time users see a full-screen welcome overlay; dismissing it sends `__welcome__` over
   the WebSocket which resets state to idle and shows the menu.
4. Inbound text → `route_web_message(email, text)` → flow handler → `WebMessenger._push(event)`.
5. WebSocket drain task reads the queue and pushes JSON events to the browser:
   - `{"type": "text", "text": "..."}` → chat bubble
   - `{"type": "document", "url": "...", "storage_path": "...", "size": N, ...}` → inline card
   - `{"type": "payment", "amount": N, "key_id": "...", ...}` → payment card
   - `{"type": "quick_replies", "text": "...", "options": [...]}` → tappable chips
6. File upload → `POST /api/chat/upload` → document parser → `store_user_document` →
   `asyncio.create_task(route_web_document(...))` (background, non-blocking).
7. Voice memo → `POST /api/chat/voice` → `transcribe_bytes()` → return transcript to frontend
   for review; user sends it as text (no double-injection).
8. Pay button → `POST /api/payment/create-order` → Razorpay Checkout JS → on success →
   `POST /api/payment/verify` (HMAC-SHA256 check) → `mark_subscription_active` →
   `_resume_pending_output`.

### WhatsApp channel
1. Gupshup POSTs to `POST /webhooks/gupshup`.
2. Dedup + rate-limit guards.
3. `route_message(payload)` → flow handler → `WhatsAppMessenger` → Gupshup send API.
4. Payment: Razorpay payment link delivered inline as text. Webhook at `POST /webhooks/razorpay`
   verifies HMAC, marks subscription active, resumes pending output.

---

## Key routes (quick reference)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | none | Landing page |
| GET | `/login` | none | Google Sign-In page |
| GET | `/api/auth/google/login` | none | Redirect to Google consent |
| GET | `/api/auth/google/callback` | state cookie | Exchange code → session cookie |
| POST | `/api/auth/logout` | session | Clear cookie + delete session row |
| GET | `/chat` | session | Chat UI |
| WS | `/ws/chat` | session cookie | Real-time chat |
| GET | `/dashboard` | session | Subscription status + artifacts |
| GET | `/health` | none | Supabase + echo_mode status |
| GET | `/healthz` | none | Liveness (no DB call) |
| POST | `/api/chat/upload` | session | File upload → document parser → flow inject |
| POST | `/api/chat/voice` | session | Voice blob → Transcribe → transcript |
| POST | `/api/payment/create-order` | session | Create Razorpay order, return order_id + key_id |
| POST | `/api/payment/verify` | session | Verify Razorpay signature, activate subscription |
| GET | `/api/files/refresh-url?path=` | session | Fresh 15-min signed URL for a Storage object |
| GET | `/api/user/history` | session | Last 50 conversation turns |
| GET | `/api/user/state` | session | Current flow / step / progress label |
| POST | `/api/admin/activate-user` | admin_secret | Manual beta activation (TODO: remove after Razorpay live) |
| POST | `/webhooks/gupshup` | none | Inbound WhatsApp messages |
| POST | `/webhooks/razorpay` | HMAC | Payment lifecycle events |

---

## Database tables

```
users           phone PK (nullable for web), email unique, google_id, avatar_url, name, channel, locale
conversations   id, phone TEXT (WhatsApp E.164 or email — no FK), role, content, created_at
user_state      phone PK TEXT (E.164 or email — no FK), flow, resume_step, interview_step, data jsonb
payments        id, phone references users(phone), amount_paise, link_id, razorpay_payment_id,
                payment_type, status, period_end, created_at
artifacts       id, phone references users(phone), kind, storage_path, version, created_at
sessions        id, user_id TEXT (email), jwt_token_hash, expires_at
user_documents  id, user_id TEXT (email), type, filename, mime_type, raw_text, created_at
otp_codes       id, phone, code_hash, expires_at, attempts, verified  [legacy, unused]
```

**Important:** `conversations` and `user_state` have no FK to `users.phone` — the migration
`DO $$ ... DROP CONSTRAINT ... $$` in `schema.sql` removed it so email addresses work as keys.
The `payments` and `artifacts` tables still FK to `users.phone`, so web-only users (no phone)
cannot have rows in those tables. At `/api/payment/verify` time, a phone is collected via
the Razorpay checkout form and used to upsert the user before writing payment rows.

---

## Conventions

- **Async everywhere.** Wrap sync SDK calls in `asyncio.to_thread`.
- **Money in paise (int).** Never use floats for currency. `price_subscription_paise = 179900`.
- **Logging:** never log PII at INFO level. Mask phone with `_mask(phone)`, email with `_mask_email(email)`.
- **Secrets:** all in `config.py` via `Settings`. Never read `os.environ` directly.
- **WhatsApp text formatting:** `*bold*`, `_italic_`, `~strike~`.
- **Persona rules** (no emojis unless user uses them, Hinglish mirroring) are in `prompts/persona.txt` — keep code paths neutral.
- **`.env` must never be committed.** Already in `.gitignore`; verify with `git status` before any push.

---

## Subscription gating

`has_active_subscription(phone_or_email)` in `models/database.py` queries `payments` for a `paid`
row with `period_end` in the future. For web users who haven't given a phone yet, the subscription
is looked up by email (stored as the `phone` column value in payments after the verify step).

`_resume_pending_output(user_key)` in `main.py` reads `user_state.data.pending_action` and
re-enters the blocked flow step (either `resume_proc2` or `interview_prep`). Called from:
- `POST /webhooks/razorpay` on `payment_link.paid`
- `POST /api/payment/verify` on web Checkout success
- `POST /api/admin/activate-user` on manual activation

---

## Local run

```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env           # fill in credentials
uvicorn main:app --reload
```

For local webhook testing, expose port 8000 with `ngrok http 8000` and update Gupshup /
Razorpay webhook URLs to the ngrok HTTPS host.

---

## Voice notes

- **WhatsApp:** `transcribe_voice_note(media_url, sender)` — downloads OGG/OPUS from Gupshup,
  uploads to `AWS_TRANSCRIBE_S3_BUCKET`, starts Transcribe job (`language_code=en-IN`), polls,
  returns transcript, cleans up S3 objects.
- **Web:** `transcribe_bytes(audio_bytes)` — same S3/Transcribe pipeline but skips download.
  Returns transcript to the frontend for user review before they hit send (avoids double-injection).

---

## PDF / DOCX generation

WeasyPrint renders `templates/resume_*.html` Jinja2 templates to PDF. python-docx builds the DOCX.
Both are delivered via `messenger.send_document(user_id, bytes, filename, caption)`.
`WebMessenger.send_document` uploads to Supabase Storage and pushes a `{type: "document", url, storage_path, size}` event the frontend renders as an inline card with Preview + Download.

---

## Admin endpoint (temporary)

`POST /api/admin/activate-user` — body `{email, duration_days, admin_secret}`.
Validates `admin_secret` with `secrets.compare_digest` against `ADMIN_SECRET` env var.
Calls `mark_subscription_active(phone=email)` + `_resume_pending_output(email)`.
**TODO: remove once Razorpay production keys are live** and all activation goes through `/api/payment/verify`.

---

## Completed features

- WhatsApp flow (Gupshup webhook → resume + interview flows → Razorpay payment link → PDF/DOCX delivery)
- Document parsing (3-layer: pdfplumber → PyPDF2 → Tesseract OCR)
- Claude integration with persona system prompt + prompt caching
- PDF + DOCX dual delivery (WeasyPrint + python-docx)
- Razorpay 60-day access pass (payment links for WhatsApp, Checkout JS for web)
- Google Sign-In (no phone at sign-in; JWT session in httponly cookie; 30-day TTL)
- WebSocket real-time chat with per-user asyncio.Queue
- File upload (`/api/chat/upload`) with document parsing + flow injection
- Voice transcription (`/api/chat/voice`) via AWS Transcribe
- Conversation history persistence + `/api/user/history` endpoint
- Flow state progress indicator + `/api/user/state` endpoint
- First-time welcome overlay (localStorage flag per email, `__welcome__` sentinel)
- Skeleton loading screen + connection-error banner + responsive 480px desktop column
- `/dashboard` page (profile card, subscription status, signed-URL artifact downloads)
- Landing page (web-first copy, Google Sign-In CTA, pricing, social proof)
- Admin manual activation endpoint (`/api/admin/activate-user`)
- Railway deployment config (`railway.toml`, `nixpacks.toml`, `DEPLOY.md`)
- Silent-user nudge + 7-day archive background loop

## Known schema issues to apply before go-live

Run the full `schema.sql` in the Supabase SQL editor. The idempotent migration blocks handle:
- Adding `email`, `google_id`, `avatar_url` columns to `users`
- Removing FK constraints from `conversations` and `user_state` so email keys work
- Creating `sessions` and `user_documents` tables if they don't exist yet

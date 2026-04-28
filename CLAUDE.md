# ViMa - Engineering Notes for Claude Code

ViMa is a WhatsApp-first AI career coach for Indian corporate professionals. This file is the orientation map for any agent working in this repo. Read it before editing.

## Stack
- **API**: Python 3.11+, FastAPI, Uvicorn.
- **AI**: Anthropic Claude (default model: `claude-sonnet-4-6`) via the `anthropic` SDK. Use prompt caching for the persona system prompt.
- **WhatsApp**: Gupshup Business API.
- **DB / Storage**: Supabase (Postgres + Storage). Sync `supabase-py` wrapped in `asyncio.to_thread`.
- **Payments**: Razorpay payment links + webhook signature verification.
- **PDF**: WeasyPrint rendering Jinja2 HTML templates in `templates/`.
- **Voice**: OpenAI Whisper for inbound voice notes (swappable via `VOICE_PROVIDER`).

## Layout
- `main.py` - FastAPI app + Gupshup/Razorpay webhook entrypoints.
- `config.py` - Pydantic `Settings` loaded from env. Import `settings` from here, never read `os.environ` directly.
- `flows/` - per-feature conversation handlers. `router.py` dispatches inbound messages by intent and saved state; `resume.py` and `interview.py` own their own multi-step flows.
- `services/` - thin wrappers around external SDKs: Claude, Gupshup, Razorpay, WeasyPrint, Whisper, Supabase Storage, plus document/research helpers.
- `models/database.py` - all Supabase reads/writes. Add new helpers here, do not hit the client directly from flows.
- `prompts/` - plain-text system prompts (`persona.txt`, `resume.txt`, `interview.txt`). Edit text here, not in code.
- `templates/` - Jinja2 + CSS for PDFs.

## How a message flows
1. Gupshup POSTs to `/webhooks/gupshup`.
2. `flows/router.route_message` parses sender + message, looks up `user_state`, picks a handler.
3. Handler returns a normalized reply dict `{to, type, text|document_url|...}`.
4. `services.whatsapp_service.WhatsAppService.send` dispatches to the right Gupshup endpoint.

## Conventions
- Async everywhere. If wrapping a sync SDK, use `asyncio.to_thread`.
- Keep persona/system prompts in `prompts/*.txt` and load them once per `ClaudeService` instance.
- Use prompt caching on the persona system block (`cache_control: {type: "ephemeral"}`).
- Money is stored in **paise (int)**. Never use floats for currency.
- WhatsApp formatting: `*bold*`, `_italic_`, `~strike~`, single newlines for line breaks.
- Persona rules (no emojis unless user uses them, mirror Hinglish, etc.) are enforced by `prompts/persona.txt` - keep code paths neutral.

## Database (expected tables)
- `users(phone PK, name, locale, created_at)`
- `conversations(id, phone, role, content, created_at)`
- `user_state(phone PK, flow, resume_step, interview_step, data jsonb)`
- `payments(id, phone, product, amount_paise, link_id, status, created_at)`
- `artifacts(id, phone, kind, storage_path, created_at)`

When adding a new flow, add the `flow` enum value and any new step columns or jsonb keys here first.

## Local run
```
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill in keys
uvicorn main:app --reload
```
For local Gupshup/Razorpay webhooks, expose port 8000 with `ngrok http 8000` and point the webhook URLs to the ngrok HTTPS host.

## Pricing
Single ₹1,799/month subscription unlocks all features (resume + interview). No per-feature pricing. Money is stored as **int paise** (`179900`). Use `PaymentService.is_subscribed(phone)` to gate paid flows. `PaymentService.create_subscription_link(phone)` creates the Razorpay payment link.

## AWS Transcribe (voice)
Voice notes from WhatsApp are OGG/OPUS. The flow: download → upload to `AWS_TRANSCRIBE_S3_BUCKET` → start job with `language_code='en-IN'` → poll → fetch JSON → delete temp S3 objects. All config in `config.py` (`aws_*` fields). The boto3 calls are sync; they run in `asyncio.to_thread` inside `voice_service.py`.

## Things to keep in mind
- Most service methods still have `# TODO` stubs. Prefer filling those in over restructuring the skeleton.
- The user is non-technical and ships from a Windows machine. Keep tooling cross-platform; do not assume bash-only scripts.
- WhatsApp users expect quick replies. Long-running work (PDF rendering, Claude calls) should stay under ~15s; offload anything heavier to a background task and follow up with a second message.
- Never log PII (phone numbers, full message bodies) at info level. Mask phone numbers in logs.
- Never commit `.env`. The `.gitignore` already covers it but double-check before any `git add`.

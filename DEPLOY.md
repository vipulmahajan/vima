# Deploying ViMa to Railway

End-to-end checklist for shipping ViMa to production. Follow it top-to-bottom
the first time; on subsequent deploys you'll only repeat steps 7-8.

---

## 0. Pre-flight check (do this once, before everything else)

- [ ] All four sets of credentials rotated since they were last shared in chat:
  Anthropic, Supabase service key, AWS access key, Razorpay (live mode).
- [ ] Supabase project exists and the **full `schema.sql` has been run** in the SQL editor.
  The schema now includes: `users` (with `email`, `google_id`, `avatar_url` columns),
  `sessions`, `user_documents`, `otp_codes` (unused but harmless), and idempotent
  migrations to drop FK constraints on `conversations` and `user_state` so web users
  (identified by email) can be stored without a phone number.
- [ ] Supabase Storage bucket `vima-artifacts` exists and is **private**.
- [ ] Razorpay account is in **live** mode and KYC is complete (₹1,799 charges
      will fail in test mode for real users).
- [ ] You own the WhatsApp Business number that Gupshup is provisioning.

---

## 1. Push the code to GitHub

From the project root (`C:\Users\VM\vima` on your machine):

```bash
git init                                       # if not already a repo
git add .
git commit -m "Initial ViMa production build"
git branch -M main
git remote add origin https://github.com/vipulmahajan/vima.git
git push -u origin main
```

> **Sanity check** before you push: `git status` should NOT list `.env`. It's
> already in `.gitignore`, but verify. If `.env` ever shows up in `git status`,
> stop and figure out why before pushing.

---

## 2. Create the Railway project

1. Go to <https://railway.app/new> and pick **Deploy from GitHub repo**.
2. Authorise Railway on the `vima` repo.
3. Select the repo. Railway detects `nixpacks.toml` + `railway.toml` and
   begins the first build. The first build takes ~5 minutes (it has to
   install GTK + tesseract + poppler from Nix).
4. **Don't worry about the build crashing on the first run** — it will fail
   when it tries to start because we haven't set the environment variables
   yet. We do that next.

---

## 3. Set environment variables in Railway

Open the project → **Variables** tab → **+ New Variable** for each row below.
Values come from your real production credentials, not the placeholders in
`.env.example`.

### App
| Variable               | Value                                              |
|------------------------|----------------------------------------------------|
| `APP_ENV`              | `production`                                       |
| `DEBUG`                | `false`                                            |
| `BASE_URL`             | `https://<your-railway-subdomain>.up.railway.app`  |
| `ECHO_MODE`            | `false`                                            |
| `WEB_CONCURRENCY`      | `2`     *(bump to 4 once you have steady traffic)* |

> `PORT` is injected by Railway — **do not set it manually**.
> `HOST` is hardcoded to `0.0.0.0` in the start command.

### Claude (Anthropic)
| Variable              | Value                                |
|-----------------------|--------------------------------------|
| `ANTHROPIC_API_KEY`   | `sk-ant-...`  *(production key)*     |
| `CLAUDE_MODEL`        | `claude-sonnet-4-6`                  |
| `CLAUDE_MAX_TOKENS`   | `2048`                               |

### Gupshup (WhatsApp)
| Variable                  | Value                                          |
|---------------------------|------------------------------------------------|
| `GUPSHUP_API_KEY`         | from Gupshup dashboard → API key               |
| `GUPSHUP_APP_NAME`        | the app name registered with Gupshup           |
| `GUPSHUP_SOURCE_NUMBER`   | E.164 without `+` (e.g. `919876543210`)        |
| `GUPSHUP_WEBHOOK_SECRET`  | Gupshup app → Settings → webhook shared secret |

### Supabase
| Variable                    | Value                                     |
|-----------------------------|-------------------------------------------|
| `SUPABASE_URL`              | `https://<project>.supabase.co`           |
| `SUPABASE_SERVICE_KEY`      | service-role key  *(server-only, secret)* |
| `SUPABASE_ANON_KEY`         | anon key                                  |
| `SUPABASE_STORAGE_BUCKET`   | `vima-artifacts`                          |

### Razorpay
| Variable                   | Value                                    |
|----------------------------|------------------------------------------|
| `RAZORPAY_KEY_ID`          | `rzp_live_...`  *(LIVE, not test)*       |
| `RAZORPAY_KEY_SECRET`      | live secret                              |
| `RAZORPAY_WEBHOOK_SECRET`  | from Razorpay → Settings → Webhooks      |

### Pricing
| Variable                    | Value      |
|-----------------------------|------------|
| `PRICE_SUBSCRIPTION_PAISE`  | `179900`   |

### AWS Transcribe (voice notes)
| Variable                     | Value                              |
|------------------------------|------------------------------------|
| `AWS_ACCESS_KEY_ID`          | IAM user with Transcribe + S3 perms|
| `AWS_SECRET_ACCESS_KEY`      | matching secret                    |
| `AWS_REGION`                 | `ap-south-1`                       |
| `AWS_TRANSCRIBE_S3_BUCKET`   | `vima-transcribe`  *(must exist)*  |

### Web auth (Google Sign-In + sessions)
| Variable               | Value                                                              |
|------------------------|--------------------------------------------------------------------|
| `GOOGLE_CLIENT_ID`     | from Google Cloud Console → APIs & Services → Credentials          |
| `GOOGLE_CLIENT_SECRET` | same credential, secret value                                      |
| `GOOGLE_REDIRECT_URI`  | `https://<your-railway-url>/api/auth/google/callback`              |
| `JWT_SECRET`           | random 32-byte hex — `python -c "import secrets; print(secrets.token_hex(32))"` |

> **Before going live:** add `https://<your-railway-url>/api/auth/google/callback`
> as an **Authorised redirect URI** in Google Cloud Console → APIs & Services →
> Credentials → your OAuth 2.0 Client ID → Edit. Without this, Google will return
> `redirect_uri_mismatch` and users cannot sign in.

### Public site
| Variable                | Value                                                |
|-------------------------|------------------------------------------------------|
| `VIMA_WHATSAPP_NUMBER`  | E.164 without `+` — same number as `GUPSHUP_SOURCE_NUMBER` |
| `SUPPORT_EMAIL`         | the address that handles data-deletion requests      |

After saving, Railway will automatically redeploy the service.

---

## 4. Confirm the deploy is healthy

Once the redeploy finishes, your service has a public URL like
`https://vima-production.up.railway.app`. Run:

```bash
curl -sf https://<your-url>/health
```

Expected response:

```json
{
  "status": "ok",
  "env": "production",
  "supabase": "connected",
  "echo_mode": false
}
```

What each `supabase` value means:
- `connected` — Supabase reachable and responsive ✅
- `not_configured` — `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` env vars missing
- `init_failed` — creds present but client init threw — check the values
- `error` — query failed (RLS misconfigured, network, etc.) — see `supabase_error`

Also visit `https://<your-url>/` in a browser. You should see the landing
page with the "Start on WhatsApp" button. Click it — it should open a
WhatsApp chat with the production number.

Visit `https://<your-url>/privacy` to confirm the privacy page renders with
your `SUPPORT_EMAIL`.

---

## 5. Point Gupshup at the Railway URL

1. Go to <https://www.gupshup.io/>, open your WhatsApp app.
2. **Settings → Callback URL**.
3. Replace the existing URL with:
   ```
   https://<your-railway-url>/webhooks/gupshup
   ```
4. Save.
5. Send a "hi" from a real WhatsApp client to the production number.
6. Watch the Railway **Deployments → Logs** stream. You should see:
   ```
   INFO vima: Gupshup event: type=message
   INFO vima: Inbound msg: from=91XXX****YYY type=text text='hi'
   ```
7. The user should receive the 5-stage menu reply.

If nothing arrives after 10 seconds, common causes:
- Gupshup IP allowlist on your end (we don't have one — skip)
- Gupshup app still in **sandbox** mode (see step 7 below)
- Wrong webhook URL (leading/trailing slash, wrong subdomain)

---

## 6. Point Razorpay at the Railway URL

1. Go to <https://dashboard.razorpay.com/app/webhooks>.
2. **Add new webhook**.
3. URL: `https://<your-railway-url>/webhooks/razorpay`
4. Secret: paste a strong random string. Use the **same value** as
   `RAZORPAY_WEBHOOK_SECRET` in Railway (re-set it now if they don't match).
5. Active events — tick at least:
   - `payment_link.paid`
   - `payment_link.expired`
   - `payment.failed`
6. Save.
7. From the same page, click **Test** → pick `payment_link.paid` → send.
   Railway logs should show:
   ```
   INFO vima: Razorpay event: payment_link.paid
   INFO services.payment_service: razorpay.event payment_link.paid
   ```
   The webhook responds 200 even though there's no matching row in our
   payments table for the test event — that's expected.

---

## 7. Flip Gupshup from sandbox to live

Sandbox mode is the WhatsApp environment Gupshup gives you for testing
during signup — only numbers you opt-in can message it, no template
constraints, no real WhatsApp Business policies.

To go live:

1. In Gupshup → your app → **Go Live** (or **Promote to Production**).
2. Submit your WhatsApp Business profile (display name, description, logo,
   business category). Meta reviews this — usually <24h.
3. Submit at least one **template message** for opt-in / re-engagement
   (we use plain session messages today, so this can be minimal).
4. Once Meta approves, your app moves out of sandbox automatically.
5. Re-test step 5 — send a "hi" from a number that was NOT in the sandbox
   allow-list. It should work.

> **Don't skip this.** Sandbox numbers can only receive messages from
> opted-in test contacts; real users will never see ViMa replies.

---

## 8. Smoke-test the full path

From a real WhatsApp client (NOT the sandbox test contact), send:

1. `hi` → should receive the 5-stage menu.
2. `2` → should enter the resume flow at Q1.
3. Walk through Q1-Q6.
4. At Q6 → ViMa generates the resume → sends the payment link.
5. Pay through Razorpay (use a real ₹1,799 transaction or create a test
   coupon for yourself).
6. Within ~30 seconds you should receive: PDF resume + DOCX resume +
   strategy note.
7. Reply `interview` → walk through the prep flow.
8. Reply `menu` → confirm the menu shows again.

Tail the Railway logs through the whole walk and confirm there are no
ERROR-level lines.

---

## 9. Operational notes

- **Routine deploys**: just `git push origin main`. Railway auto-deploys
  on push and runs the health check before swapping traffic.
- **Rollback**: Railway → Deployments tab → click the previous successful
  deploy → **Redeploy**.
- **Logs**: `Logs` tab streams stdout/stderr from all replicas. Filter by
  `ERROR` for failures, by `Razorpay event` for payment activity, by
  `claude.ok` for successful Claude calls (token usage included).
- **Scaling up**: bump `WEB_CONCURRENCY` (uvicorn worker count) before
  bumping `numReplicas` in `railway.toml`. The in-process rate-limit and
  dedup state is per-worker today; multi-replica needs Redis.
- **Cost monitoring**: Anthropic + AWS Transcribe + Gupshup all bill per
  use. Watch the dashboards weekly until you have a feel for the unit
  economics of one access pass.

---

## 10. When something breaks

- **`/health` says `supabase: error`** — copy the `supabase_error` field
  and check Supabase dashboard → Logs. Most likely RLS or a service-key
  rotation.
- **Gupshup webhooks return 200 but user never gets a reply** — check
  `GUPSHUP_API_KEY` in Railway matches the dashboard. The send call logs
  `Gupshup not configured` if the key is empty.
- **Razorpay webhook signature invalid** — `RAZORPAY_WEBHOOK_SECRET` in
  Railway doesn't match the value you set in Razorpay Dashboard. Re-paste
  on both sides; they must be byte-identical.
- **PDF render fails on the running deploy** — `nixpacks.toml` should have
  installed GTK; if WeasyPrint still can't load `libgobject-2.0-0`, force
  a clean rebuild from the Railway Deployments tab.

You're done. Welcome to production.

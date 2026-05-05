"""ViMa - WhatsApp-first AI Career Coach.

FastAPI application entrypoint. Receives Gupshup WhatsApp webhooks, routes
inbound messages through flow handlers, and dispatches replies via Gupshup.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, UploadFile, status, WebSocket, WebSocketDisconnect  # noqa: F401
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import settings
from services.auth_service import (
    get_google_auth_url,
    handle_google_callback,
    create_session,
    validate_session,
    logout as auth_logout,
)
from flows.router import (
    route_message,
    route_web_message,
    route_web_document,
    _extract_sender,
    _extract_sender_name,
    _extract_message,
)
from services.messenger import get_messenger
from services.whatsapp_service import WhatsAppService  # noqa: F401 — kept for any incidental import
from services.payment_service import PaymentService
from models.database import (
    init_db, close_db, upsert_user,
    get_user_state, upsert_user_state, merge_user_state_data,
    list_active_user_states, archive_user_state,
    store_user_document,
    record_payment_intent, mark_subscription_active,
)


# ── Edge-case state ─────────────────────────────────────────────────────────

# Last 200 message ids we've processed (FIFO). Gupshup occasionally re-delivers
# the same webhook; the first-seen wins.
_RECENT_MESSAGE_IDS: collections.deque[str] = collections.deque(maxlen=200)
_RECENT_MESSAGE_IDS_SET: set[str] = set()

# Per-user message timestamps for rate limiting (max 30 msg / 60s).
_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_MAX        = 30
_USER_MSG_TIMES: dict[str, "collections.deque[float]"] = {}

# Silent-user nudge config.
_NUDGE_AFTER_HOURS  = 24
_ARCHIVE_AFTER_DAYS = 7
_NUDGE_LOOP_INTERVAL_SEC = 3600  # every hour


def _seen_message(msg_id: Optional[str]) -> bool:
    """Return True if this message id has already been processed."""
    if not msg_id:
        return False
    if msg_id in _RECENT_MESSAGE_IDS_SET:
        return True
    if len(_RECENT_MESSAGE_IDS) == _RECENT_MESSAGE_IDS.maxlen:
        evicted = _RECENT_MESSAGE_IDS[0]
        _RECENT_MESSAGE_IDS_SET.discard(evicted)
    _RECENT_MESSAGE_IDS.append(msg_id)
    _RECENT_MESSAGE_IDS_SET.add(msg_id)
    return False


def _rate_limited(phone: str) -> bool:
    """Return True if this phone has sent > 30 messages in the last 60s."""
    now = time.monotonic()
    times = _USER_MSG_TIMES.setdefault(phone, collections.deque(maxlen=_RATE_LIMIT_MAX + 5))
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    while times and times[0] < cutoff:
        times.popleft()
    if len(times) >= _RATE_LIMIT_MAX:
        return True
    times.append(now)
    return False

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("vima")


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting ViMa (env=%s, echo_mode=%s)", settings.app_env, settings.echo_mode)
    if not settings.echo_mode:
        try:
            await init_db()
        except Exception as exc:  # noqa: BLE001
            log.warning("Supabase init skipped (%s); continuing in degraded mode.", exc)

    # Background lifecycle loop: silent-user nudges + 7-day archive.
    nudge_task = asyncio.create_task(_silence_nudge_loop(), name="vima.nudge_loop")

    try:
        yield
    finally:
        nudge_task.cancel()
        try:
            await nudge_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if not settings.echo_mode:
            try:
                await close_db()
            except Exception:  # noqa: BLE001
                pass


# ── Lifecycle loop: silent-user nudge + 7-day archive ───────────────────────

async def _silence_nudge_loop() -> None:
    """Run forever, every hour, checking for stale state."""
    log.info("nudge_loop.start interval=%ds", _NUDGE_LOOP_INTERVAL_SEC)
    while True:
        try:
            await asyncio.sleep(_NUDGE_LOOP_INTERVAL_SEC)
            await _check_and_nudge_silent_users()
        except asyncio.CancelledError:
            log.info("nudge_loop.cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("nudge_loop iteration failed")


async def _check_and_nudge_silent_users() -> None:
    """Single sweep: nudge users idle 24h-7d, archive users idle >7d."""
    try:
        states = await list_active_user_states()
    except Exception:  # noqa: BLE001
        log.exception("list_active_user_states failed")
        return

    now = _dt.datetime.now(_dt.timezone.utc)
    nudge_count   = 0
    archive_count = 0

    for s in states:
        phone = s.get("user_id") or ""
        if not phone:
            continue

        flow = s.get("flow") or "idle"
        step = (
            s.get("resume_step")    if flow == "resume"
            else s.get("interview_step") if flow == "interview"
            else None
        )
        # Skip terminal / quiet steps.
        if flow == "idle":
            continue
        if step in ("delivered", "prep_delivered", None):
            continue

        updated_at = _parse_ts(s.get("updated_at"))
        if updated_at is None:
            continue
        idle = now - updated_at

        # ── 7-day archive ──
        if idle.days >= _ARCHIVE_AFTER_DAYS:
            try:
                await archive_user_state(phone)
                archive_count += 1
            except Exception:  # noqa: BLE001
                log.exception("archive_user_state failed phone=%s", _mask(phone))
            continue

        # ── 24-hour nudge ──
        if idle.total_seconds() < _NUDGE_AFTER_HOURS * 3600:
            continue

        data = s.get("data") or {}
        last_nudge_iso = data.get("last_nudge_at")
        last_nudge = _parse_ts(last_nudge_iso) if last_nudge_iso else None
        if last_nudge and (now - last_nudge).total_seconds() < _NUDGE_AFTER_HOURS * 3600:
            continue

        # Send the nudge.
        users_name = (data.get("user_name") or "").strip()
        greeting = f"Hey {users_name}," if users_name else "Hey,"
        body = (
            f"{greeting} I've got your details saved. Ready to pick up "
            "where we left off? Just reply with anything."
        )
        try:
            messenger = await get_messenger(phone)
            await messenger.send_text(phone, body)
            await merge_user_state_data(phone, {"last_nudge_at": now.isoformat()})
            nudge_count += 1
            log.info("nudge.sent phone=%s flow=%s step=%s idle_h=%.1f",
                     _mask(phone), flow, step, idle.total_seconds() / 3600)
        except Exception:  # noqa: BLE001
            log.exception("nudge send failed phone=%s", _mask(phone))

    log.info("nudge_loop.sweep nudged=%d archived=%d total_states=%d",
             nudge_count, archive_count, len(states))


def _parse_ts(value: Any) -> Optional[_dt.datetime]:
    """Parse ISO-8601 / Supabase timestamp string into an aware datetime."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


app = FastAPI(
    title="ViMa - AI Career Coach",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Public site: landing + privacy ──────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_site_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

# Cookie names — defined here so all routes and the WS handler share the same constants.
_SESSION_COOKIE = "vima_session"
_STATE_COOKIE   = "vima_oauth_state"


def _wa_link(number: str) -> str:
    """Build the wa.me deeplink. Falls back to a click-to-chat placeholder."""
    n = (number or "").strip().lstrip("+").replace(" ", "")
    return f"https://wa.me/{n}" if n else "#"


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """Render the landing page."""
    template = _site_env.get_template("landing.html")
    html = template.render(
        wa_link=_wa_link(settings.vima_whatsapp_number),
        wa_number=settings.vima_whatsapp_number,
        price_inr=settings.price_subscription_paise // 100,
        support_email=settings.support_email,
    )
    return HTMLResponse(html)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    template = _site_env.get_template("privacy.html")
    html = template.render(support_email=settings.support_email)
    return HTMLResponse(html)


@app.get("/chat", response_class=HTMLResponse, response_model=None)
async def chat_page(
    request: Request,
    vima_session: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE),
) -> HTMLResponse | RedirectResponse:
    """Render the chat UI. Redirects to /login if the session is invalid."""
    if not vima_session:
        return RedirectResponse(url="/login", status_code=302)
    email = await validate_session(vima_session)
    if not email:
        return RedirectResponse(url="/login", status_code=302)

    from models.database import get_user_by_email
    user = await get_user_by_email(email) or {}

    template = _site_env.get_template("chat.html")
    html = template.render(
        user_email=email,
        user_name=user.get("name") or email.split("@")[0],
        user_avatar=user.get("avatar_url") or "",
        razorpay_key_id=settings.razorpay_key_id,
        price_paise=settings.price_subscription_paise,
        price_inr=settings.price_subscription_paise // 100,
    )
    return HTMLResponse(html)


@app.get("/dashboard", response_class=HTMLResponse, response_model=None)
async def dashboard_page(
    request: Request,
    vima_session: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE),
) -> HTMLResponse | RedirectResponse:
    """User dashboard: subscription status + list of generated artifacts."""
    if not vima_session:
        return RedirectResponse(url="/login", status_code=302)
    email = await validate_session(vima_session)
    if not email:
        return RedirectResponse(url="/login", status_code=302)

    from models.database import get_user_by_email
    user = await get_user_by_email(email) or {}
    name = user.get("name") or email.split("@")[0]
    initials = (name[:2]).upper()

    # Subscription status — web users are keyed by email in the payments table
    from models.database import has_active_subscription
    import datetime as _dt2
    is_active = await has_active_subscription(email)

    # Days remaining (fetch from payments table)
    days_remaining = 0
    if is_active:
        from models.database import _get_client
        client = _get_client()
        if client:
            def _days():
                now = _dt2.datetime.now(_dt2.timezone.utc)
                resp = (
                    client.table("payments")
                    .select("period_end")
                    .eq("user_id", email)
                    .eq("status", "paid")
                    .gt("period_end", now.isoformat())
                    .order("period_end", desc=True)
                    .limit(1)
                    .execute()
                )
                if resp.data:
                    pe = resp.data[0].get("period_end") or ""
                    if pe:
                        end = _dt2.datetime.fromisoformat(pe.replace("Z", "+00:00"))
                        return max(0, (end - now).days)
                return 0
            days_remaining = await asyncio.to_thread(_days)

    # Artifacts with fresh signed URLs
    from services.storage_service import StorageService
    storage = StorageService()

    def _fetch_artifacts():
        c = _get_client()
        if not c:
            return []
        resp = (
            c.table("artifacts")
            .select("kind, storage_path, created_at")
            .eq("user_id", email)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return resp.data or []

    from models.database import _get_client as _gc
    raw_arts = await asyncio.to_thread(_fetch_artifacts)
    artifacts = []
    for art in raw_arts:
        signed_url = ""
        try:
            signed_url = await storage.create_signed_url(art["storage_path"])
        except Exception:
            pass
        created = art.get("created_at") or ""
        try:
            dt = _dt2.datetime.fromisoformat(created.replace("Z", "+00:00"))
            created_fmt = dt.strftime("%-d %b %Y")
        except Exception:
            created_fmt = created[:10]
        artifacts.append({
            "kind":          art.get("kind") or "",
            "storage_path":  art.get("storage_path") or "",
            "signed_url":    signed_url,
            "created_at_fmt": created_fmt,
        })

    template = _site_env.get_template("dashboard.html")
    html = template.render(
        email=email,
        name=name,
        initials=initials,
        avatar_url=user.get("avatar_url") or "",
        is_active=is_active,
        days_remaining=days_remaining,
        artifacts=artifacts,
    )
    return HTMLResponse(html)


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    """Production health probe. Used by Railway + uptime monitors.

    Returns 200 with ``supabase: connected`` on success, or 200 with
    ``supabase: error`` (and the error message) on failure. We never 5xx
    here — Railway interprets non-2xx as unhealthy and may restart the
    container. A degraded Supabase doesn't warrant a restart; we'd rather
    surface the issue in the body and keep serving traffic.
    """
    supabase_state = "connected"
    supabase_err: Optional[str] = None
    try:
        # Lazy import keeps module-level coupling minimal.
        from models.database import _get_client, _supabase_configured
        if not _supabase_configured():
            supabase_state = "not_configured"
        else:
            client = _get_client()
            if client is None:
                supabase_state = "init_failed"
            else:
                # Cheap probe: select 1 row from any always-present table.
                # We use users + limit(1) so we don't fetch a row we don't need.
                def _ping() -> None:
                    client.table("users").select("phone").limit(1).execute()
                await asyncio.to_thread(_ping)
    except Exception as exc:  # noqa: BLE001
        supabase_state = "error"
        supabase_err = str(exc)[:200]

    body: dict[str, Any] = {
        "status":   "ok",
        "env":      settings.app_env,
        "supabase": supabase_state,
        "echo_mode": settings.echo_mode,
    }
    if supabase_err:
        body["supabase_error"] = supabase_err
    return JSONResponse(body)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Lightweight liveness probe (no Supabase call)."""
    return JSONResponse({
        "status":     "ok",
        "echo_mode":  settings.echo_mode,
        "env":        settings.app_env,
    })


# ── Auth ─────────────────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    vima_session: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE),
) -> dict[str, Any]:
    """Resolve the session cookie to a user dict, or raise 401."""
    if not vima_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    email = await validate_session(vima_session)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    from models.database import get_user_by_email
    user = await get_user_by_email(email) or {}
    return {"email": email, "name": user.get("name") or "", "avatar_url": user.get("avatar_url") or ""}


# ── Google OAuth endpoints ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    vima_session: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE),
) -> HTMLResponse | RedirectResponse:
    """Render the Google Sign-In page. Redirect to /chat if already logged in."""
    if vima_session and await validate_session(vima_session):
        return RedirectResponse(url="/chat", status_code=302)
    template = _site_env.get_template("login.html")
    return HTMLResponse(template.render())


@app.get("/api/auth/google/login")
async def google_login() -> RedirectResponse:
    """Generate a CSRF state token and redirect to Google's OAuth consent page."""
    state = secrets.token_urlsafe(32)
    url   = get_google_auth_url(state)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE,
        value=state,
        max_age=600,          # 10 minutes — just for the OAuth round-trip
        httponly=True,
        secure=settings.app_env.lower() == "production",
        samesite="lax",
        path="/",
    )
    return response


@app.get("/api/auth/google/callback")
async def google_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    vima_oauth_state: Optional[str] = Cookie(default=None, alias=_STATE_COOKIE),
) -> RedirectResponse:
    """Handle Google's redirect back with the authorisation code."""
    is_prod  = settings.app_env.lower() == "production"
    ttl_secs = settings.session_ttl_days * 86_400

    if error:
        log.warning("google_callback.error: %s", error)
        return RedirectResponse(url="/login?error=access_denied", status_code=302)

    # CSRF check.
    if not state or not vima_oauth_state or state != vima_oauth_state:
        log.warning("google_callback.state_mismatch")
        return RedirectResponse(url="/login?error=state_mismatch", status_code=302)

    if not code:
        return RedirectResponse(url="/login?error=no_code", status_code=302)

    try:
        user = await handle_google_callback(code)
    except Exception:
        log.exception("google_callback.handle_failed")
        return RedirectResponse(url="/login?error=auth_failed", status_code=302)

    token = await create_session(user["email"])

    response = RedirectResponse(url="/chat", status_code=302)
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=ttl_secs,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        path="/",
    )
    # Clear the ephemeral state cookie.
    response.delete_cookie(key=_STATE_COOKIE, path="/")
    return response


@app.post("/api/auth/logout")
async def api_logout(
    response: Response,
    vima_session: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE),
) -> RedirectResponse:
    """Invalidate the session, clear the cookie, redirect to /."""
    if vima_session:
        await auth_logout(vima_session)
    redir = RedirectResponse(url="/", status_code=302)
    redir.delete_cookie(key=_SESSION_COOKIE, path="/")
    return redir


# ── Chat media endpoints ─────────────────────────────────────────────────────

_UPLOAD_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
_UPLOAD_ALLOWED_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
}
_UPLOAD_ALLOWED_SUFFIXES = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}


@app.post("/api/chat/upload")
async def api_chat_upload(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Accept a file upload (PDF, DOCX, JPG, PNG ≤ 10 MB).

    Returns {filename, size, content_type}. The actual resume/document
    processing is wired in Step 5 — for now we validate and store the bytes.
    """
    form   = await request.form()
    upload: Optional[UploadFile] = form.get("file")  # type: ignore[assignment]

    if upload is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No file attached. Send a multipart field named 'file'.")

    # Read up to MAX + 1 bytes so we can detect oversized files without
    # buffering the entire file first.
    data = await upload.read(_UPLOAD_MAX_BYTES + 1)
    if len(data) > _UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="File exceeds the 10 MB limit.")

    content_type = upload.content_type or ""
    suffix       = Path(upload.filename or "").suffix.lower()

    if content_type not in _UPLOAD_ALLOWED_TYPES and suffix not in _UPLOAD_ALLOWED_SUFFIXES:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            detail="Unsupported file type. Send PDF, Word doc, JPG or PNG.")

    filename = upload.filename or f"upload{suffix}"
    log.info("chat.upload user=%s file=%s size=%d", _mask_email(user["email"]), filename, len(data))

    # Resolve MIME (use suffix hint when browser sends generic octet-stream).
    from services.document_parser import _resolve_mime, _extract_sync
    resolved_mime = _resolve_mime(content_type or None, filename, data)

    # Extract text synchronously (OCR may block; run off event loop).
    try:
        extracted = await asyncio.to_thread(_extract_sync, data, resolved_mime)
    except Exception as exc:
        log.warning("chat.upload parse error: %s", exc)
        extracted = ""

    if not extracted:
        return JSONResponse(
            {"success": False,
             "error": "Couldn't read text from this file — try a different format or paste the text directly."},
            status_code=200,
        )

    # Persist to user_documents so the flow can reference it later.
    await store_user_document(
        user_id=user["email"],
        doc_type="resume",   # flow router will re-classify if we're in interview step
        filename=filename,
        mime_type=resolved_mime,
        raw_text=extracted,
    )

    # Inject into the flow as a background task so the HTTP response is instant.
    asyncio.create_task(
        route_web_document(user["email"], extracted, filename, resolved_mime)
    )

    return JSONResponse({"success": True, "filename": filename, "size": len(data)})


@app.post("/api/chat/voice")
async def api_chat_voice(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Accept a voice recording blob (WebM/Opus from MediaRecorder).

    Returns {transcript: str}. Transcription is wired in Step 6 — for now
    the endpoint accepts the file and returns an empty transcript stub so the
    UI flow is exercised end-to-end.
    """
    VOICE_MAX = 25 * 1024 * 1024   # 25 MB — generous for < 5-min clips

    form   = await request.form()
    upload: Optional[UploadFile] = form.get("audio")  # type: ignore[assignment]

    if upload is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No audio attached. Send a multipart field named 'audio'.")

    data = await upload.read(VOICE_MAX + 1)
    if len(data) > VOICE_MAX:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Audio file too large.")

    log.info("chat.voice user=%s size=%d", _mask_email(user["email"]), len(data))

    from services.voice_service import transcribe_bytes
    transcript = await transcribe_bytes(data)

    if not transcript:
        return JSONResponse(
            {"success": False,
             "error": "Couldn't catch that — try typing instead."},
            status_code=200,
        )

    # Return the transcript to the frontend — the user reviews it and sends
    # via the normal text path, avoiding double-injection into the flow.
    return JSONResponse({"success": True, "transcript": transcript})


# ── Razorpay Checkout (web) ──────────────────────────────────────────────────

@app.post("/api/payment/create-order")
async def api_payment_create_order(
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Create a Razorpay order and return its id + key for Checkout.js."""
    if not (settings.razorpay_key_id and settings.razorpay_key_secret):
        raise HTTPException(status_code=503, detail="Payment service not configured.")

    amount = settings.price_subscription_paise
    try:
        import razorpay as _rzp
        client = _rzp.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        order = await asyncio.to_thread(client.order.create, {
            "amount":   amount,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {
                "user_email":    user["email"],
                "payment_type":  "access_pass",
                "duration_days": "60",
            },
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("create-order failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not create payment order.")

    order_id = order.get("id") or ""
    # Intent is recorded at verify time when we have a confirmed phone.
    log.info("payment.create-order user=%s order=%s", _mask_email(user["email"]), order_id)
    return JSONResponse({
        "order_id": order_id,
        "amount":   amount,
        "currency": "INR",
        "key_id":   settings.razorpay_key_id,
    })


@app.post("/api/payment/verify")
async def api_payment_verify(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Verify Razorpay Checkout signature and activate the subscription."""
    body: dict[str, Any] = await request.json()
    order_id   = body.get("razorpay_order_id", "")
    payment_id = body.get("razorpay_payment_id", "")
    signature  = body.get("razorpay_signature", "")
    phone      = (body.get("phone") or "").strip()

    if not (order_id and payment_id and signature):
        raise HTTPException(status_code=422, detail="Missing required payment fields.")

    # HMAC-SHA256: message = order_id + "|" + payment_id
    import hmac as _hmac, hashlib as _hashlib
    secret = (settings.razorpay_key_secret or "").encode("utf-8")
    expected = _hmac.new(
        secret,
        f"{order_id}|{payment_id}".encode("utf-8"),
        _hashlib.sha256,
    ).hexdigest()

    if not _hmac.compare_digest(expected, signature):
        log.warning("payment.verify sig mismatch user=%s", _mask_email(user["email"]))
        raise HTTPException(status_code=400, detail="Payment signature verification failed.")

    # Attach phone to the user record if provided (optional — collected at checkout).
    if phone:
        await upsert_user(email=user["email"], phone=phone)

    # Always record payment keyed by email — no phone FK required in new schema.
    await record_payment_intent(
        user_id      = user["email"],
        amount_paise = settings.price_subscription_paise,
        link_id      = order_id,
        payment_type = "access_pass",
    )
    await mark_subscription_active(
        user_id             = user["email"],
        duration_days       = 60,
        razorpay_payment_id = payment_id,
        link_id             = order_id,
        payment_type        = "access_pass",
    )

    user_key = user["email"]

    log.info("payment.verify activated user=%s payment=%s", _mask_email(user["email"]), payment_id)

    # Resume any pending flow output (e.g. resume PDF queued behind paywall).
    try:
        await _resume_pending_output(user_key)
    except Exception:  # noqa: BLE001
        log.exception("resume_pending_output failed for %s", _mask_email(user["email"]))

    return JSONResponse({"success": True})


# ── File URL refresh ─────────────────────────────────────────────────────────

@app.get("/api/files/refresh-url", response_model=None)
async def api_files_refresh_url(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Return a fresh 15-minute signed URL for a Supabase Storage object.

    Query param: ``path`` — the storage_path returned in the document event.
    Ownership is not verified beyond session auth (all paths are user-scoped
    by the WebMessenger which prefixes with email/).
    """
    path = request.query_params.get("path", "").strip()
    if not path:
        raise HTTPException(status_code=422, detail="Missing 'path' query parameter.")

    from services.storage_service import StorageService
    storage = StorageService()
    try:
        url = await storage.create_signed_url(path)
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh-url failed path=%s: %s", path, exc)
        raise HTTPException(status_code=502, detail="Could not generate signed URL.")

    return JSONResponse({"url": url})


# ── User history + state (for chat init) ─────────────────────────────────────

# Step labels for the progress indicator.
# Maps (flow, step) → (display_name, step_number, total_steps)
_FLOW_STEP_META: dict[tuple[str, str], tuple[str, int, int]] = {
    # Resume flow — 6 user-facing steps, then 2 processing stages
    ("resume", "welcome"):            ("Resume", 0, 6),
    ("resume", "resume_q1"):          ("Resume", 1, 6),
    ("resume", "resume_q2"):          ("Resume", 2, 6),
    ("resume", "resume_q3"):          ("Resume", 3, 6),
    ("resume", "resume_q4"):          ("Resume", 4, 6),
    ("resume", "resume_q5"):          ("Resume", 5, 6),
    ("resume", "resume_processing1"): ("Resume", 5, 6),
    ("resume", "resume_q6"):          ("Resume", 6, 6),
    ("resume", "resume_processing2"): ("Resume", 6, 6),
    # Interview flow
    ("interview", "welcome"):          ("Interview Prep", 0, 6),
    ("interview", "await_role"):       ("Interview Prep", 1, 6),
    ("interview", "await_company"):    ("Interview Prep", 2, 6),
    ("interview", "await_jd"):         ("Interview Prep", 3, 6),
    ("interview", "q1_round"):         ("Interview Prep", 4, 6),
    ("interview", "q2_prior"):         ("Interview Prep", 4, 6),
    ("interview", "q3_interviewer"):   ("Interview Prep", 5, 6),
    ("interview", "q4_concern"):       ("Interview Prep", 6, 6),
    ("interview", "prep_generating"):  ("Interview Prep", 6, 6),
    ("interview", "awaiting_payment"): ("Interview Prep", 6, 6),
    ("interview", "prep_delivered"):   ("Interview Prep", 6, 6),
    ("interview", "mock_in_progress"): ("Mock Interview", 1, 1),
    ("interview", "mock_feedback"):    ("Mock Interview", 1, 1),
}


@app.get("/api/user/history", response_model=None)
async def api_user_history(
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Return the last 50 conversation turns for the current user.

    Messages are keyed by email for web users. Returns a list of
    {role, content, created_at} objects ordered oldest-first.
    """
    from models.database import recent_conversation
    msgs = await recent_conversation(user["email"], limit=50)
    return JSONResponse({"messages": msgs})


@app.get("/api/user/state", response_model=None)
async def api_user_state(
    user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Return the current flow/step and a formatted progress label.

    Response: {flow, step, label, step_num, total_steps}
    ``label`` is e.g. "Resume · Step 3 of 6" or "" when idle.
    """
    state = await get_user_state(user["email"])
    flow  = state.get("flow") or "idle"
    step  = state.get("resume_step") if flow == "resume" else state.get("interview_step") or ""

    meta = _FLOW_STEP_META.get((flow, step))
    if meta and meta[1] > 0:
        name, step_num, total = meta
        label = f"{name} · Step {step_num} of {total}"
    else:
        label = ""

    return JSONResponse({
        "flow":        flow,
        "step":        step,
        "label":       label,
        "step_num":    meta[1] if meta else 0,
        "total_steps": meta[2] if meta else 0,
    })


# ── WebSocket chat ───────────────────────────────────────────────────────────

# email -> WebSocket. One connection per user; a new connect replaces the old.
_WS_CONNECTIONS: dict[str, WebSocket] = {}

_WS_PING_INTERVAL = 30   # seconds between keepalive pings


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """Real-time chat endpoint for web users.

    Authentication: reads the ``vima_session`` httponly cookie from the
    WebSocket upgrade request headers and validates it. Closes with code 4001
    on failure.

    Once authenticated the handler runs two concurrent tasks:
      - ``_ws_receive``:  reads messages from the browser and routes them.
      - ``_ws_drain``:    drains the WebMessenger queue and forwards events.

    A ping is sent every 30 s to keep the connection alive through proxies.
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    cookies_header = websocket.headers.get("cookie", "")
    session_token  = _parse_cookie(cookies_header, _SESSION_COOKIE)

    if not session_token:
        await websocket.close(code=4001)
        return

    email = await validate_session(session_token)
    if not email:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    _WS_CONNECTIONS[email] = websocket
    log.info("ws.connect email=%s", _mask_email(email))

    # Derive first name from the user's display name for personalised replies.
    from models.database import get_user_by_email as _get_user
    _user_row = await _get_user(email) or {}
    _full_name = (_user_row.get("name") or email.split("@")[0]).strip()
    first_name = (_full_name.split()[0] if _full_name else "").strip()

    # Ensure the user's WebMessenger queue exists before starting the drain.
    from services.web_messenger import _queue_for
    queue = await _queue_for(email)

    receive_task = asyncio.create_task(_ws_receive(websocket, email, first_name), name=f"ws.recv.{email[:8]}")
    drain_task   = asyncio.create_task(_ws_drain(websocket, queue),   name=f"ws.drain.{email[:8]}")
    ping_task    = asyncio.create_task(_ws_ping(websocket),           name=f"ws.ping.{email[:8]}")

    try:
        # Wait until one task ends (disconnect, error, or server shutdown).
        await asyncio.wait(
            {receive_task, drain_task, ping_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (receive_task, drain_task, ping_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        _WS_CONNECTIONS.pop(email, None)
        log.info("ws.disconnect email=%s", _mask_email(email))


async def _ws_receive(websocket: WebSocket, email: str, first_name: str) -> None:
    """Read messages from the browser and route them through the flow engine."""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await _ws_send(websocket, {"type": "error", "message": "Invalid JSON."})
            continue

        msg_type = data.get("type", "message")

        if msg_type != "message":
            continue  # ignore pong, ack, etc.

        text = (data.get("text") or "").strip()
        if not text:
            continue

        try:
            await route_web_message(email, text, first_name=first_name)
        except Exception:  # noqa: BLE001
            log.exception("ws.route_error email=%s", _mask_email(email))
            await _ws_send(websocket, {
                "type":    "error",
                "message": "Something went wrong, please try again.",
            })


async def _ws_drain(
    websocket: WebSocket,
    queue: "asyncio.Queue[dict]",
) -> None:
    """Forward events from the WebMessenger queue to the browser."""
    while True:
        try:
            event = await queue.get()
        except asyncio.CancelledError:
            return
        await _ws_send(websocket, event)


async def _ws_ping(websocket: WebSocket) -> None:
    """Send a keepalive ping every _WS_PING_INTERVAL seconds."""
    while True:
        await asyncio.sleep(_WS_PING_INTERVAL)
        await _ws_send(websocket, {"type": "ping"})


async def _ws_send(websocket: WebSocket, event: dict) -> None:
    """Send a JSON event, swallowing send errors after disconnect."""
    try:
        await websocket.send_text(json.dumps(event))
    except Exception:  # noqa: BLE001
        pass  # connection already closed; drain/ping task will exit next iteration


def _parse_cookie(cookie_header: str, name: str) -> Optional[str]:
    """Extract a single cookie value from a raw Cookie header string."""
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() == name:
                return v.strip()
    return None


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked = local[:2] + "***" if len(local) > 2 else "***"
    return f"{masked}@{domain}"


# ── Gupshup webhook ─────────────────────────────────────────────────────────
@app.post("/webhooks/gupshup")
async def gupshup_webhook(request: Request) -> JSONResponse:
    """Handle inbound WhatsApp messages and delivery events from Gupshup.

    Always returns 200 — Gupshup retries on non-2xx, which causes duplicate
    deliveries. We log + swallow errors instead.
    """
    try:
        raw = await request.body()
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        log.warning("Gupshup webhook received non-JSON body; ignoring.")
        return JSONResponse({"status": "ignored", "reason": "invalid_json"})

    outer_type = payload.get("type", "")
    log.info("Gupshup event: type=%s", outer_type)

    # Status / delivery events: ack and move on.
    if outer_type != "message":
        return JSONResponse({"status": "ignored", "reason": f"type={outer_type}"})

    # Duplicate-delivery protection — Gupshup occasionally retries a webhook.
    inner = (payload.get("payload") or {})
    msg_id = inner.get("id")
    if _seen_message(msg_id):
        log.info("dedup: skipping repeat msg_id=%s", msg_id)
        return JSONResponse({"status": "duplicate", "msg_id": msg_id})

    # Per-user rate limit (30 msgs / 60s).
    sender_for_rate = _extract_sender(payload)
    if sender_for_rate and _rate_limited(sender_for_rate):
        log.warning("rate_limit hit phone=%s", _mask(sender_for_rate))
        try:
            messenger = await get_messenger(sender_for_rate)
            await messenger.send_text(
                sender_for_rate,
                "Slow down a bit, I'm processing your last message!",
            )
        except Exception:  # noqa: BLE001
            log.exception("rate-limit reply send failed")
        return JSONResponse({"status": "rate_limited"})

    try:
        reply = await _handle_message(payload)
    except Exception:  # noqa: BLE001
        log.exception("Error handling inbound message")
        return JSONResponse({"status": "error_logged"}, status_code=status.HTTP_200_OK)

    if reply is not None:
        try:
            messenger = await get_messenger(reply.get("to") or sender_for_rate or "")
            await messenger.send(reply)
            log.debug("messenger send dispatched type=%s", reply.get("type"))
        except Exception:  # noqa: BLE001
            log.exception("Error sending reply via Gupshup")

    return JSONResponse({"status": "received"})


async def _handle_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Echo-mode shortcut, or full router dispatch."""
    sender  = _extract_sender(payload)
    message = _extract_message(payload)

    if not sender or message is None:
        log.warning("Could not parse sender or message from payload.")
        return None

    log.info(
        "Inbound msg: from=%s type=%s text=%r",
        _mask(sender), message.get("type"), (message.get("text") or "")[:80],
    )

    if settings.echo_mode:
        return _echo_reply(sender, message)

    # Real path: persist user, hand off to the flow router. Users arriving
    # via the Gupshup webhook are tagged channel='whatsapp' so the messenger
    # factory routes their outbound sends through Gupshup.
    sender_name = _extract_sender_name(payload)
    try:
        await upsert_user(sender, name=sender_name, channel="whatsapp")
    except Exception as exc:  # noqa: BLE001
        log.warning("upsert_user failed (%s); continuing anyway.", exc)

    return await route_message(payload)


def _echo_reply(sender: str, message: dict[str, Any]) -> dict[str, Any]:
    """Build a friendly ViMa-flavoured echo response for the wiring test."""
    msg_type = message.get("type", "unknown")
    text     = (message.get("text") or "").strip()

    greeting = "Hi, I'm *ViMa* — your AI career coach. 👋"

    if msg_type == "text" and text:
        body = (
            f"{greeting}\n\n"
            f"You said: _{text}_\n\n"
            "I'll have full coaching, resume rewrites, and mock interviews ready "
            "very soon. For now, this is just a wiring test."
        )
    elif msg_type == "audio":
        body = f"{greeting}\n\nGot your voice note — voice replies coming soon."
    elif msg_type == "document":
        body = f"{greeting}\n\nGot your document. Resume parsing is on the way."
    elif msg_type == "image":
        body = f"{greeting}\n\nGot your image. Thanks for trying ViMa."
    else:
        body = f"{greeting}\n\nGot your message. Reply with text to chat."

    return {"to": sender, "type": "text", "text": body}


# ── Razorpay webhook ────────────────────────────────────────────────────────
@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    """Handle Razorpay payment lifecycle events.

    Always returns 200 — even on signature mismatch or processing error —
    so Razorpay doesn't retry. Failures are logged.
    """
    raw = await request.body()
    signature = (
        request.headers.get("x-razorpay-signature")
        or request.headers.get("X-Razorpay-Signature")
        or ""
    )

    payments = PaymentService()
    if not payments.verify_webhook_signature(raw, signature):
        log.warning("Razorpay webhook signature invalid; ignoring.")
        return JSONResponse({"status": "ignored", "reason": "bad_signature"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        log.warning("Razorpay webhook had invalid JSON body.")
        return JSONResponse({"status": "ignored", "reason": "invalid_json"})

    event = payload.get("event", "")
    log.info("Razorpay event: %s", event)

    try:
        result = await payments.handle_webhook(payload)
    except Exception:  # noqa: BLE001
        log.exception("Razorpay webhook processing failed.")
        return JSONResponse({"status": "error_logged"})

    # Events we don't handle — ack and move on.
    if not result:
        return JSONResponse({"status": "received"})

    kind  = result.get("kind")
    phone = result.get("phone", "")

    if kind == "activated":
        # Pass activated. Resume any paused output for this user.
        try:
            await _resume_pending_output(phone)
        except Exception:  # noqa: BLE001
            log.exception("Resuming pending output failed for %s", _mask(phone))
        return JSONResponse({"status": "received", "activated": True})

    if kind == "renewed":
        # Expired or failed payment — send the renewal link to the user.
        body = result.get("outbound_text") or ""
        if phone and body:
            try:
                messenger = await get_messenger(phone)
                await messenger.send_text(phone, body)
            except Exception:  # noqa: BLE001
                log.exception("Renewal link send failed for %s", _mask(phone))
        return JSONResponse({"status": "received", "renewed": True})

    return JSONResponse({"status": "received"})


# ── Admin — manual beta activation ──────────────────────────────────────────
# TODO: remove this endpoint once Razorpay production keys are live and all
# access-pass activation flows through /api/payment/verify.

@app.post("/api/admin/activate-user")
async def api_admin_activate_user(request: Request) -> JSONResponse:
    """Manually activate a user's 60-day access pass.

    Body: {email, duration_days (default 60), admin_secret}
    """
    body: dict[str, Any] = await request.json()
    admin_secret = (body.get("admin_secret") or "").strip()
    expected     = (settings.admin_secret or "").strip()

    if not expected or not admin_secret or not secrets.compare_digest(expected, admin_secret):
        raise HTTPException(status_code=403, detail="Invalid admin secret.")

    email        = (body.get("email") or "").strip().lower()
    duration     = int(body.get("duration_days") or 60)

    if not email:
        raise HTTPException(status_code=422, detail="email is required.")

    import datetime as _dt
    await mark_subscription_active(
        user_id             = email,
        duration_days       = duration,
        razorpay_payment_id = None,
        link_id             = None,
        payment_type        = "access_pass",
    )

    period_end = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=duration)
    ).isoformat()

    log.info("admin.activate email=%s duration=%d", _mask_email(email), duration)

    resumed = False
    try:
        resumed = await _resume_pending_output(email)
    except Exception:  # noqa: BLE001
        log.exception("admin.activate resume_pending failed for %s", _mask_email(email))

    if not resumed:
        # No pending flow to resume (or resumption failed) — send the user a
        # direct notification so they know their access is live.
        try:
            messenger = await get_messenger(email)
            await messenger.send_text(
                email,
                "Your access is now active! Type *menu* to continue.",
            )
        except Exception:  # noqa: BLE001
            log.warning("admin.activate fallback notify failed for %s", _mask_email(email))

    return JSONResponse({"success": True, "user": email, "period_end": period_end})


async def _resume_pending_output(user_id: str) -> bool:
    """Resume a flow paused awaiting payment. Returns True if output was delivered."""
    state = await get_user_state(user_id)
    data  = state.get("data") or {}
    pending = data.get("pending_action")
    if not pending:
        return False

    from flows import resume    as resume_flow
    from flows import interview as interview_flow

    log.info("Resuming pending output for %s: %s", _mask(user_id), pending)

    fake_msg: dict[str, Any] = {"type": "text", "text": ""}

    reply: Optional[dict[str, Any]] = None

    if pending == "resume_proc2":
        await upsert_user_state(
            user_id, {"flow": "resume", "resume_step": resume_flow.RESUME_PROC2}
        )
        st = await get_user_state(user_id)
        reply = await resume_flow.handle(user_id, fake_msg, st)

    elif pending == "interview_prep":
        await upsert_user_state(
            user_id, {"flow": "interview", "interview_step": interview_flow.PREP_GENERATING}
        )
        st = await get_user_state(user_id)
        reply = await interview_flow.handle(user_id, fake_msg, st)

    else:
        log.warning("Unknown pending_action %r for %s", pending, _mask(user_id))
        return False

    await merge_user_state_data(user_id, {"pending_action": None})

    if reply is not None:
        messenger = await get_messenger(user_id)
        await messenger.send(reply)

    return True


# ── Helpers ─────────────────────────────────────────────────────────────────
def _mask(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"


# ── Entry ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    import uvicorn

    # Railway injects PORT at runtime; honour it. Local dev falls back to 8000.
    port = int(os.environ.get("PORT", settings.port))
    is_prod = settings.app_env.lower() == "production"

    # Production: 2 workers minimum, scale via WEB_CONCURRENCY (Railway / Heroku
    # convention). Dev: single process with --reload for hot reloads.
    workers = int(os.environ.get("WEB_CONCURRENCY", "2")) if is_prod else 1
    reload  = (not is_prod) and settings.debug

    log.info(
        "Boot uvicorn host=0.0.0.0 port=%d workers=%d reload=%s env=%s",
        port, workers, reload, settings.app_env,
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        workers=None if reload else workers,  # --reload incompatible with workers
    )

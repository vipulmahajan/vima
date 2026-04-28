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
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import settings
from flows.router import (
    route_message,
    _extract_sender,
    _extract_sender_name,
    _extract_message,
)
from services.whatsapp_service import WhatsAppService
from services.payment_service import PaymentService
from models.database import (
    init_db, close_db, upsert_user,
    get_user_state, upsert_user_state, merge_user_state_data,
    list_active_user_states, archive_user_state,
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
        phone = s.get("phone") or ""
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
            async with WhatsAppService() as wa:
                await wa.send_text_message(phone, body)
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
            async with WhatsAppService() as wa:
                await wa.send_text_message(
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
            async with WhatsAppService() as wa:
                send_result = await wa.send(reply)
                log.debug("Gupshup send result: %s", send_result)
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

    # Real path: persist user, hand off to the flow router.
    sender_name = _extract_sender_name(payload)
    try:
        await upsert_user(sender, name=sender_name)
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
                async with WhatsAppService() as wa:
                    await wa.send_text_message(phone, body)
            except Exception:  # noqa: BLE001
                log.exception("Renewal link send failed for %s", _mask(phone))
        return JSONResponse({"status": "received", "renewed": True})

    return JSONResponse({"status": "received"})


async def _resume_pending_output(phone: str) -> None:
    """If the user has a flow paused awaiting payment, resume it now."""
    state = await get_user_state(phone)
    data  = state.get("data") or {}
    pending = data.get("pending_action")
    if not pending:
        return

    # Lazy-import to avoid circular imports at module load.
    from flows import resume    as resume_flow
    from flows import interview as interview_flow

    log.info("Resuming pending output for %s: %s", _mask(phone), pending)

    # Build a synthetic empty inbound message — handlers expect one.
    fake_msg: dict[str, Any] = {"type": "text", "text": ""}

    reply: Optional[dict[str, Any]] = None

    if pending == "resume_proc2":
        await upsert_user_state(
            phone, {"flow": "resume", "resume_step": resume_flow.RESUME_PROC2}
        )
        st = await get_user_state(phone)
        reply = await resume_flow.handle(phone, fake_msg, st)

    elif pending == "interview_prep":
        await upsert_user_state(
            phone, {"flow": "interview", "interview_step": interview_flow.PREP_GENERATING}
        )
        st = await get_user_state(phone)
        reply = await interview_flow.handle(phone, fake_msg, st)

    else:
        log.warning("Unknown pending_action %r for %s", pending, _mask(phone))
        return

    # Clear the pending marker now that we've kicked off the resumption.
    await merge_user_state_data(phone, {"pending_action": None})

    if reply is not None:
        async with WhatsAppService() as wa:
            await wa.send(reply)


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

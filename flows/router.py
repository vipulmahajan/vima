"""Inbound message router.

Parses the Gupshup v2 webhook payload, normalises message types into a
common dict, resolves user state from Supabase, and dispatches to the
correct flow handler.

Gupshup v2 inbound shape (type == "message"):
  {
    "app": "<app_name>",
    "timestamp": 1234567890123,
    "version": 2,
    "type": "message",
    "payload": {
      "id": "<msg_id>",
      "source": "919xxxxxxxxx",
      "type": "text" | "audio" | "file" | "image" | "location" | ...,
      "payload": {                    # varies by type
        "text": "...",                # type=text
        "url": "https://...",         # type=audio|file|image
        "caption": "...",             # type=file|image
        "filename": "resume.pdf",     # type=file
        "content-type": "...",        # type=file (MIME)
      },
      "sender": {
        "phone": "919xxxxxxxxx",
        "name": "...",
        "country_code": "91",
      }
    }
  }

Delivery / status events have outer type != "message" and are ignored.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from models.database import (
    get_user_state,
    upsert_user_state,
    upsert_user,
    merge_user_state_data,
)
from flows import resume as resume_flow
from flows import interview as interview_flow
from services.claude_service import ClaudeService

log = logging.getLogger(__name__)

# ── Flow state constants ────────────────────────────────────────────────────
STATE_IDLE      = "idle"
STATE_RESUME    = "resume"
STATE_INTERVIEW = "interview"

# Text the user can send at any point to reset / see the menu.
RESET_TRIGGERS = {"menu", "hi", "hello", "start", "start over", "reset", "help", "/start"}


# ── Entry point ─────────────────────────────────────────────────────────────

async def route_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse a Gupshup webhook payload and return the outbound reply (or None)."""
    outer_type = payload.get("type", "")

    # Ignore delivery receipts, reactions, read events, etc.
    if outer_type != "message":
        return None

    sender  = _extract_sender(payload)
    message = _extract_message(payload)

    if not sender or message is None:
        log.warning("Unrecognised Gupshup payload — could not extract sender/message.")
        return None

    # Ensure user row exists (idempotent).
    sender_name = _extract_sender_name(payload)
    await upsert_user(sender, name=sender_name)

    state   = await get_user_state(sender)
    current = state.get("flow", STATE_IDLE)
    text    = (message.get("text") or "").strip()

    # ── Hard reset / menu ───────────────────────────────────────────────────
    if text.lower() in RESET_TRIGGERS:
        await upsert_user_state(sender, {"flow": STATE_IDLE})
        await merge_user_state_data(sender, {"awaiting_stage": True})
        return _menu_reply(sender)

    # ── Explicit flow switches from within idle state ────────────────────────
    intent = _detect_intent(text)

    if current == STATE_IDLE:
        # If the menu was just shown, try to parse a stage selection first.
        data = state.get("data") or {}
        if data.get("awaiting_stage"):
            stage = _parse_stage_choice(text)
            if stage is not None:
                return await _dispatch_stage(sender, stage, message)
            # Couldn't parse — re-show the menu so the user knows their options.
            return _menu_reply(sender)

        if intent == STATE_RESUME:
            return await _enter_resume(sender, message, skip_welcome=False)

        if intent == STATE_INTERVIEW:
            return await _enter_interview(sender, message)

        # First contact (no awaiting_stage flag set yet, no obvious intent):
        # show the stage selector instead of a free-form Claude reply.
        if not data:
            await merge_user_state_data(sender, {"awaiting_stage": True})
            return _menu_reply(sender)

        # Free-form coaching for returning users.
        claude = ClaudeService()
        reply_text = await claude.chat_with_persona(sender, text or _media_fallback(message))
        return _text_reply(sender, reply_text)

    # ── Continue an active flow ─────────────────────────────────────────────
    # Allow mid-flow intent switch if user explicitly names the other flow.
    if current == STATE_RESUME and intent == STATE_INTERVIEW:
        return await _enter_interview(sender, message)

    if current == STATE_INTERVIEW and intent == STATE_RESUME:
        return await _enter_resume(sender, message, skip_welcome=False)

    if current == STATE_RESUME:
        reply = await resume_flow.handle(sender, message, state)
        return reply

    if current == STATE_INTERVIEW:
        reply = await interview_flow.handle(sender, message, state)
        return reply

    # Fallback — should not normally reach here.
    await upsert_user_state(sender, {"flow": STATE_IDLE})
    return _menu_reply(sender)


# ── Payload parsing ─────────────────────────────────────────────────────────

def _extract_sender(payload: dict[str, Any]) -> Optional[str]:
    """Return the sender's WhatsApp number in E.164 form, or None."""
    try:
        # Prefer the nested sender block; fall back to source.
        sender = (
            payload["payload"]["sender"].get("phone")
            or payload["payload"].get("source")
        )
        return str(sender).strip() if sender else None
    except (KeyError, TypeError):
        return None


def _extract_sender_name(payload: dict[str, Any]) -> Optional[str]:
    try:
        return payload["payload"]["sender"].get("name") or None
    except (KeyError, TypeError):
        return None


def _extract_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalise the inner payload into a common message dict.

    Returns:
        {
          "type": "text" | "audio" | "document" | "image" | "unknown",
          "text": str | None,
          "media_url": str | None,
          "mime_type": str | None,
          "filename": str | None,
          "caption": str | None,
        }
    or None if the payload shape is unrecognised.
    """
    try:
        outer = payload["payload"]
        msg_type = outer.get("type", "")
        inner    = outer.get("payload") or {}

        base: dict[str, Any] = {
            "type":      msg_type,
            "text":      None,
            "media_url": None,
            "mime_type": None,
            "filename":  None,
            "caption":   None,
        }

        if msg_type == "text":
            base["text"] = (inner.get("text") or "").strip()

        elif msg_type == "audio":
            base["media_url"] = inner.get("url")

        elif msg_type == "file":
            base["media_url"] = inner.get("url")
            base["mime_type"] = inner.get("content-type")
            base["filename"]  = inner.get("filename")
            base["caption"]   = inner.get("caption")
            # Treat document type as "document" for downstream logic.
            base["type"] = "document"

        elif msg_type == "image":
            base["media_url"] = inner.get("url")
            base["caption"]   = inner.get("caption")

        else:
            # Location, contact, sticker, etc. — pass through as unknown.
            base["type"] = "unknown"

        return base

    except (KeyError, TypeError):
        return None


# ── Intent detection ────────────────────────────────────────────────────────

def _detect_intent(text: str) -> Optional[str]:
    """Lightweight keyword intent detection.  Claude handles ambiguous cases."""
    t = text.lower()
    resume_kw    = {"resume", "cv", "rewrite", "update my resume", "update resume"}
    interview_kw = {"interview", "mock", "practice interview", "mock interview", "prep"}

    if any(k in t for k in resume_kw):
        return STATE_RESUME
    if any(k in t for k in interview_kw):
        return STATE_INTERVIEW
    return None


# ── Reply helpers ───────────────────────────────────────────────────────────

def _text_reply(to: str, text: str) -> dict[str, Any]:
    return {"to": to, "type": "text", "text": text}


def _media_fallback(message: dict[str, Any]) -> str:
    """Return a text prompt to Claude when the user sends a non-text message."""
    kind = message.get("type", "unknown")
    if kind == "audio":
        return "[User sent a voice note — respond that you received it and ask them to type their query for now.]"
    if kind == "document":
        return "[User sent a document — respond that you received it and ask what they'd like to do with it.]"
    if kind == "image":
        return "[User sent an image — respond that you received it and ask what they'd like to do with it.]"
    return "[User sent an unsupported message type — ask them to type their query.]"


def _menu_reply(to: str) -> dict[str, Any]:
    text = (
        "Hi, I'm *ViMa* — your AI career coach.\n\n"
        "Where are you in your career transition? Reply with the number:\n\n"
        "*1.* Searching for roles\n"
        "*2.* Creating a custom resume\n"
        "*3.* Preparing for interview\n"
        "*4.* Negotiating an offer\n"
        "*5.* First 90 days planning\n\n"
        "All features are included in the ViMa subscription (₹1,799/month)."
    )
    return _text_reply(to, text)


# ── Stage selection ─────────────────────────────────────────────────────────

# Stage codes we persist into user_state.data.interest_signals.
STAGE_SEARCHING   = "searching"
STAGE_RESUME      = "resume"
STAGE_INTERVIEW   = "interview"
STAGE_NEGOTIATION = "negotiation"
STAGE_FIRST_90    = "first_90_days"

_STAGE_KEYWORDS: dict[str, str] = {
    "1": STAGE_SEARCHING,
    "search": STAGE_SEARCHING,
    "searching": STAGE_SEARCHING,
    "job search": STAGE_SEARCHING,
    "looking": STAGE_SEARCHING,

    "2": STAGE_RESUME,
    "resume": STAGE_RESUME,
    "cv": STAGE_RESUME,
    "custom resume": STAGE_RESUME,

    "3": STAGE_INTERVIEW,
    "interview": STAGE_INTERVIEW,
    "preparing for interview": STAGE_INTERVIEW,
    "prep": STAGE_INTERVIEW,

    "4": STAGE_NEGOTIATION,
    "negotiation": STAGE_NEGOTIATION,
    "negotiating": STAGE_NEGOTIATION,
    "offer": STAGE_NEGOTIATION,

    "5": STAGE_FIRST_90,
    "first 90": STAGE_FIRST_90,
    "first 90 days": STAGE_FIRST_90,
    "90 days": STAGE_FIRST_90,
    "onboarding": STAGE_FIRST_90,
}

_COMING_SOON_LABELS = {
    STAGE_SEARCHING:   "Searching for roles",
    STAGE_NEGOTIATION: "Negotiating an offer",
    STAGE_FIRST_90:    "First 90 days planning",
}


def _parse_stage_choice(text: str) -> Optional[str]:
    """Map a user reply to a stage code, or None if unrecognised."""
    if not text:
        return None
    t = text.strip().lower()
    # Strip leading punctuation people often type ("1.", "1)", "(1)" ...).
    t_clean = t.lstrip("(").rstrip(").: ").strip()
    if t_clean in _STAGE_KEYWORDS:
        return _STAGE_KEYWORDS[t_clean]
    # Substring match for free-text replies like "creating a custom resume please".
    for key, code in _STAGE_KEYWORDS.items():
        if len(key) >= 4 and key in t:
            return code
    return None


async def _dispatch_stage(
    sender: str,
    stage: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    """Route a parsed stage to the right flow or save interest."""
    # Clear the awaiting-stage flag and record the chosen stage either way.
    data = await merge_user_state_data(sender, {
        "awaiting_stage": False,
        "chosen_stage": stage,
    })

    if stage == STAGE_RESUME:
        return await _enter_resume(sender, message, skip_welcome=True)

    if stage == STAGE_INTERVIEW:
        return await _enter_interview(sender, message)

    # 1, 4, 5 → coming soon. Append to interest_signals (deduped).
    interests: list[str] = list(data.get("interest_signals") or [])
    if stage not in interests:
        interests.append(stage)
        await merge_user_state_data(sender, {"interest_signals": interests})

    label = _COMING_SOON_LABELS.get(stage, "That stage")
    return _text_reply(
        sender,
        f"*{label}* — Coming soon. I'll notify you when this is ready!\n\n"
        "Meanwhile, reply *2* to build a tailored resume or *3* to run a mock "
        "interview. Reply *menu* anytime to see all options."
    )


# ── Flow entry helpers ─────────────────────────────────────────────────────

async def _enter_resume(
    sender: str,
    message: dict[str, Any],
    *,
    skip_welcome: bool,
) -> dict[str, Any]:
    """Switch to the resume flow and dispatch its first turn.

    When `skip_welcome=True` (user just picked 'resume' from the top-level
    stage selector), we land directly at Q1 instead of showing the resume
    flow's own intra-flow welcome screen.
    """
    first_step = resume_flow.RESUME_Q1 if skip_welcome else resume_flow.INITIAL_STEP
    await upsert_user_state(
        sender, {"flow": STATE_RESUME, "resume_step": first_step}
    )
    state = await get_user_state(sender)
    return await resume_flow.handle(sender, message, state)


async def _enter_interview(
    sender: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    await upsert_user_state(
        sender,
        {"flow": STATE_INTERVIEW, "interview_step": interview_flow.INITIAL_STEP},
    )
    state = await get_user_state(sender)
    return await interview_flow.handle(sender, message, state)

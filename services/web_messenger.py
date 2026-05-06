"""Web channel messenger backed by per-user asyncio.Queue.

The web channel is consumed by a WebSocket endpoint (Step 3 in the
channel-agnostic plan). Until that endpoint lands, this module's role is
purely to *accept* outbound messages from flows and store them so the
WebSocket consumer can drain them.

Each message is a normalised event dict:

    {"type": "text",     "text": "..."}                        # send_text
    {"type": "typing"}                                          # typing_indicator
    {"type": "document", "url": "...", "filename": "...",
     "caption": "..."}                                          # send_document
    {"type": "payment",  "order_id": "...", "amount": 179900,
     "description": "...", "payment_link": "..."}              # send_payment_request
    {"type": "quick_replies", "text": "...",
     "options": [{"id": "...", "title": "..."}, ...]}          # send_quick_replies

Documents are uploaded to Supabase Storage exactly the same way the
WhatsApp messenger does — the difference is that the *URL itself* is what
the web frontend needs (and gets, via the queue event), rather than the
URL being delivered through Gupshup as an out-of-band attachment.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from config import settings
from services.messenger import Messenger, QuickReplyOption, _guess_content_type
from services.storage_service import StorageService

log = logging.getLogger(__name__)


# ── Per-user queue registry ────────────────────────────────────────────────

# user_id -> asyncio.Queue[event]. Bounded so a runaway flow can't OOM us;
# 256 events per user is generous (a full PROC2 turn is ~5 events).
_QUEUE_MAXSIZE = 256

_USER_QUEUES: dict[str, "asyncio.Queue[dict[str, Any]]"] = {}
_QUEUE_LOCK = asyncio.Lock()


async def _queue_for(user_id: str) -> "asyncio.Queue[dict[str, Any]]":
    """Return (creating if necessary) the per-user event queue."""
    async with _QUEUE_LOCK:
        q = _USER_QUEUES.get(user_id)
        if q is None:
            q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            _USER_QUEUES[user_id] = q
        return q


def get_user_queue(user_id: str) -> Optional["asyncio.Queue[dict[str, Any]]"]:
    """Return the queue for ``user_id`` if one exists, else None.

    The future WebSocket consumer calls this on connect and drains events.
    Synchronous on purpose — checks-and-creates happen lazily via
    ``_queue_for`` from the messenger side.
    """
    return _USER_QUEUES.get(user_id)


# ── Web messenger ──────────────────────────────────────────────────────────

class WebMessenger(Messenger):
    """Buffer outbound events in an in-memory queue keyed by user_id."""

    channel = "web"

    def __init__(self) -> None:
        self._storage = StorageService()

    async def _push(self, user_id: str, event: dict[str, Any]) -> None:
        """Push an event onto the user's queue. Drops oldest on overflow."""
        q = await _queue_for(user_id)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event to make room — this keeps the queue
            # responsive when the WebSocket consumer is slow or absent.
            try:
                q.get_nowait()
                q.put_nowait(event)
                log.warning("WebMessenger queue full for %s; dropped oldest event", user_id)
            except Exception:  # noqa: BLE001
                log.exception("WebMessenger queue overflow recovery failed")

    # send_text ─────────────────────────────────────────────────────────────

    async def send_text(self, user_id: str, text: str) -> None:
        await self._push(user_id, {
            "type": "text",
            "text": text,
            "ts":   time.time(),
        })

    # send_document ─────────────────────────────────────────────────────────

    async def send_document(
        self,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> None:
        storage_path = f"{user_id}/{filename}"
        try:
            content_type = _guess_content_type(filename)
            url = await self._storage.upload(storage_path, file_bytes, content_type)
        except Exception as exc:  # noqa: BLE001
            log.exception("WebMessenger.send_document upload failed: %s", exc)
            raise

        await self._push(user_id, {
            "type":         "document",
            "url":          url,
            "filename":     filename,
            "storage_path": storage_path,   # used by /api/files/refresh-url
            "size":         len(file_bytes),
            "caption":      caption,
            "ts":           time.time(),
        })

    async def send_document_url(
        self,
        user_id: str,
        url: str,
        storage_path: str,
        filename: str,
        caption: Optional[str] = None,
    ) -> None:
        """Push a document event for an already-uploaded file (no re-upload)."""
        await self._push(user_id, {
            "type":         "document",
            "url":          url,
            "filename":     filename,
            "storage_path": storage_path,
            "size":         0,
            "caption":      caption,
            "ts":           time.time(),
        })

    # send_typing_indicator ─────────────────────────────────────────────────

    async def send_typing_indicator(self, user_id: str) -> None:
        await self._push(user_id, {
            "type": "typing",
            "ts":   time.time(),
        })

    # send_payment_request ──────────────────────────────────────────────────

    async def send_payment_request(
        self,
        user_id: str,
        order_id: str,
        amount: int,
        currency: str = "INR",
        key_id: str = "",
        user_name: str = "",
        user_email: str = "",
        description: str = "60-day Access Pass",
    ) -> None:
        await self._push(user_id, {
            "type":        "payment",
            "order_id":    order_id,
            "amount":      amount,
            "currency":    currency,
            "key_id":      key_id or settings.razorpay_key_id,
            "user_name":   user_name,
            "user_email":  user_email,
            "description": description,
            "ts":          time.time(),
        })

    # send_quick_replies ────────────────────────────────────────────────────

    async def send_quick_replies(
        self,
        user_id: str,
        text: str,
        options: list[QuickReplyOption],
    ) -> None:
        normalised = []
        for i, opt in enumerate(options):
            if isinstance(opt, str):
                normalised.append({"id": f"opt_{i}", "title": opt})
            else:
                normalised.append({
                    "id":    str(opt.get("id", f"opt_{i}")),
                    "title": str(opt.get("title", "")),
                })
        await self._push(user_id, {
            "type":    "quick_replies",
            "text":    text,
            "options": normalised,
            "ts":      time.time(),
        })


# ── Test / debug helper ────────────────────────────────────────────────────

def reset_for_tests() -> None:
    """Drop every queue. Used by unit tests; never call in production."""
    _USER_QUEUES.clear()

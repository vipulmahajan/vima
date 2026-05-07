"""Channel-agnostic outbound messaging.

ViMa's flows used to call ``WhatsAppService`` directly. That coupled every
flow site to a specific transport, which made it hard to add a web channel
or any future surface (Telegram, Slack, an in-app chat in the dashboard).

This module introduces the :class:`Messenger` abstract interface plus
two concrete implementations:

  - :class:`WhatsAppMessenger` — wraps the existing ``WhatsAppService``.
  - :class:`WebMessenger` — pushes messages onto a per-user in-memory queue
    that the WebSocket consumer (Step 3 in the channel-agnostic plan) reads.

All flows now go through :func:`get_messenger`, which inspects the user's
``channel`` field and returns the right concrete messenger.

Conventions:

- ``user_id`` is the canonical identifier used everywhere. For WhatsApp it
  is the phone number (E.164, no leading '+'). For web it is the session id
  the frontend sends along with each WebSocket message.
- ``send_document`` takes raw bytes — the messenger uploads to Supabase
  Storage internally and either delivers the signed URL via Gupshup
  (WhatsApp) or pushes a ``{type: "document", url: ...}`` event (web).
- ``send_payment_request`` is a *semantic* primitive: WhatsApp users get a
  text message with the link inline; web users get a structured event the
  frontend opens with Razorpay Checkout.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Union

from services.storage_service import StorageService
from services.whatsapp_service import WhatsAppService

log = logging.getLogger(__name__)


# A quick-reply option may be a plain label or a {id, title} dict.
QuickReplyOption = Union[str, dict[str, str]]


class Messenger(ABC):
    """Abstract outbound-messaging interface.

    All methods are idempotent from the caller's perspective: failures are
    logged and swallowed unless they would corrupt state. Flows treat send
    failures as non-fatal — the user can retry; the underlying state is
    preserved.
    """

    channel: str = "abstract"

    @abstractmethod
    async def send_text(self, user_id: str, text: str) -> None:
        """Send a plain text message."""

    @abstractmethod
    async def send_document(
        self,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> None:
        """Send a binary document (PDF, DOCX, etc.) to the user."""

    @abstractmethod
    async def send_typing_indicator(self, user_id: str) -> None:
        """Show a 'thinking...' / typing affordance to the user."""

    @abstractmethod
    async def send_payment_request(
        self,
        user_id: str,
        payment_link: str,
        amount: int,
        description: str,
    ) -> None:
        """Prompt the user to pay. ``amount`` is in INR paise."""

    @abstractmethod
    async def send_quick_replies(
        self,
        user_id: str,
        text: str,
        options: list[QuickReplyOption],
    ) -> None:
        """Send a message with a short list of tappable options."""

    # ── Convenience: dispatch a normalised reply dict ───────────────────────

    async def send(self, reply: dict[str, Any]) -> None:
        """Dispatch a normalised reply dict produced by a flow handler.

        This preserves backwards-compat with the old reply-dict shape that
        the router still produces. Most call sites should prefer the typed
        primitives above; ``send`` is the bridge for the router.
        """
        if not reply:
            return
        msg_type = reply.get("type", "text")
        to       = reply["to"]

        if msg_type == "text":
            await self.send_text(to, reply.get("text", ""))
            return

        if msg_type == "buttons":
            await self.send_quick_replies(
                to,
                reply.get("text", ""),
                reply.get("buttons") or [],
            )
            return

        if msg_type == "document":
            # The reply dict may carry either raw bytes (preferred) or a
            # pre-signed URL (legacy path that flows use today). Prefer bytes.
            data = reply.get("file_bytes")
            if data is None and reply.get("document_url"):
                # Legacy: the caller already uploaded and produced a URL.
                # Surface the URL via send_text since we have no bytes to
                # re-dispatch through the abstraction.
                txt = reply.get("caption") or "Document attached."
                txt += f"\n\n{reply['document_url']}"
                await self.send_text(to, txt)
                return
            if data is None:
                log.warning("send: document reply missing file_bytes/document_url")
                return
            await self.send_document(
                to,
                data,
                filename=reply.get("filename") or "vima.bin",
                caption=reply.get("caption"),
            )
            return

        if msg_type == "target_role":
            roles = reply.get("roles") or []
            if hasattr(self, "send_target_roles"):
                await self.send_target_roles(to, roles)
            else:
                # WhatsApp / unknown channel — format as plain text.
                lines = ["Here are your target role options:\n"]
                labels = ["A", "B", "C"]
                for i, r in enumerate(roles[:3]):
                    label = labels[i] if i < len(labels) else str(i + 1)
                    title = r.get("title", "")
                    sector = r.get("sector", "")
                    lines.append(f"*{label}. {title}" + (f" — {sector}" if sector else "") + "*")
                    if r.get("why_fits"):
                        lines.append(r["why_fits"])
                    lines.append("")
                lines.append("Reply *A*, *B*, or *C* to confirm your target.")
                await self.send_text(to, "\n".join(lines))
            return

        # image and other types fall through as text for now.
        log.warning("send: unsupported reply.type=%s — falling back to text", msg_type)
        await self.send_text(to, reply.get("text") or "")


# ── WhatsApp implementation ────────────────────────────────────────────────

class WhatsAppMessenger(Messenger):
    """WhatsApp via Gupshup. Wraps :class:`services.whatsapp_service.WhatsAppService`."""

    channel = "whatsapp"

    def __init__(self) -> None:
        self._wa = WhatsAppService()
        self._storage = StorageService()

    # send_text ─────────────────────────────────────────────────────────────

    async def send_text(self, user_id: str, text: str) -> None:
        try:
            await self._wa.send_text_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("WhatsAppMessenger.send_text failed: %s", exc)

    # send_document ─────────────────────────────────────────────────────────

    async def send_document(
        self,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> None:
        try:
            content_type = _guess_content_type(filename)
            url = await self._storage.upload(
                f"{user_id}/{filename}", file_bytes, content_type,
            )
            await self._wa.send_document(
                user_id, url, filename=filename, caption=caption,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("WhatsAppMessenger.send_document failed: %s", exc)
            raise

    # send_typing_indicator ─────────────────────────────────────────────────

    async def send_typing_indicator(self, user_id: str) -> None:
        # Gupshup's standard messaging API doesn't expose typing receipts.
        # We log a debug line so traces stay coherent across channels.
        log.debug("WhatsAppMessenger.send_typing_indicator no-op user=%s", _mask(user_id))

    # send_payment_request ──────────────────────────────────────────────────

    async def send_payment_request(
        self,
        user_id: str,
        payment_link: str,
        amount: int,
        description: str,
    ) -> None:
        rupees = amount / 100
        body = (
            f"{description}\n\n"
            f"Amount: ₹{rupees:,.2f}\n\n"
            f"Pay here: {payment_link}"
            if payment_link
            else f"{description}\n\nAmount: ₹{rupees:,.2f}"
        )
        await self.send_text(user_id, body)

    # send_quick_replies ────────────────────────────────────────────────────

    async def send_quick_replies(
        self,
        user_id: str,
        text: str,
        options: list[QuickReplyOption],
    ) -> None:
        if not options:
            await self.send_text(user_id, text)
            return

        # WhatsApp interactive button cap = 3. >3 → numbered text fallback.
        if len(options) <= 3:
            try:
                await self._wa.send_interactive_buttons(user_id, text, options)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("WA buttons failed (%s); falling back to numbered list", exc)

        # Numbered text fallback for >3 options OR button send failure.
        lines = [text, ""]
        for i, opt in enumerate(options, start=1):
            label = opt if isinstance(opt, str) else (opt.get("title") or str(opt))
            lines.append(f"*{i}.* {label}")
        await self.send_text(user_id, "\n".join(lines))

    # Cleanup ───────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        try:
            await self._wa.aclose()
        except Exception:  # noqa: BLE001
            pass


# ── Internal helpers ───────────────────────────────────────────────────────

_MIME_BY_EXT = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc":  "application/msword",
    "txt":  "text/plain",
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
}


def _guess_content_type(filename: str) -> str:
    if not filename or "." not in filename:
        return "application/octet-stream"
    ext = filename.rsplit(".", 1)[-1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _mask(user_id: str) -> str:
    if not user_id or len(user_id) < 6:
        return "***"
    return f"{user_id[:5]}****{user_id[-3:]}"


# ── Factory ────────────────────────────────────────────────────────────────

# Defined here (not in a separate module) so callers have one import line.
async def get_messenger(user_id: str) -> Messenger:
    """Return the right Messenger for ``user_id``.

    Looks up the user's ``channel`` field. New users default to ``"web"``;
    existing users (especially WhatsApp ones whose row was created before
    this column existed) keep their stored channel — which is exactly what
    the channel-agnostic refactor needs to preserve.
    """
    # Inline import to avoid a circular load at module init: the database
    # module pulls in config + supabase, which is fine, but the web messenger
    # imports this file — so we keep the symbol resolution lazy.
    from models.database import get_user_channel
    from services.web_messenger import WebMessenger

    channel = (await get_user_channel(user_id)) or "web"
    if channel == "whatsapp":
        return WhatsAppMessenger()
    return WebMessenger()

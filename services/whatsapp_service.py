"""Gupshup WhatsApp Business API client.

Sends text, image, document, and interactive-button messages via the Gupshup
v1 messaging endpoint, and downloads inbound media for resume PDFs and voice
notes.

Gupshup v1 endpoint:
    POST https://api.gupshup.io/sm/api/v1/msg
Headers:
    apikey: <GUPSHUP_API_KEY>
    Content-Type: application/x-www-form-urlencoded
Form fields:
    channel=whatsapp
    source=<WhatsApp business number>
    destination=<recipient phone, no '+'>
    src.name=<Gupshup app name>
    message=<JSON-encoded message body — shape varies by type>
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from config import settings

log = logging.getLogger(__name__)

GUPSHUP_BASE_URL  = "https://api.gupshup.io/sm/api/v1"
GUPSHUP_MSG_URL   = f"{GUPSHUP_BASE_URL}/msg"

# WhatsApp text body limit is 4096 chars. We split anything longer.
_MAX_TEXT_LEN = 3900


class WhatsAppService:
    """Async client for Gupshup outbound messaging + inbound media download."""

    def __init__(self) -> None:
        self._api_key  = settings.gupshup_api_key
        self._app_name = settings.gupshup_app_name
        self._source   = settings.gupshup_source_number
        self._client   = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={
                "apikey":       self._api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept":       "application/json",
            },
        )

    # ── High-level dispatcher ───────────────────────────────────────────────

    async def send(self, reply: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a normalised reply dict to the right Gupshup call."""
        msg_type = reply.get("type", "text")
        to       = reply["to"]

        if msg_type == "text":
            return await self.send_text_message(to, reply["text"])

        if msg_type == "image":
            return await self.send_image(
                to,
                reply["image_url"],
                caption=reply.get("caption"),
            )

        if msg_type == "document":
            return await self.send_document(
                to,
                reply["document_url"],
                filename=reply.get("filename"),
                caption=reply.get("caption"),
            )

        if msg_type == "buttons":
            return await self.send_interactive_buttons(
                to,
                reply["text"],
                buttons=reply["buttons"],
                header=reply.get("header"),
                footer=reply.get("footer"),
            )

        raise ValueError(f"Unsupported reply type: {msg_type}")

    # ── Outbound: text ──────────────────────────────────────────────────────

    async def send_text_message(self, to: str, text: str) -> dict[str, Any]:
        """Send a plain WhatsApp text message. Long messages auto-split."""
        if not text:
            return {}

        chunks = _split_text(text, _MAX_TEXT_LEN)
        last: dict[str, Any] = {}
        for chunk in chunks:
            body = {"type": "text", "text": chunk}
            last = await self._post_msg(to, body)
        return last

    # Backwards-compat alias for callers that used the old name.
    send_text = send_text_message

    # ── Outbound: image ─────────────────────────────────────────────────────

    async def send_image(
        self,
        to: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type":         "image",
            "originalUrl":  image_url,
            "previewUrl":   image_url,
        }
        if caption:
            body["caption"] = caption
        return await self._post_msg(to, body)

    # ── Outbound: document ──────────────────────────────────────────────────

    async def send_document(
        self,
        to: str,
        document_url: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "file",
            "url":  document_url,
        }
        if filename:
            body["filename"] = filename
        if caption:
            body["caption"] = caption
        return await self._post_msg(to, body)

    # ── Outbound: interactive buttons ───────────────────────────────────────

    async def send_interactive_buttons(
        self,
        to: str,
        text: str,
        buttons: list[str | dict[str, str]],
        header: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send an interactive reply-button message.

        WhatsApp allows up to 3 reply buttons per message (max 20 chars each).
        `buttons` may be a list of strings or dicts with {id, title}.
        """
        normalized = []
        for i, b in enumerate(buttons[:3]):
            if isinstance(b, str):
                normalized.append({
                    "type":  "reply",
                    "reply": {"id": f"btn_{i}", "title": b[:20]},
                })
            else:
                normalized.append({
                    "type":  "reply",
                    "reply": {
                        "id":    str(b.get("id", f"btn_{i}"))[:256],
                        "title": str(b.get("title", ""))[:20],
                    },
                })

        interactive: dict[str, Any] = {
            "type":   "button",
            "body":   {"text": text},
            "action": {"buttons": normalized},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}

        body = {"type": "interactive", "interactive": interactive}
        return await self._post_msg(to, body)

    # ── Inbound: media download ─────────────────────────────────────────────

    async def download_media(self, media_id_or_url: str) -> bytes:
        """Download a Gupshup-hosted media file (document, image, or audio)."""
        if not media_id_or_url:
            return b""

        url = media_id_or_url
        if not url.startswith(("http://", "https://")):
            # Treat as a Gupshup media id.
            url = f"{GUPSHUP_BASE_URL}/wa/media/{url}"

        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            log.warning("download_media failed: %s", exc)
            return b""

    # ── Internals ───────────────────────────────────────────────────────────

    async def _post_msg(self, to: str, message_body: dict[str, Any]) -> dict[str, Any]:
        """POST to Gupshup /msg with the given message body."""
        if not self._api_key or not self._source:
            log.warning("Gupshup not configured; would have sent: to=%s body=%s",
                        _mask(to), message_body.get("type"))
            return {"skipped": True, "reason": "gupshup_not_configured"}

        form = {
            "channel":     "whatsapp",
            "source":      self._source,
            "destination": _normalise_phone(to),
            "src.name":    self._app_name,
            "message":     json.dumps(message_body),
        }

        try:
            resp = await self._client.post(GUPSHUP_MSG_URL, data=form)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as exc:
            log.error("Gupshup send failed (%s): %s", exc.response.status_code, exc.response.text)
            return {"error": exc.response.text, "status": exc.response.status_code}
        except httpx.HTTPError as exc:
            log.error("Gupshup send transport error: %s", exc)
            return {"error": str(exc)}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WhatsAppService":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


# ── Module helpers ──────────────────────────────────────────────────────────

def _normalise_phone(phone: str) -> str:
    """Strip '+' and whitespace; Gupshup expects E.164 without the leading '+'."""
    return phone.replace("+", "").replace(" ", "").strip()


def _split_text(text: str, limit: int) -> list[str]:
    """Split text on paragraph or word boundaries to fit the WhatsApp limit."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= limit:
            current = f"{current}\n\n{paragraph}" if current else paragraph
        else:
            if current:
                chunks.append(current)
            if len(paragraph) <= limit:
                current = paragraph
            else:
                # Hard-wrap this oversized paragraph.
                for i in range(0, len(paragraph), limit):
                    chunks.append(paragraph[i : i + limit])
                current = ""
    if current:
        chunks.append(current)
    return chunks


def _mask(phone: str) -> str:
    """Mask phone for safe logging: 919876543210 → 91987****210."""
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"

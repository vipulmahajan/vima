"""Razorpay payments integration.

Phase 1: One-time **60-day access pass** at ₹1,799. We create a Razorpay
Payment Link, the user pays, Razorpay calls /webhooks/razorpay, we verify
the signature, mark the user's pass active for 60 days, and resume any
output that was paused awaiting payment.

──────────────────────────────────────────────────────────────────────────────
TODO Phase 1.5: Monthly recurring subscription via Razorpay Subscriptions API
──────────────────────────────────────────────────────────────────────────────
Once we have returning users whose 60-day access pass has expired, replace
the manual link-creation flow with Razorpay Subscriptions:

  1. One-time setup: create a Razorpay **Plan** (period=monthly, amount=179900,
     currency=INR, name="ViMa Monthly"). Store its plan_id in settings.

  2. On expiry/upsell: call ``client.subscription.create(...)`` with that
     plan_id, customer_notify=1, total_count=12 (or null for indefinite),
     notes={"phone": user_phone, "type": "monthly_renewal"}, and return the
     short_url to the user the same way we return the access-pass link today.

  3. Webhook events to handle: ``subscription.charged`` (renew period_end by
     30 days), ``subscription.cancelled`` / ``subscription.halted`` (let pass
     run to period_end, then expire), ``subscription.completed``.

  4. ``record_payment_intent`` is already parameterised on payment_type;
     pass ``payment_type="monthly_renewal"`` for these rows so they're
     reportable separately.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Optional

import razorpay

from config import settings
from models.database import (
    record_payment_intent,
    mark_subscription_active,
    has_active_subscription,
    find_payment_by_link_id,
)

log = logging.getLogger(__name__)

# Hard-coded contract values for the access pass. Amount itself is read from
# settings.price_subscription_paise so it can be tuned without code changes.
ACCESS_PASS_DURATION_DAYS = 60
ACCESS_PASS_DESCRIPTION   = "Vima Career Coach — 30+30 days Access Pass"
ACCESS_PASS_LINK_TTL_SEC  = 24 * 60 * 60   # 24 hours

PAYMENT_TYPE_ACCESS_PASS     = "access_pass"
PAYMENT_TYPE_MONTHLY_RENEWAL = "monthly_renewal"  # reserved for Phase 1.5


class PaymentService:
    """Razorpay SDK wrapper + access-pass lifecycle."""

    def __init__(self) -> None:
        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )

    # ── Subscription state ──────────────────────────────────────────────────

    async def is_subscribed(self, phone: str) -> bool:
        return await has_active_subscription(phone)

    # ── Access pass: Checkout JS order ─────────────────────────────────────

    async def create_order(
        self,
        user_id: str,
        user_name: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Order for Checkout JS and record a payment intent.

        Returns a dict with order_id, amount (paise), currency, key_id,
        user_name, user_email — ready to push as a payment event.
        """
        amount_paise = settings.price_subscription_paise

        payload: dict[str, Any] = {
            "amount":          amount_paise,
            "currency":        "INR",
            "payment_capture": 1,
            "notes": {
                "user_id":       user_id,
                "payment_type":  PAYMENT_TYPE_ACCESS_PASS,
                "duration_days": str(ACCESS_PASS_DURATION_DAYS),
            },
        }
        resp = await asyncio.to_thread(self._client.order.create, payload)
        order_id = resp.get("id") or ""

        await record_payment_intent(
            user_id      = user_id,
            amount_paise = amount_paise,
            link_id      = order_id,
            payment_type = PAYMENT_TYPE_ACCESS_PASS,
        )
        log.info("Razorpay order created: order=%s user=%s", order_id, _mask(user_id))
        return {
            "order_id":   order_id,
            "amount":     amount_paise,
            "currency":   "INR",
            "key_id":     settings.razorpay_key_id,
            "user_name":  user_name or "",
            "user_email": user_email or "",
        }

    # ── Access pass: link creation (WhatsApp / fallback) ────────────────────

    async def create_access_pass_link(
        self,
        user_phone: str,
        user_name: Optional[str] = None,
    ) -> str:
        """Create a Razorpay Payment Link for the 60-day access pass.

        Returns the short URL the user can open in WhatsApp, or an empty
        string if Razorpay isn't configured (dev mode).
        """
        amount_paise = settings.price_subscription_paise
        link_url     = ""
        link_id      = ""

        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            log.warning("Razorpay not configured; recording intent without link.")
        else:
            payload: dict[str, Any] = {
                "amount":      amount_paise,
                "currency":    "INR",
                "accept_partial": False,
                "description": ACCESS_PASS_DESCRIPTION,
                "expire_by":   int(time.time()) + ACCESS_PASS_LINK_TTL_SEC,
                "reference_id": f"vima-{user_phone}-{int(time.time())}",
                "customer": {
                    "contact": _e164_for_razorpay(user_phone),
                },
                "notify": {
                    "sms":   True,
                    "email": False,
                },
                "reminder_enable": True,
                "notes": {
                    "user_phone":   user_phone,
                    "payment_type": PAYMENT_TYPE_ACCESS_PASS,
                    "duration_days": str(ACCESS_PASS_DURATION_DAYS),
                },
                "callback_url":    f"{settings.base_url}/webhooks/razorpay",
                "callback_method": "get",
            }
            if user_name:
                payload["customer"]["name"] = user_name

            try:
                # razorpay-python is sync; punt to a thread.
                resp = await asyncio.to_thread(
                    self._client.payment_link.create, payload
                )
                link_url = resp.get("short_url", "") or ""
                link_id  = resp.get("id", "")        or ""
            except Exception as exc:  # noqa: BLE001
                log.exception("Razorpay payment_link.create failed: %s", exc)

        # Record the intent regardless — gives us a paper trail in the DB.
        await record_payment_intent(
            user_id      = user_phone,
            amount_paise = amount_paise,
            link_id      = link_id,
            payment_type = PAYMENT_TYPE_ACCESS_PASS,
        )
        return link_url

    # Backwards-compat alias for older callers.
    async def create_subscription_link(self, sender: str) -> str:
        return await self.create_access_pass_link(sender)

    # ── Webhook signature verification ──────────────────────────────────────

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """Verify the X-Razorpay-Signature header against the raw body."""
        secret = settings.razorpay_webhook_secret or ""
        if not secret or not signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ── Payment confirmation ────────────────────────────────────────────────

    async def handle_payment_confirmed(
        self,
        razorpay_payment_id: str,
        payment_link_id: str,
        user_phone: str,
    ) -> dict[str, Any]:
        """Mark the access pass active and return the payment row."""
        await mark_subscription_active(
            user_id             = user_phone,
            duration_days       = ACCESS_PASS_DURATION_DAYS,
            razorpay_payment_id = razorpay_payment_id,
            link_id             = payment_link_id,
            payment_type        = PAYMENT_TYPE_ACCESS_PASS,
        )
        row = await find_payment_by_link_id(payment_link_id) or {}
        log.info(
            "Access pass activated: phone=%s link=%s payment=%s period_end=%s",
            _mask(user_phone), payment_link_id, razorpay_payment_id,
            row.get("period_end"),
        )
        return row

    # ── Webhook event router ────────────────────────────────────────────────

    async def handle_webhook(
        self,
        payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Process a verified Razorpay webhook event.

        Returns:
          - ``{"kind": "activated", "phone": ..., "payment_link_id": ...,
                "razorpay_payment_id": ...}`` when the pass has been activated.
          - ``{"kind": "renewed",  "phone": ..., "outbound_text": "..."}`` when
                a renewal link has been issued for an expired or failed payment.
          - ``None`` for any other event.
        """
        event = payload.get("event") or ""
        log.info("razorpay.event %s", event)

        if event == "payment_link.paid":
            return await self._handle_event_paid(payload)

        if event == "payment_link.expired":
            return await self._handle_event_expired(payload)

        if event == "payment.failed":
            return await self._handle_event_payment_failed(payload)

        return None

    async def _handle_event_paid(
        self, payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        link_entity, payment_entity = _extract_paid_entities(payload)
        if not link_entity:
            log.warning("payment_link.paid event missing link entity")
            return None

        notes = link_entity.get("notes") or {}
        user_phone = (notes.get("user_phone") or notes.get("phone") or "").strip()
        link_id    = link_entity.get("id") or ""
        payment_id = (payment_entity or {}).get("id", "")

        if not user_phone:
            log.warning("payment_link.paid missing user_phone in notes; link=%s", link_id)
            return None

        await self.handle_payment_confirmed(
            razorpay_payment_id = payment_id,
            payment_link_id     = link_id,
            user_phone          = user_phone,
        )
        return {
            "phone":               user_phone,
            "kind":                "activated",
            "phone":               user_phone,
            "payment_link_id":     link_id,
            "razorpay_payment_id": payment_id,
        }

    async def _handle_event_expired(
        self, payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Reissue a fresh link when the user's 24-hour window expires."""
        link_entity, _ = _extract_paid_entities(payload)
        if not link_entity:
            return None
        user_phone = _phone_from_link_entity(link_entity)
        if not user_phone:
            log.warning("payment_link.expired missing phone; link=%s",
                        link_entity.get("id"))
            return None

        new_link = await self.create_access_pass_link(user_phone)
        body = (
            "Your payment window expired — here's a fresh link valid for "
            "another 24 hours: " + (new_link or "(link generation failed; "
            "reply *retry* in a minute)")
        )
        log.info("razorpay.expired phone=%s reissued=%s",
                 _mask(user_phone), bool(new_link))
        return {"kind": "renewed", "phone": user_phone, "outbound_text": body}

    async def _handle_event_payment_failed(
        self, payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Reissue a fresh link when the user's payment attempt fails."""
        body = payload.get("payload") or {}
        payment_entity = ((body.get("payment") or {}).get("entity")) or {}
        notes = payment_entity.get("notes") or {}
        user_phone = (notes.get("user_phone") or notes.get("phone") or "").strip()

        # Some payment.failed events ride alongside a payment_link block.
        if not user_phone:
            link_entity = ((body.get("payment_link") or {}).get("entity")) or {}
            user_phone = _phone_from_link_entity(link_entity)

        if not user_phone:
            log.warning("payment.failed missing phone; payment_id=%s",
                        payment_entity.get("id"))
            return None

        new_link = await self.create_access_pass_link(user_phone)
        body_text = (
            "Looks like the payment didn't go through. Here's a fresh link "
            "to try again: " + (new_link or "(link generation failed; "
            "reply *retry* in a minute)")
        )
        log.info("razorpay.payment_failed phone=%s reissued=%s",
                 _mask(user_phone), bool(new_link))
        return {"kind": "renewed", "phone": user_phone, "outbound_text": body_text}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _phone_from_link_entity(link_entity: dict[str, Any]) -> str:
    notes = (link_entity or {}).get("notes") or {}
    return (notes.get("user_phone") or notes.get("phone") or "").strip()

def _extract_paid_entities(
    payload: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return (payment_link_entity, payment_entity) from a Razorpay webhook."""
    body = payload.get("payload") or {}
    link    = ((body.get("payment_link") or {}).get("entity")) or {}
    payment = ((body.get("payment")      or {}).get("entity")) or {}
    return (link or None), (payment or None)


def _e164_for_razorpay(phone: str) -> str:
    """Razorpay accepts E.164 numbers with the leading '+'. Add it if missing."""
    p = (phone or "").strip().replace(" ", "")
    if not p:
        return ""
    if p.startswith("+"):
        return p
    return f"+{p}"


def _mask(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"

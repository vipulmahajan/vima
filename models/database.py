"""Supabase-backed persistence layer.

Tables (see schema.sql or Supabase dashboard):
  - users            (phone PK, name, created_at, locale)
  - conversations    (id, phone, role, content, created_at)
  - user_state       (phone PK, flow, resume_step, interview_step, data jsonb)
  - payments         (id, phone, amount_paise, link_id, payment_type,
                      razorpay_payment_id, status, period_end, created_at)
  - artifacts        (id, phone, kind, storage_path, created_at)

This module exposes coroutine helpers used by flows/ and services/. For now
the supabase-py client is sync; we wrap calls in `asyncio.to_thread`.
"""

import asyncio
import logging
from typing import Any, Optional

from supabase import Client, create_client

from config import settings

log = logging.getLogger(__name__)

_client: Optional[Client] = None
# Tiny in-process fallback so the state machine still works for local dev
# when Supabase isn't configured. Production deploys with real creds skip this.
_LOCAL_USERS:        dict[str, dict[str, Any]] = {}
_LOCAL_USER_STATE:   dict[str, dict[str, Any]] = {}
_LOCAL_PAYMENTS:     list[dict[str, Any]]      = []


def _supabase_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_key)


def _get_client() -> Optional[Client]:
    """Return the Supabase client, or None if creds aren't configured."""
    global _client
    if not _supabase_configured():
        return None
    if _client is None:
        try:
            _client = create_client(settings.supabase_url, settings.supabase_service_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Supabase client init failed (%s); falling back to in-memory store.", exc)
            return None
    return _client


async def init_db() -> None:
    """Eagerly construct the client and verify reachability."""
    # TODO: run a trivial query to fail fast on bad creds.
    _get_client()


async def close_db() -> None:
    """Tear-down hook for app shutdown."""
    # supabase-py has no explicit close; nothing to do today.
    return


# ---------------------------------------------------------------------------
# Users + conversation
# ---------------------------------------------------------------------------


async def upsert_user(
    phone: Optional[str] = None,
    name: Optional[str] = None,
    channel: Optional[str] = None,
    email: Optional[str] = None,
    google_id: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> None:
    """Insert or update a user row.

    Web users are identified by email; WhatsApp users by phone. At least one
    of phone or email must be provided. `channel` is "web" or "whatsapp".
    For existing rows, channel is not changed — only set on first insert.
    """
    if not phone and not email:
        raise ValueError("upsert_user requires at least phone or email")

    # Build update payload (fields that are always safe to overwrite on update).
    update_fields: dict[str, Any] = {}
    if name is not None:
        update_fields["name"] = name
    if avatar_url is not None:
        update_fields["avatar_url"] = avatar_url

    client = _get_client()
    if client is None:
        key = email or phone
        existing = _LOCAL_USERS.get(key)
        if existing is None:
            existing = {
                "phone": phone,
                "email": email,
                "google_id": google_id,
                "channel": channel or "web",
            }
            _LOCAL_USERS[key] = existing
        existing.update({k: v for k, v in update_fields.items() if v is not None})
        if channel and not existing.get("channel"):
            existing["channel"] = channel
        return

    def _do() -> None:
        # Determine the lookup key and build payloads.
        if email:
            lookup_col = "email"
            lookup_val = email
        else:
            lookup_col = "phone"
            lookup_val = phone

        existing_resp = (
            client.table("users")
            .select("phone, email, channel")
            .eq(lookup_col, lookup_val)
            .limit(1)
            .execute()
        )

        if existing_resp.data:
            if update_fields:
                client.table("users").update(update_fields).eq(lookup_col, lookup_val).execute()
        else:
            insert_payload: dict[str, Any] = {}
            if phone:
                insert_payload["phone"] = phone
            if email:
                insert_payload["email"] = email
            if google_id:
                insert_payload["google_id"] = google_id
            if name:
                insert_payload["name"] = name
            if avatar_url:
                insert_payload["avatar_url"] = avatar_url
            insert_payload["channel"] = channel or "web"
            # Web users without a phone need a synthetic PK; use email as phone
            # placeholder so the NOT NULL constraint is satisfied on old schemas.
            # New schema has phone nullable, so only add if present.
            if not insert_payload.get("phone") and not _email_pk_schema():
                insert_payload["phone"] = email  # fallback for old schema
            client.table("users").insert(insert_payload).execute()

    await asyncio.to_thread(_do)


def _email_pk_schema() -> bool:
    """Return True if the users table has a nullable phone column (new schema)."""
    # We optimistically assume new schema. If insert fails, the caller handles it.
    return True


async def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    """Fetch name, avatar_url, email for a web user. Returns None if not found."""
    client = _get_client()
    if client is None:
        return _LOCAL_USERS.get(email)

    def _do() -> Optional[dict[str, Any]]:
        resp = (
            client.table("users")
            .select("email, name, avatar_url, channel")
            .eq("email", email)
            .maybe_single()
            .execute()
        )
        return resp.data or None

    return await asyncio.to_thread(_do)


async def get_user_channel(user_id: str) -> Optional[str]:
    """Return the user's channel ('web' or 'whatsapp'), or None if unknown.

    ``user_id`` may be a phone number (WhatsApp) or an email address (web).
    We try phone first; if that returns nothing and the value looks like an
    email we try the email column.
    """
    client = _get_client()
    if client is None:
        row = _LOCAL_USERS.get(user_id) or {}
        return row.get("channel")

    def _do() -> Optional[str]:
        # Try phone column first (covers WhatsApp users and legacy web users).
        resp = (
            client.table("users")
            .select("channel")
            .eq("phone", user_id)
            .maybe_single()
            .execute()
        )
        if resp.data:
            return resp.data.get("channel")
        # Fall back to email column (web / Google Sign-In users).
        if "@" in user_id:
            resp2 = (
                client.table("users")
                .select("channel")
                .eq("email", user_id)
                .maybe_single()
                .execute()
            )
            if resp2.data:
                return resp2.data.get("channel")
        return None

    return await asyncio.to_thread(_do)


async def append_conversation(phone: str, role: str, content: str) -> None:
    client = _get_client()
    if client is None:
        return  # not stored locally — only needed for Claude context retrieval

    def _do() -> None:
        client.table("conversations").insert(
            {"phone": phone, "role": role, "content": content}
        ).execute()

    await asyncio.to_thread(_do)


async def recent_conversation(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` conversation turns for a user.

    ``user_id`` may be a phone (WhatsApp) or email (web). For web users the
    conversations table stores the email in the ``phone`` column (text field).
    """
    client = _get_client()
    if client is None:
        return []

    def _do() -> list[dict[str, Any]]:
        resp = (
            client
            .table("conversations")
            .select("role, content, created_at")
            .eq("phone", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(resp.data or []))

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Per-user flow state
# ---------------------------------------------------------------------------


async def get_user_state(phone: str) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        return dict(_LOCAL_USER_STATE.get(phone, {}))

    def _do() -> dict[str, Any]:
        resp = (
            client
            .table("user_state")
            .select("*")
            .eq("phone", phone)
            .maybe_single()
            .execute()
        )
        return resp.data or {}

    return await asyncio.to_thread(_do)


async def upsert_user_state(phone: str, patch: dict[str, Any]) -> None:
    client = _get_client()
    if client is None:
        import datetime
        existing = _LOCAL_USER_STATE.get(phone, {"phone": phone})
        existing.update(patch)
        existing["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _LOCAL_USER_STATE[phone] = existing
        return

    def _do() -> None:
        payload = {"phone": phone, **patch}
        client.table("user_state").upsert(payload, on_conflict="phone").execute()

    await asyncio.to_thread(_do)


async def merge_user_state_data(phone: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge `patch` into user_state.data (jsonb) and persist.

    Returns the merged data dict.
    """
    current = await get_user_state(phone)
    data = dict(current.get("data") or {})
    data.update(patch)
    await upsert_user_state(phone, {"data": data})
    return data


# ---------------------------------------------------------------------------
# Lifecycle helpers (nudges + archive)
# ---------------------------------------------------------------------------


async def list_active_user_states() -> list[dict[str, Any]]:
    """Return every user_state row. Used by the scheduled nudge / archive loop.

    For Supabase: select *. For in-memory: dump the dict. Either way the
    caller is responsible for filtering on flow / step / updated_at.
    """
    client = _get_client()
    if client is None:
        return [dict(s) for s in _LOCAL_USER_STATE.values()]

    def _do() -> list[dict[str, Any]]:
        resp = client.table("user_state").select("*").execute()
        return list(resp.data or [])

    return await asyncio.to_thread(_do)


async def archive_user_state(phone: str) -> None:
    """Move the current state under data.archived_state and reset to idle.

    The next message from this user will land in the menu flow.
    """
    state = await get_user_state(phone)
    if not state:
        return
    snapshot = {k: state.get(k) for k in
                ("flow", "resume_step", "interview_step", "data")}
    new_data = {"archived_state": snapshot}

    log.info("archiving stale state phone=%s flow=%s", _mask(phone), state.get("flow"))

    await upsert_user_state(phone, {
        "flow":            "idle",
        "resume_step":     "welcome",
        "interview_step":  "welcome",
        "data":            new_data,
    })


def _mask(phone: str) -> str:
    if not phone or len(phone) < 6:
        return "***"
    return f"{phone[:5]}****{phone[-3:]}"


# ---------------------------------------------------------------------------
# Payments / subscription
# ---------------------------------------------------------------------------


async def record_payment_intent(
    phone: str,
    amount_paise: int,
    link_id: str,
    payment_type: str = "access_pass",
) -> None:
    """Insert a `created`-status row for a freshly-issued payment link.

    `payment_type` is one of:
      - "access_pass"      — one-time 60-day access pass (current default)
      - "monthly_renewal"  — recurring monthly subscription (future)
    """
    client = _get_client()
    if client is None:
        _LOCAL_PAYMENTS.append({
            "phone":         phone,
            "amount_paise":  amount_paise,
            "link_id":       link_id,
            "payment_type":  payment_type,
            "status":        "created",
        })
        return

    def _do() -> None:
        client.table("payments").insert({
            "phone":        phone,
            "amount_paise": amount_paise,
            "link_id":      link_id,
            "payment_type": payment_type,
            "status":       "created",
        }).execute()

    await asyncio.to_thread(_do)


async def mark_subscription_active(
    phone: str,
    duration_days: int = 30,
    razorpay_payment_id: Optional[str] = None,
    link_id: Optional[str] = None,
    payment_type: Optional[str] = None,
) -> None:
    """Mark the latest payment as paid and set period_end to +`duration_days`.

    For the 60-day access pass, callers pass ``duration_days=60``. The default
    of 30 is preserved for backwards compatibility with the older monthly
    code path.

    If `link_id` is provided, the matching payment row is updated; otherwise
    the most recent ``status='created'`` row for the phone is used.
    """
    import datetime

    period_end = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=duration_days)
    ).isoformat()

    update_fields: dict[str, Any] = {"status": "paid", "period_end": period_end}
    if razorpay_payment_id:
        update_fields["razorpay_payment_id"] = razorpay_payment_id
    if payment_type:
        update_fields["payment_type"] = payment_type

    client = _get_client()
    if client is None:
        # Prefer a row matched by link_id; fall back to the latest created.
        target = None
        if link_id:
            for row in _LOCAL_PAYMENTS:
                if row.get("link_id") == link_id:
                    target = row
                    break
        if target is None:
            for row in reversed(_LOCAL_PAYMENTS):
                if row.get("phone") == phone and row.get("status") == "created":
                    target = row
                    break
        if target is not None:
            target.update(update_fields)
        return

    def _do() -> None:
        q = client.table("payments").update(update_fields).eq("phone", phone)
        if link_id:
            q = q.eq("link_id", link_id)
        else:
            q = (q
                 .eq("status", "created")
                 .order("created_at", desc=True)
                 .limit(1))
        q.execute()

    await asyncio.to_thread(_do)


async def find_payment_by_link_id(link_id: str) -> Optional[dict[str, Any]]:
    """Look up a payment row by Razorpay payment_link id."""
    if not link_id:
        return None

    client = _get_client()
    if client is None:
        for row in _LOCAL_PAYMENTS:
            if row.get("link_id") == link_id:
                return dict(row)
        return None

    def _do() -> Optional[dict[str, Any]]:
        resp = (
            client
            .table("payments")
            .select("*")
            .eq("link_id", link_id)
            .maybe_single()
            .execute()
        )
        return resp.data or None

    return await asyncio.to_thread(_do)


async def has_active_subscription(phone: str) -> bool:
    """Return True if there is a paid subscription row with period_end in the future."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    client = _get_client()
    if client is None:
        return any(
            row["phone"] == phone
            and row.get("status") == "paid"
            and (row.get("period_end") or "") > now
            for row in _LOCAL_PAYMENTS
        )

    def _do() -> bool:
        resp = (
            client
            .table("payments")
            .select("id")
            .eq("phone", phone)
            .eq("status", "paid")
            .gt("period_end", now)
            .limit(1)
            .execute()
        )
        return bool(resp.data)

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Artifacts (PDFs, audio, etc.)
# ---------------------------------------------------------------------------


async def store_user_document(
    user_id: str,
    doc_type: str,
    filename: Optional[str],
    mime_type: Optional[str],
    raw_text: str,
) -> None:
    """Insert a row into user_documents for a web upload.

    ``user_id`` is the user's email. ``doc_type`` is one of 'resume', 'jd', 'other'.
    """
    client = _get_client()
    if client is None:
        return

    def _do() -> None:
        client.table("user_documents").insert({
            "user_id":   user_id,
            "type":      doc_type,
            "filename":  filename,
            "mime_type": mime_type,
            "raw_text":  raw_text,
        }).execute()

    await asyncio.to_thread(_do)


async def record_artifact(phone: str, kind: str, storage_path: str) -> None:
    client = _get_client()
    if client is None:
        return

    def _do() -> None:
        client.table("artifacts").insert(
            {"phone": phone, "kind": kind, "storage_path": storage_path}
        ).execute()

    await asyncio.to_thread(_do)

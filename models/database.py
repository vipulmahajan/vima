"""Supabase-backed persistence layer.

Tables (see schema_v2.sql or Supabase dashboard):
  - users            (id uuid PK, phone unique nullable, email unique nullable,
                      name, google_id, avatar_url, channel, locale)
  - conversations    (id, user_id TEXT — email or phone, no FK, role, content, created_at)
  - user_state       (user_id TEXT PK — email or phone, no FK, flow, resume_step,
                      interview_step, data jsonb)
  - payments         (id, user_id TEXT — email or phone, no FK, amount_paise, link_id,
                      payment_type, razorpay_payment_id, status, period_end)
  - artifacts        (id, user_id TEXT — email or phone, no FK, kind, storage_path, version)
  - sessions         (id, user_id TEXT — email, jwt_token_hash, expires_at)
  - user_documents   (id, user_id TEXT — email, type, filename, mime_type, raw_text)

user_id is always: email for web users, E.164 phone for WhatsApp users.
"""

import asyncio
import logging
from typing import Any, Optional

from supabase import Client, create_client

from config import settings

log = logging.getLogger(__name__)

_client: Optional[Client] = None
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
    _get_client()


async def close_db() -> None:
    return


# ---------------------------------------------------------------------------
# Users
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

    Web users are looked up by email; WhatsApp users by phone. At least one
    of phone or email must be provided. channel is 'web' or 'whatsapp'.
    """
    if not phone and not email:
        raise ValueError("upsert_user requires at least phone or email")

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
                "channel": channel or ("web" if email else "whatsapp"),
            }
            _LOCAL_USERS[key] = existing
        existing.update({k: v for k, v in update_fields.items() if v is not None})
        if channel and not existing.get("channel"):
            existing["channel"] = channel
        return

    def _do() -> None:
        if email:
            lookup_col, lookup_val = "email", email
        else:
            lookup_col, lookup_val = "phone", phone

        existing_resp = (
            client.table("users")
            .select("id, phone, email, channel")
            .eq(lookup_col, lookup_val)
            .limit(1)
            .execute()
        )

        if existing_resp.data:
            patch: dict[str, Any] = dict(update_fields)
            # Attach a phone to an existing web user (collected at checkout).
            if phone and not existing_resp.data[0].get("phone"):
                patch["phone"] = phone
            if patch:
                client.table("users").update(patch).eq(lookup_col, lookup_val).execute()
        else:
            insert_payload: dict[str, Any] = {
                "channel": channel or ("web" if email else "whatsapp"),
            }
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
            client.table("users").insert(insert_payload).execute()

    await asyncio.to_thread(_do)


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
    """Return 'web' or 'whatsapp' for the given user_id (email or phone)."""
    client = _get_client()
    if client is None:
        row = _LOCAL_USERS.get(user_id) or {}
        return row.get("channel")

    def _do() -> Optional[str]:
        if "@" in user_id:
            resp = (
                client.table("users")
                .select("channel")
                .eq("email", user_id)
                .maybe_single()
                .execute()
            )
        else:
            resp = (
                client.table("users")
                .select("channel")
                .eq("phone", user_id)
                .maybe_single()
                .execute()
            )
        return (resp.data or {}).get("channel")

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


async def append_conversation(user_id: str, role: str, content: str) -> None:
    client = _get_client()
    if client is None:
        return

    def _do() -> None:
        client.table("conversations").insert(
            {"user_id": user_id, "role": role, "content": content}
        ).execute()

    await asyncio.to_thread(_do)


async def recent_conversation(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` turns for a user (email or phone)."""
    client = _get_client()
    if client is None:
        return []

    def _do() -> list[dict[str, Any]]:
        resp = (
            client
            .table("conversations")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(resp.data or []))

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Per-user flow state
# ---------------------------------------------------------------------------


async def get_user_state(user_id: str) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        return dict(_LOCAL_USER_STATE.get(user_id, {}))

    def _do() -> dict[str, Any]:
        resp = (
            client
            .table("user_state")
            .select("user_id,flow,resume_step,interview_step,data,updated_at")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if resp is None or resp.data is None:
            return {}
        return resp.data

    return await asyncio.to_thread(_do)


async def upsert_user_state(user_id: str, patch: dict[str, Any]) -> None:
    client = _get_client()
    if client is None:
        import datetime
        existing = _LOCAL_USER_STATE.get(user_id, {"user_id": user_id})
        existing.update(patch)
        existing["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _LOCAL_USER_STATE[user_id] = existing
        return

    def _do() -> None:
        payload = {"user_id": user_id, **patch}
        client.table("user_state").upsert(payload, on_conflict="user_id").execute()

    await asyncio.to_thread(_do)


async def merge_user_state_data(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``patch`` into user_state.data (jsonb) and persist."""
    current = await get_user_state(user_id)
    data = dict(current.get("data") or {})
    data.update(patch)
    await upsert_user_state(user_id, {"data": data})
    return data


# ---------------------------------------------------------------------------
# Lifecycle helpers (nudges + archive)
# ---------------------------------------------------------------------------


async def list_active_user_states() -> list[dict[str, Any]]:
    """Return every user_state row for the nudge / archive loop."""
    client = _get_client()
    if client is None:
        return [dict(s) for s in _LOCAL_USER_STATE.values()]

    def _do() -> list[dict[str, Any]]:
        resp = client.table("user_state").select("user_id,flow,resume_step,interview_step,data,updated_at").execute()
        if resp is None or resp.data is None:
            return []
        return list(resp.data)

    return await asyncio.to_thread(_do)


async def archive_user_state(user_id: str) -> None:
    """Snapshot current state into data.archived_state and reset to idle."""
    state = await get_user_state(user_id)
    if not state:
        return
    snapshot = {k: state.get(k) for k in
                ("flow", "resume_step", "interview_step", "data")}

    log.info("archiving stale state user_id=%s flow=%s", _mask(user_id), state.get("flow"))

    await upsert_user_state(user_id, {
        "flow":           "idle",
        "resume_step":    "welcome",
        "interview_step": "welcome",
        "data":           {"archived_state": snapshot},
    })


def _mask(value: str) -> str:
    """Mask a phone or email for safe logging."""
    if not value or len(value) < 6:
        return "***"
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:2]}***@{domain}"
    return f"{value[:5]}****{value[-3:]}"


# ---------------------------------------------------------------------------
# Payments / subscription
# ---------------------------------------------------------------------------


async def record_payment_intent(
    user_id: str,
    amount_paise: int,
    link_id: str,
    payment_type: str = "access_pass",
) -> None:
    """Insert a ``created``-status row for a freshly-issued payment link.

    ``user_id`` is email for web users, E.164 phone for WhatsApp users.
    """
    client = _get_client()
    if client is None:
        _LOCAL_PAYMENTS.append({
            "user_id":      user_id,
            "amount_paise": amount_paise,
            "link_id":      link_id,
            "payment_type": payment_type,
            "status":       "created",
        })
        return

    def _do() -> None:
        client.table("payments").insert({
            "user_id":      user_id,
            "amount_paise": amount_paise,
            "link_id":      link_id,
            "payment_type": payment_type,
            "status":       "created",
        }).execute()

    await asyncio.to_thread(_do)


async def mark_subscription_active(
    user_id: str,
    duration_days: int = 30,
    razorpay_payment_id: Optional[str] = None,
    link_id: Optional[str] = None,
    payment_type: Optional[str] = None,
) -> None:
    """Mark the matching payment row as paid and set period_end to +duration_days.

    If ``link_id`` is provided, the matching payment row is updated; otherwise
    the most recent ``status='created'`` row for the user is used.
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
        target = None
        if link_id:
            for row in _LOCAL_PAYMENTS:
                if row.get("link_id") == link_id:
                    target = row
                    break
        if target is None:
            for row in reversed(_LOCAL_PAYMENTS):
                if row.get("user_id") == user_id and row.get("status") == "created":
                    target = row
                    break
        if target is not None:
            target.update(update_fields)
        return

    def _do() -> None:
        q = client.table("payments").update(update_fields).eq("user_id", user_id)
        if link_id:
            q = q.eq("link_id", link_id)
        else:
            q = q.eq("status", "created")
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


async def has_active_subscription(user_id: str) -> bool:
    """Return True if there is a paid subscription row with period_end in the future."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    client = _get_client()
    if client is None:
        return any(
            row["user_id"] == user_id
            and row.get("status") == "paid"
            and (row.get("period_end") or "") > now
            for row in _LOCAL_PAYMENTS
        )

    def _do() -> bool:
        resp = (
            client
            .table("payments")
            .select("id")
            .eq("user_id", user_id)
            .eq("status", "paid")
            .gt("period_end", now)
            .limit(1)
            .execute()
        )
        return bool(resp.data)

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Artifacts (PDFs, etc.)
# ---------------------------------------------------------------------------


async def record_artifact(user_id: str, kind: str, storage_path: str) -> None:
    client = _get_client()
    if client is None:
        return

    def _do() -> None:
        client.table("artifacts").insert(
            {"user_id": user_id, "kind": kind, "storage_path": storage_path}
        ).execute()

    await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# User documents (uploaded files)
# ---------------------------------------------------------------------------


async def store_user_document(
    user_id: str,
    doc_type: str,
    filename: Optional[str],
    mime_type: Optional[str],
    raw_text: str,
) -> None:
    """Insert a row into user_documents for a web upload.

    ``user_id`` is the user's email. ``doc_type`` is 'resume', 'jd', or 'other'.
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

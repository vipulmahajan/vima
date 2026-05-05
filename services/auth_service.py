"""Google OAuth authentication service for ViMa web channel.

Flow:
  1. get_google_auth_url(state)      — build the Google OAuth consent URL
  2. handle_google_callback(code)    — exchange code → tokens → user profile,
                                       upsert user in Supabase, return user dict
  3. create_session(email)           — mint JWT, store hash, return raw token
  4. validate_session(token)         — verify JWT + DB hash, return email or None
  5. logout(token)                   — delete session row
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt

from config import settings
from models.database import upsert_user, _get_client

log = logging.getLogger("vima.auth")

# ── Google OAuth endpoints ────────────────────────────────────────────────────

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_GOOGLE_SCOPES = "openid email profile"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _db():
    return _get_client()


# ── Google OAuth ──────────────────────────────────────────────────────────────

def get_google_auth_url(state: str) -> str:
    """Return the Google OAuth consent page URL for the given CSRF state token."""
    params = {
        "client_id":     settings.google_client_id,
        "redirect_uri":  settings.google_redirect_uri,
        "response_type": "code",
        "scope":         _GOOGLE_SCOPES,
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    }
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"


async def handle_google_callback(code: str) -> dict:
    """Exchange an authorisation code for user profile data.

    1. POST to Google's token endpoint to get an access token.
    2. GET /userinfo with the access token.
    3. Upsert the user in Supabase (channel='web').
    4. Return the user dict: {email, name, google_id, avatar_url}.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        # Exchange code for tokens.
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code":          code,
                "client_id":     settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri":  settings.google_redirect_uri,
                "grant_type":    "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        log.error(
            "auth.google.token_exchange_failed status=%d body=%s",
            token_resp.status_code,
            token_resp.text[:300],
        )
        raise RuntimeError("Google token exchange failed.")

    tokens = token_resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("No access_token in Google response.")

    async with httpx.AsyncClient(timeout=10) as client:
        info_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if info_resp.status_code != 200:
        log.error(
            "auth.google.userinfo_failed status=%d body=%s",
            info_resp.status_code,
            info_resp.text[:300],
        )
        raise RuntimeError("Failed to fetch Google user profile.")

    profile = info_resp.json()
    email      = profile.get("email") or ""
    name       = profile.get("name") or ""
    google_id  = profile.get("sub") or ""
    avatar_url = profile.get("picture") or ""

    if not email:
        raise RuntimeError("Google did not return an email address.")

    # Upsert the user; phone stays NULL for web-only sign-ups.
    await upsert_user(
        phone=None,
        name=name,
        channel="web",
        email=email,
        google_id=google_id,
        avatar_url=avatar_url,
    )

    log.info("auth.google.callback email=%s", _mask_email(email))
    return {"email": email, "name": name, "google_id": google_id, "avatar_url": avatar_url}


# ── Session management ────────────────────────────────────────────────────────

def _mint_jwt(email: str, expires_at: _dt.datetime) -> str:
    payload = {
        "user_id": email,
        "exp":     int(expires_at.timestamp()),
        "iat":     int(_now_utc().timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def _store_session(email: str, token_hash: str, expires_at: _dt.datetime) -> None:
    def _insert():
        _db().table("sessions").insert({
            "user_id":        email,
            "jwt_token_hash": token_hash,
            "expires_at":     expires_at.isoformat(),
        }).execute()

    await asyncio.to_thread(_insert)


async def create_session(email: str) -> str:
    """Mint a JWT keyed to the email, persist its hash, return raw token."""
    expires_at = _now_utc() + _dt.timedelta(days=settings.session_ttl_days)
    token      = _mint_jwt(email, expires_at)
    token_hash = _sha256(token)

    await _store_session(email, token_hash, expires_at)
    log.info("auth.session.created email=%s expires=%s", _mask_email(email), expires_at.isoformat())
    return token


async def validate_session(token: str) -> Optional[str]:
    """Verify JWT signature + expiry, confirm hash exists in DB.

    Returns the email (user_id) on success, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        log.debug("auth.session.expired")
        return None
    except jwt.InvalidTokenError as exc:
        log.debug("auth.session.invalid_jwt: %s", exc)
        return None

    email      = payload.get("user_id")
    token_hash = _sha256(token)
    now_iso    = _now_utc().isoformat()

    if not email:
        return None

    def _lookup():
        return (
            _db()
            .table("sessions")
            .select("id")
            .eq("user_id", email)
            .eq("jwt_token_hash", token_hash)
            .gte("expires_at", now_iso)
            .limit(1)
            .execute()
        )

    result = await asyncio.to_thread(_lookup)
    if not (result.data or []):
        log.debug("auth.session.not_in_db email=%s", _mask_email(email))
        return None

    return email


async def logout(token: str) -> None:
    """Delete the session row so the token can no longer be validated."""
    token_hash = _sha256(token)

    def _delete():
        _db().table("sessions").delete().eq("jwt_token_hash", token_hash).execute()

    await asyncio.to_thread(_delete)
    log.info("auth.session.logout token_hash=%s…", token_hash[:8])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked_local = local[:2] + "***" if len(local) > 2 else "***"
    return f"{masked_local}@{domain}"

"""Supabase Storage wrapper.

Uploads generated artifacts (resume PDFs, interview reports, voice clips)
and returns time-limited signed URLs for delivery via WhatsApp.
"""

import asyncio
from typing import Optional

from supabase import Client, create_client

from config import settings


_SIGNED_URL_TTL_SECONDS         = 60 * 60 * 24 * 7  # 7 days  (initial delivery)
_SIGNED_URL_TTL_REFRESH_SECONDS = 60 * 15            # 15 min  (on-demand refresh)


class StorageService:
    """Async-flavored wrapper around the supabase-py storage client."""

    def __init__(self) -> None:
        self._client: Client = create_client(
            settings.supabase_url,
            settings.supabase_service_key,
        )
        self._bucket = settings.supabase_storage_bucket

    async def upload(
        self,
        path: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Upload bytes and return a public/signed URL."""
        # TODO: run the sync supabase calls in a thread; ensure bucket exists.
        bucket = self._client.storage.from_(self._bucket)
        bucket.upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        signed = bucket.create_signed_url(path, _SIGNED_URL_TTL_SECONDS)
        return signed.get("signedURL") or signed.get("signed_url") or ""

    async def create_signed_url(self, path: str, ttl_seconds: int = _SIGNED_URL_TTL_REFRESH_SECONDS) -> str:
        """Generate a fresh signed URL for an existing storage object."""
        def _do() -> str:
            signed = self._client.storage.from_(self._bucket).create_signed_url(path, ttl_seconds)
            return signed.get("signedURL") or signed.get("signed_url") or ""
        return await asyncio.to_thread(_do)

    async def download(self, path: str) -> Optional[bytes]:
        # TODO: error handling for missing objects.
        return self._client.storage.from_(self._bucket).download(path)

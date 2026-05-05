"""Voice-note transcription via AWS Transcribe.

Flow:
  1. Send an immediate acknowledgement to the user so they aren't left waiting
     in silence while Transcribe spins up (jobs typically take 5-15s for short
     clips, sometimes longer).
  2. Download OGG/OPUS audio from WhatsApp.
  3. Upload to S3 under a temp key.
  4. Start an AWS Transcribe job (language_code='en-IN').
  5. Poll until COMPLETED or FAILED (short clips finish in <10s).
  6. Fetch the JSON transcript output from S3 and extract the text.
  7. Delete the temp S3 objects.
"""

import asyncio
import json
import time
import uuid
from typing import Optional

import boto3
import botocore.exceptions

from config import settings
from services.whatsapp_service import WhatsAppService

_POLL_INTERVAL = 2       # seconds between status checks
_POLL_TIMEOUT  = 60      # give up after this many seconds

_ACK_MESSAGE = "Give me a moment to listen to that..."


def _transcribe_client():
    return boto3.client(
        "transcribe",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


async def transcribe_voice_note(
    media_url_or_id: str,
    sender: Optional[str] = None,
) -> str:
    """Acknowledge, download, transcribe, and return the user's voice note text.

    If `sender` is provided, an immediate ack is sent to that WhatsApp number
    BEFORE the slow Transcribe job starts so the user isn't left in silence.
    """
    # 1. Fire-and-forget ack via the channel-agnostic messenger so the user
    #    sees activity immediately, regardless of which channel they're on.
    if sender:
        try:
            from services.messenger import get_messenger
            messenger = await get_messenger(sender)
            await messenger.send_typing_indicator(sender)
            await messenger.send_text(sender, _ACK_MESSAGE)
        except Exception:  # noqa: BLE001
            # Never let ack failure block the actual transcription.
            pass

    # 2. Download the audio. Inbound media download is channel-specific —
    #    today only WhatsApp/Gupshup voice notes hit this code path.
    whatsapp = WhatsAppService()
    try:
        audio_bytes = await whatsapp.download_media(media_url_or_id)
    finally:
        await whatsapp.aclose()

    if not audio_bytes:
        return ""

    # 3-7. Run the blocking S3 + Transcribe pipeline off the event loop.
    return await asyncio.to_thread(_run_transcription_job, audio_bytes)


def _run_transcription_job(audio_bytes: bytes) -> str:
    """Blocking helper: upload → transcribe → fetch → clean up → return text."""
    bucket = settings.aws_transcribe_s3_bucket
    job_id = uuid.uuid4().hex
    audio_key = f"transcribe/audio/{job_id}.ogg"
    output_key = f"transcribe/output/{job_id}.json"

    s3 = _s3_client()
    tc = _transcribe_client()

    try:
        # Upload audio to S3.
        s3.put_object(
            Bucket=bucket,
            Key=audio_key,
            Body=audio_bytes,
            ContentType="audio/ogg",
        )

        # Start transcription job.
        tc.start_transcription_job(
            TranscriptionJobName=job_id,
            LanguageCode="en-IN",
            MediaFormat="ogg",
            Media={"MediaFileUri": f"s3://{bucket}/{audio_key}"},
            OutputBucketName=bucket,
            OutputKey=output_key,
        )

        # Poll until done.
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            resp = tc.get_transcription_job(TranscriptionJobName=job_id)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED":
                return ""
            time.sleep(_POLL_INTERVAL)
        else:
            return ""

        # Fetch transcript JSON from S3.
        obj = s3.get_object(Bucket=bucket, Key=output_key)
        data = json.loads(obj["Body"].read())
        return data["results"]["transcripts"][0]["transcript"]

    except (botocore.exceptions.BotoCoreError, KeyError):
        return ""
    finally:
        # Clean up S3 objects (best-effort).
        for key in (audio_key, output_key):
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001
                pass


async def transcribe_bytes(audio_bytes: bytes) -> str:
    """Transcribe raw audio bytes (e.g. WebM/Opus from MediaRecorder).

    Bypasses the WhatsApp download step — bytes are already in memory.
    Runs the S3 + Transcribe pipeline in a thread and returns the transcript,
    or "" on failure.
    """
    if not audio_bytes:
        return ""
    return await asyncio.to_thread(_run_transcription_job, audio_bytes)


async def synthesize_speech(text: str) -> Optional[bytes]:
    """Optional TTS for replies — not implemented yet."""
    _ = text
    return None

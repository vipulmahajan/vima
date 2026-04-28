"""Document parser with OCR fallback.

Two entry points:

1. ``parse_document(data, mime_type)`` — synchronous, takes raw bytes.
   Returns the structured-resume dict the rest of the pipeline expects.

2. ``extract_text(media_url, mime_type=None, *, filename=None)`` — async,
   downloads the file from a Gupshup media URL and returns clean extracted
   text. Used by the resume flow when the user uploads a CV or JD as a PDF
   or image. Strategy:
     • PDF  → PyPDF2.PdfReader. If the result is too thin (scanned PDF),
              rasterise pages with pdf2image and OCR with pytesseract.
     • DOCX → python-docx paragraph join.
     • Image (jpeg/png/webp) → pytesseract directly via Pillow.
     • Plain text → decode UTF-8.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from io import BytesIO
from typing import Any, Optional

import pdfplumber
from docx import Document
from PIL import Image
from PyPDF2 import PdfReader
import pytesseract

from services.whatsapp_service import WhatsAppService

log = logging.getLogger(__name__)

# Heuristic: anything below this length triggers the OCR fallback for a PDF.
_OCR_FALLBACK_MIN_CHARS = 80

SUPPORTED_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/jpeg": "image",
    "image/png":  "image",
    "image/webp": "image",
    "text/plain": "text",
}


# ── Public: structured parser (used by resume rewrite pipeline) ─────────────

def parse_document(data: bytes, mime_type: str) -> dict[str, Any]:
    """Return a structured-resume dict for the given bytes."""
    text = _extract_sync(data, mime_type)
    return {
        "raw_text":   text,
        # Section extraction is Claude's job downstream; leave stubs for now.
        "contact":    {},
        "summary":    "",
        "experience": [],
        "education":  [],
        "skills":     [],
    }


# ── Public: async URL → text ────────────────────────────────────────────────

async def extract_text(
    media_url: str,
    mime_type: Optional[str] = None,
    *,
    filename: Optional[str] = None,
) -> str:
    """Download the file from a Gupshup media URL and return cleaned text."""
    if not media_url:
        return ""

    whatsapp = WhatsAppService()
    try:
        data = await whatsapp.download_media(media_url)
    finally:
        await whatsapp.aclose()

    if not data:
        log.warning("extract_text: download returned 0 bytes for %s", media_url)
        return ""

    resolved_mime = _resolve_mime(mime_type, filename, data)
    return await asyncio.to_thread(_extract_sync, data, resolved_mime)


# ── Sync core ───────────────────────────────────────────────────────────────

def _extract_sync(data: bytes, mime_type: str) -> str:
    kind = SUPPORTED_MIME.get((mime_type or "").lower())

    if kind == "pdf":
        text = _extract_pdf_text(data)
        if len(text) < _OCR_FALLBACK_MIN_CHARS:
            log.info("PDF text under threshold (%d chars); falling back to OCR.", len(text))
            ocr_text = _ocr_pdf(data)
            if len(ocr_text) > len(text):
                text = ocr_text
        return _clean(text)

    if kind == "docx":
        return _clean(_extract_docx_text(data))

    if kind == "image":
        return _clean(_ocr_image_bytes(data))

    if kind == "text":
        return _clean(data.decode("utf-8", errors="replace"))

    raise ValueError(f"Unsupported document type: {mime_type!r}")


# ── PDF text extraction (PyPDF2 + pdfplumber backstop) ──────────────────────

def _extract_pdf_text(data: bytes) -> str:
    """Try PyPDF2 first; fall back to pdfplumber if PyPDF2 returns nothing."""
    chunks: list[str] = []
    try:
        reader = PdfReader(BytesIO(data))
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                log.debug("PyPDF2 page failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("PyPDF2 failed entirely: %s", exc)

    text = "\n".join(c for c in chunks if c).strip()
    if len(text) >= _OCR_FALLBACK_MIN_CHARS:
        return text

    # Backstop with pdfplumber (often slightly better for tables / columns).
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            plumbed = "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
        if len(plumbed) > len(text):
            return plumbed
    except Exception as exc:  # noqa: BLE001
        log.debug("pdfplumber failed: %s", exc)

    return text


# ── PDF OCR fallback (scanned resumes / image-only PDFs) ────────────────────

def _ocr_pdf(data: bytes) -> str:
    """Rasterise each page and run pytesseract. Returns "" if pdf2image is unavailable."""
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        log.warning("pdf2image not installed; skipping PDF OCR fallback.")
        return ""

    try:
        images = convert_from_bytes(data, dpi=200)
    except Exception as exc:  # noqa: BLE001
        # pdf2image needs poppler on the system PATH; fall through gracefully.
        log.warning("pdf2image conversion failed (poppler missing?): %s", exc)
        return ""

    out: list[str] = []
    for img in images:
        try:
            out.append(pytesseract.image_to_string(img))
        except Exception as exc:  # noqa: BLE001
            log.warning("tesseract OCR failed on a page: %s", exc)
    return "\n".join(out).strip()


# ── Image OCR ───────────────────────────────────────────────────────────────

def _ocr_image_bytes(data: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open image bytes: %s", exc)
        return ""
    try:
        return pytesseract.image_to_string(img)
    except Exception as exc:  # noqa: BLE001
        log.warning("tesseract OCR failed on image: %s", exc)
        return ""


# ── DOCX ────────────────────────────────────────────────────────────────────

def _extract_docx_text(data: bytes) -> str:
    doc = Document(BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs).strip()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_mime(
    mime_type: Optional[str],
    filename: Optional[str],
    data: bytes,
) -> str:
    """Return a best-guess MIME type when Gupshup doesn't provide one."""
    if mime_type:
        return mime_type.lower()

    if filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext == "pdf":
            return "application/pdf"
        if ext == "docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if ext in ("jpg", "jpeg"):
            return "image/jpeg"
        if ext == "png":
            return "image/png"
        if ext == "webp":
            return "image/webp"
        if ext == "txt":
            return "text/plain"

    # Magic-byte sniff as a last resort.
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return ("application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document")
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"


_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()

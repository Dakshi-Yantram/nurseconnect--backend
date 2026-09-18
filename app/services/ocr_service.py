"""OCR-assisted extraction of a provider's legal name (and, where present,
registration/license number) from an uploaded degree certificate or
license document.

IMPORTANT — this is SUGGESTION-ONLY. Results are written to
WorkerDocument.ocr_extracted_name / ocr_extracted_registration_no /
ocr_confidence and never auto-applied to WorkerProfile or User.full_name.
A worker or admin must explicitly confirm via
POST /workers/me/documents/{document_id}/apply-ocr before the suggestion
becomes the value used on contracts, since a misread certificate silently
corrupting a legal name or license number is worse than asking once.

Provider wiring:
  - Set OCR_PROVIDER=google_vision and GOOGLE_VISION_API_KEY to use Google
    Cloud Vision (recommended for production — handles photographed
    certificates well, no local binary needed).
  - Set OCR_PROVIDER=tesseract to use local pytesseract (needs the
    `tesseract-ocr` system package installed on the host/container).
  - Unset/anything else -> stub provider that returns no suggestion, so the
    upload flow still works end-to-end in dev without OCR credentials.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Registration-number patterns seen on Indian nursing/medical/dental council
# certificates — used to pull a number out of the raw OCR text even when the
# layout varies. Extend this list as new formats are seen.
_REG_NO_PATTERNS = [
    re.compile(r"Reg(?:istration)?\.?\s*No\.?\s*[:\-]?\s*([A-Z0-9\/\-]{4,20})", re.IGNORECASE),
    re.compile(r"Enrol(?:l)?ment\s*No\.?\s*[:\-]?\s*([A-Z0-9\/\-]{4,20})", re.IGNORECASE),
    re.compile(r"Council\s*No\.?\s*[:\-]?\s*([A-Z0-9\/\-]{4,20})", re.IGNORECASE),
]

# Heuristic for "Name" line on certificates — deliberately conservative;
# false negatives (no suggestion) are fine, false positives on a legal
# document are not.
_NAME_PATTERNS = [
    re.compile(r"Name\s*(?:of\s*(?:the\s*)?(?:Candidate|Nurse|Student|Registrant))?\s*[:\-]\s*([A-Za-z][A-Za-z\.\s]{2,60})"),
    re.compile(r"This\s+is\s+to\s+certify\s+that\s+([A-Za-z][A-Za-z\.\s]{2,60}?)\s+(?:has|is|s/o|d/o|w/o)", re.IGNORECASE),
]


@dataclass
class OcrResult:
    extracted_name: Optional[str] = None
    extracted_registration_no: Optional[str] = None
    confidence: float = 0.0
    raw_text: str = ""


def _clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" .\n\t")


def _parse_text(text: str) -> OcrResult:
    name = None
    reg_no = None
    for pattern in _NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            name = _clean_name(m.group(1))
            break
    for pattern in _REG_NO_PATTERNS:
        m = pattern.search(text)
        if m:
            reg_no = m.group(1).strip()
            break
    confidence = 0.0
    if name:
        confidence += 0.5
    if reg_no:
        confidence += 0.3
    return OcrResult(extracted_name=name, extracted_registration_no=reg_no, confidence=round(confidence, 3), raw_text=text[:4000])


async def _ocr_via_google_vision(file_bytes: bytes) -> str:
    import base64

    api_key = getattr(settings, "GOOGLE_VISION_API_KEY", None)
    if not api_key:
        logger.warning("OCR_PROVIDER=google_vision but GOOGLE_VISION_API_KEY is not set")
        return ""
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(file_bytes).decode()},
                "features": [{"type": "TEXT_DETECTION"}],
            }
        ]
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        return data["responses"][0]["fullTextAnnotation"]["text"]
    except (KeyError, IndexError):
        return ""


def _ocr_via_tesseract(file_bytes: bytes) -> str:
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("OCR_PROVIDER=tesseract but pytesseract/Pillow is not installed")
        return ""
    try:
        image = Image.open(io.BytesIO(file_bytes))
        return pytesseract.image_to_string(image)
    except Exception:
        logger.exception("Local tesseract OCR failed")
        return ""


async def extract_from_document(file_bytes: bytes) -> OcrResult:
    """Run OCR on an uploaded degree/license document and return a
    best-effort (name, registration_no) suggestion. Never raises — OCR
    failures degrade to an empty OcrResult so document upload never blocks
    on a third-party OCR outage."""
    provider = getattr(settings, "OCR_PROVIDER", "").lower()
    text = ""
    try:
        if provider == "google_vision":
            text = await _ocr_via_google_vision(file_bytes)
        elif provider == "tesseract":
            text = _ocr_via_tesseract(file_bytes)
        else:
            logger.info("OCR_PROVIDER not configured — skipping OCR, upload proceeds without a name suggestion")
    except Exception:
        logger.exception("OCR extraction failed; continuing without a suggestion")
        text = ""

    if not text:
        return OcrResult()
    return _parse_text(text)

"""Parse payment screenshots using Ollama vision model or pytesseract OCR fallback."""

import asyncio
import base64
import io
import logging

import httpx

from app.config import settings
from app.services.ollama_parser import _extract_json, parse_with_ollama

logger = logging.getLogger(__name__)

_IMAGE_PROMPT = """\
You are a UPI payment screenshot analyzer. Examine this payment screenshot from a mobile payment app
(PhonePe, Google Pay, Paytm, BHIM, Amazon Pay, or a bank app) and extract payment details.
{hint_section}
CRITICAL RULE for "amount": Read the rupee amount EXACTLY as shown.
  Indian amounts use a decimal point: "₹1.00" means ONE rupee (1.0), NOT one hundred.
  "₹1,500.00" means fifteen hundred (1500.0).
  Read the digits carefully — do NOT drop or ignore the decimal point.
  Examples: ₹1.00 → 1.0 | ₹100.00 → 100.0 | ₹1,500.00 → 1500.0

CRITICAL RULE for "upi_ref": Extract the BANK UTR / UPI Reference Number ONLY.
  This is typically a 10–15 digit numeric string.
  - PhonePe shows it as "UTR: 586264656963" — use THAT number.
    Do NOT use the "PhonePe Transaction ID" (starts with T, e.g. T260627...).
  - Google Pay shows it as "UPI transaction ID" or "Reference No."
  - Bank receipts show it as "UPI Reference No." or "Ref No."
  The UTR is the same number the bank puts in its notification email/SMS.

Return ONLY valid JSON (no extra text). Use null for missing values:
{{
  "amount": <float — read the decimal point carefully, e.g. 1.0 for ₹1.00>,
  "upi_ref": "<bank UTR — numeric digits only, e.g. 586264656963 — or null>",
  "bank": "<bank name or payment app name or null>",
  "timestamp": "<payment date and time or null>",
  "status": "<credited|debited|success|failed|null>"
}}
"""


async def parse_image_bytes(
    image_bytes: bytes,
    content_type: str = "image/jpeg",
    parse_hint: str | None = None,
) -> tuple[dict, str]:
    """
    Returns (extracted_dict, method_used).
    method_used is one of: 'ollama_vision', 'ocr+ollama', 'ocr+regex', 'failed'.
    """
    vision_error: str | None = None
    ocr_error: str | None = None

    # 1. Try Ollama vision model
    try:
        result = await _ollama_vision(image_bytes, parse_hint)
        if any(v is not None for v in result.values()):
            return result, "ollama_vision"
        vision_error = "Ollama vision returned all-null result"
        logger.warning(vision_error + "; trying OCR fallback")
    except Exception as exc:
        vision_error = str(exc)
        logger.warning("Ollama vision failed (%s); trying OCR fallback", exc)

    # 2. pytesseract OCR → Ollama text parser
    try:
        loop = asyncio.get_event_loop()
        ocr_text = await loop.run_in_executor(None, _ocr_extract, image_bytes)
        if ocr_text.strip():
            result = await parse_with_ollama(ocr_text, parse_hint)
            method = "ocr+ollama"
            if all(v is None for v in result.values()):
                from app.services.ollama_parser import _regex_fallback
                result = _regex_fallback(ocr_text)
                method = "ocr+regex"
            return result, method
    except Exception as exc:
        ocr_error = str(exc)
        logger.warning("OCR fallback failed: %s", exc)

    return {
        "amount": None,
        "upi_ref": None,
        "bank": None,
        "timestamp": None,
        "status": None,
        "_vision_error": vision_error or "unknown",
        "_ocr_error": ocr_error or "unknown",
    }, "failed"


async def _ollama_vision(image_bytes: bytes, parse_hint: str | None) -> dict:
    hint_section = f"\nContext: {parse_hint.strip()}\n" if parse_hint and parse_hint.strip() else ""
    prompt = _IMAGE_PROMPT.format(hint_section=hint_section)
    b64 = base64.b64encode(image_bytes).decode()

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": settings.OLLAMA_VISION_MODEL,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
        )
        resp.raise_for_status()
        return _extract_json(resp.json().get("response", ""))


def _ocr_extract(image_bytes: bytes) -> str:
    """Synchronous pytesseract OCR — run in a thread executor."""
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageOps, ImageStat
    except ImportError as exc:
        raise RuntimeError(
            f"pytesseract/Pillow not installed: {exc}. "
            "Install with: pip install pytesseract pillow"
        ) from exc

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Scale up small images so OCR has more pixels to work with
    w, h = img.size
    if max(w, h) < 1200:
        scale = 1200 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Invert dark-background screenshots (PhonePe dark mode, etc.)
    # pytesseract is trained on dark text on light background
    stat = ImageStat.Stat(img)
    avg_brightness = sum(stat.mean) / 3  # average across R, G, B channels
    if avg_brightness < 100:
        img = ImageOps.invert(img)

    # Boost contrast to sharpen text edges
    img = ImageEnhance.Contrast(img).enhance(2.0)

    # Run OCR — psm 6 assumes a single uniform block of text
    return pytesseract.image_to_string(img, config="--psm 6")

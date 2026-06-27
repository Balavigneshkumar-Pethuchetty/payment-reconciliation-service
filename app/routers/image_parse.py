import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.config import settings
from app.database import get_db
from app.models.channel import ChannelConfig
from app.models.payment import ParsedTransaction, SourceType
from app.schemas.payment import ParseResponse
from app.services.image_parser import parse_image_bytes
from app.services.payment_verifier import verify_screenshot_against_email
from app.services.sse_bus import sse_bus

router = APIRouter(tags=["Parse"])

_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
_MAX_SIZE = 10 * 1024 * 1024  # 10 MB


@router.post("/parseImage", response_model=ParseResponse, status_code=status.HTTP_200_OK)
async def parse_image(
    file: UploadFile = File(..., description="UPI payment screenshot — JPG, PNG, WebP"),
    parse_hint: str | None = Form(default=None, description="Optional context for Ollama"),
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    content_type = file.content_type or "image/jpeg"
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{content_type}'. Upload a JPG, PNG, or WebP image.",
        )

    image_bytes = await file.read()
    if len(image_bytes) > _MAX_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image must be under 10 MB.",
        )

    extracted, method_used = await parse_image_bytes(image_bytes, content_type, parse_hint)

    # Pull out internal error fields before storing (don't pollute the clean payload)
    vision_error = extracted.pop("_vision_error", None)
    ocr_error = extracted.pop("_ocr_error", None)

    extra: dict = {}
    if method_used == "failed":
        if vision_error:
            extra["vision_error"] = vision_error
        if ocr_error:
            extra["ocr_error"] = ocr_error

    parsed = ParsedTransaction(
        source_type=SourceType.IMAGE,
        raw_text=f"[Image: {file.filename or 'upload'}]",
        extracted_amount=extracted.get("amount"),
        extracted_upi_ref=extracted.get("upi_ref"),
        extracted_bank=extracted.get("bank"),
        extracted_timestamp=extracted.get("timestamp"),
        extracted_status=extracted.get("status"),
        extracted_data={
            **extracted,
            "source_filename": file.filename,
            "parse_method": method_used,
            "parse_hint": parse_hint,
            **extra,
        },
    )
    db.add(parsed)
    await db.flush()

    await sse_bus.publish(
        "image_parsed",
        {
            "parse_id": str(parsed.id),
            "filename": file.filename,
            "extracted_amount": float(parsed.extracted_amount) if parsed.extracted_amount else None,
            "extracted_upi_ref": parsed.extracted_upi_ref,
            "extracted_status": parsed.extracted_status,
            "method": method_used,
        },
    )

    return ParseResponse(
        parse_id=parsed.id,
        source_type=parsed.source_type,
        extracted_amount=float(parsed.extracted_amount) if parsed.extracted_amount else None,
        extracted_upi_ref=parsed.extracted_upi_ref,
        extracted_bank=parsed.extracted_bank,
        extracted_timestamp=parsed.extracted_timestamp,
        extracted_status=parsed.extracted_status,
        extracted_data=parsed.extracted_data,
        is_reconciled=parsed.is_reconciled,
    )


async def _read_and_validate_image(file: UploadFile) -> tuple[bytes, str]:
    content_type = file.content_type or "image/jpeg"
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{content_type}'. Upload JPG, PNG, or WebP.",
        )
    image_bytes = await file.read()
    if len(image_bytes) > _MAX_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image must be under 10 MB.",
        )
    return image_bytes, content_type


@router.get("/setupCheck", status_code=status.HTTP_200_OK)
async def setup_check(
    _: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    """Check which image-extraction backends are available on this server."""
    result: dict = {
        "ollama": {},
        "pytesseract": {},
        "setup_ok": False,
        "recommendations": [],
    }

    # Check Ollama reachability and whether the vision model is pulled
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            llava_pulled = any(
                settings.OLLAMA_VISION_MODEL.lower() in n.lower() for n in model_names
            )
            result["ollama"] = {
                "running": True,
                "models": model_names,
                "vision_model_configured": settings.OLLAMA_VISION_MODEL,
                "llava_pulled": llava_pulled,
            }
            if not llava_pulled:
                result["recommendations"].append(
                    f"Pull the vision model: ollama pull {settings.OLLAMA_VISION_MODEL}"
                )
    except Exception as exc:
        result["ollama"] = {"running": False, "error": str(exc)}
        result["recommendations"].append("Ollama is not reachable — start it with: ollama serve")

    # Check pytesseract (requires both the Python package and the tesseract binary)
    try:
        import pytesseract  # type: ignore[import]
        ver = pytesseract.get_tesseract_version()
        result["pytesseract"] = {"installed": True, "version": str(ver)}
    except ImportError:
        result["pytesseract"] = {
            "installed": False,
            "error": "pytesseract Python package not found",
        }
        result["recommendations"].append(
            "Install OCR: pip install pytesseract pillow  &&  apt install tesseract-ocr"
        )
    except Exception as exc:
        result["pytesseract"] = {"installed": False, "error": str(exc)}
        result["recommendations"].append("Install Tesseract binary: apt install tesseract-ocr")

    result["setup_ok"] = (
        result["ollama"].get("llava_pulled", False)
        or result["pytesseract"].get("installed", False)
    )
    return result


@router.post("/verifyPaymentScreenshot", status_code=status.HTTP_200_OK)
async def verify_payment_screenshot(
    file: UploadFile = File(..., description="UPI payment success screenshot"),
    channel_id: uuid.UUID = Form(..., description="Gmail/IMAP channel to search for bank notification"),
    parse_hint: str | None = Form(default=None),
    manual_upi_ref: str | None = Form(
        default=None,
        description="Manual UTR override — enter this when AI extraction fails",
    ),
    manual_amount: float | None = Form(
        default=None,
        description="Manual amount override — enter this when AI extraction fails",
    ),
    search_days: int = Form(default=3, ge=1, le=14, description="How many days back to search Gmail"),
    txn_id: str | None = Form(default=None, description="Auto-reconcile against this transaction ID if CONFIRMED"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    """
    Two-step payment verification:
    1. Ollama reads the screenshot → extracts UPI ref, amount, timestamp
    2. System searches Gmail (via IMAP channel) for the matching bank notification email
    3. Amounts are cross-checked → verdict: CONFIRMED / PENDING / AMOUNT_MISMATCH
    4. If txn_id supplied and CONFIRMED → auto-reconcile the payment intent
    """
    ch = await db.get(ChannelConfig, channel_id)
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    if not ch.credentials:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Channel has no credentials stored")

    image_bytes, content_type = await _read_and_validate_image(file)

    try:
        result = await verify_screenshot_against_email(
            image_bytes=image_bytes,
            content_type=content_type,
            channel_credentials=ch.credentials,
            parse_hint=parse_hint,
            search_days=search_days,
            manual_upi_ref=manual_upi_ref or None,
            manual_amount=manual_amount,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    # Always store the screenshot ParsedTransaction
    screenshot_extra: dict = {}
    if manual_upi_ref:
        screenshot_extra["manual_upi_ref"] = manual_upi_ref
    if manual_amount is not None:
        screenshot_extra["manual_amount"] = manual_amount

    screenshot_parsed = ParsedTransaction(
        source_type=SourceType.IMAGE,
        raw_text=f"[Verified image: {file.filename or 'upload'}]",
        extracted_amount=result.screenshot_amount,
        extracted_upi_ref=result.screenshot_upi_ref,
        extracted_bank=result.screenshot_bank,
        extracted_timestamp=result.screenshot_timestamp,
        extracted_status=result.screenshot_status,
        extracted_data={
            "source_filename": file.filename,
            "parse_method": result.parse_method,
            "parse_hint": parse_hint,
            "verification_verdict": result.verdict,
            "verification_confidence": result.confidence,
            "search_term": result.search_term_used,
            **screenshot_extra,
        },
    )
    db.add(screenshot_parsed)

    # If email was found, store it too as a separate ParsedTransaction
    email_parse_id: str | None = None
    if result.email_found and result.email_match:
        m = result.email_match
        email_parsed = ParsedTransaction(
            source_type=SourceType.EMAIL,
            raw_text=f"From: {m.from_}\nSubject: {m.subject}\nDate: {m.date}\n\n{m.body_preview}",
            extracted_amount=m.extracted_amount,
            extracted_upi_ref=m.extracted_upi_ref,
            extracted_bank=None,
            extracted_timestamp=m.date,
            extracted_status=m.extracted_status,
            extracted_data={
                "email_meta": {
                    "from": m.from_,
                    "subject": m.subject,
                    "date": m.date,
                    "channel_id": str(channel_id),
                    "channel_name": ch.name,
                },
                "linked_screenshot_parse_id": None,  # filled after flush
                "verification_verdict": result.verdict,
            },
        )
        db.add(email_parsed)
        await db.flush()
        email_parse_id = str(email_parsed.id)
        screenshot_parsed.extracted_data["linked_email_parse_id"] = email_parse_id
    else:
        await db.flush()

    screenshot_parse_id = str(screenshot_parsed.id)

    # Auto-reconcile if requested and payment is CONFIRMED
    reconcile_result: dict | None = None
    if txn_id and result.verdict == "CONFIRMED" and email_parse_id:
        from app.services.reconciliation import reconcile_payment
        matched_by = user.get("preferred_username", "image-verify")
        rec = await reconcile_payment(txn_id, email_parsed.id, matched_by, db)
        if rec:
            intent, _ = rec
            reconcile_result = {
                "transaction_id": intent.transaction_id,
                "new_status": intent.status.value,
                "message": "Auto-reconciled from verified screenshot",
            }

    await sse_bus.publish(
        "payment_verified",
        {
            "screenshot_parse_id": screenshot_parse_id,
            "verdict": result.verdict,
            "confidence": result.confidence,
            "amount": result.screenshot_amount,
            "upi_ref": result.screenshot_upi_ref,
            "email_found": result.email_found,
            "channel": ch.name,
        },
    )

    return {
        "screenshot": {
            "parse_id": screenshot_parse_id,
            "amount": result.screenshot_amount,
            "upi_ref": result.screenshot_upi_ref,
            "bank": result.screenshot_bank,
            "status": result.screenshot_status,
            "timestamp": result.screenshot_timestamp,
            "parse_method": result.parse_method,
        },
        "email": {
            "found": result.email_found,
            "parse_id": email_parse_id,
            "from": result.email_match.from_ if result.email_match else None,
            "subject": result.email_match.subject if result.email_match else None,
            "date": result.email_match.date if result.email_match else None,
            "body_preview": result.email_match.body_preview if result.email_match else None,
            "amount": result.email_match.extracted_amount if result.email_match else None,
            "upi_ref": result.email_match.extracted_upi_ref if result.email_match else None,
            "status": result.email_match.extracted_status if result.email_match else None,
            "search_term": result.search_term_used,
        },
        "verification": {
            "verdict": result.verdict,
            "confidence": result.confidence,
            "message": result.message,
        },
        "reconcile": reconcile_result,
        "channel_name": ch.name,
    }

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.database import get_db
from app.models.payment import ParsedTransaction, SourceType
from app.schemas.payment import ParseRequest, ParseResponse
from app.services.ollama_parser import parse_with_ollama

router = APIRouter(tags=["Parse"])


async def _parse_and_store(body: ParseRequest, db: AsyncSession) -> ParseResponse:
    extracted = await parse_with_ollama(body.raw_text)

    full_data = dict(extracted)
    if body.email_meta:
        full_data["email_meta"] = body.email_meta.model_dump(exclude_none=True)

    parsed = ParsedTransaction(
        source_type=body.source_type,
        raw_text=body.raw_text,
        extracted_amount=extracted.get("amount"),
        extracted_upi_ref=extracted.get("upi_ref"),
        extracted_bank=extracted.get("bank"),
        extracted_timestamp=extracted.get("timestamp"),
        extracted_status=extracted.get("status"),
        extracted_data=full_data,
    )
    db.add(parsed)
    await db.flush()

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


@router.post("/parseSMS", response_model=ParseResponse, status_code=status.HTTP_200_OK)
async def parse_sms(
    body: ParseRequest,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    body.source_type = SourceType.SMS
    return await _parse_and_store(body, db)


@router.post("/parseEmail", response_model=ParseResponse, status_code=status.HTTP_200_OK)
async def parse_email(
    body: ParseRequest,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    body.source_type = SourceType.EMAIL
    return await _parse_and_store(body, db)


@router.get("/parsedTransactions", status_code=status.HTTP_200_OK)
async def list_parsed_transactions(
    source_type: SourceType | None = Query(default=None),
    is_reconciled: bool | None = Query(default=None),
    search: str | None = Query(default=None, description="Search UPI ref, bank, status, filename"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    q = select(ParsedTransaction)
    if source_type:
        q = q.where(ParsedTransaction.source_type == source_type)
    if is_reconciled is not None:
        q = q.where(ParsedTransaction.is_reconciled == is_reconciled)
    if search:
        q = q.where(
            ParsedTransaction.extracted_upi_ref.ilike(f"%{search}%")
            | ParsedTransaction.extracted_bank.ilike(f"%{search}%")
            | ParsedTransaction.extracted_status.ilike(f"%{search}%")
            | ParsedTransaction.raw_text.ilike(f"%{search}%")
        )
    if from_date:
        q = q.where(ParsedTransaction.parsed_at >= from_date)
    if to_date:
        q = q.where(ParsedTransaction.parsed_at <= to_date)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(ParsedTransaction.parsed_at.desc()).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    items = [
        {
            "id": str(r.id),
            "source_type": r.source_type.value,
            "extracted_amount": float(r.extracted_amount) if r.extracted_amount else None,
            "extracted_upi_ref": r.extracted_upi_ref,
            "extracted_bank": r.extracted_bank,
            "extracted_status": r.extracted_status,
            "extracted_timestamp": r.extracted_timestamp,
            "is_reconciled": r.is_reconciled,
            "parsed_at": r.parsed_at.isoformat() if r.parsed_at else None,
            "raw_text_preview": (r.raw_text or "")[:120],
            "extracted_data": r.extracted_data or {},
        }
        for r in rows
    ]

    return {"total": total, "page": page, "limit": limit, "items": items}

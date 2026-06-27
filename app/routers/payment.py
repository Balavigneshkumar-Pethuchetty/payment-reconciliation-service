import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.database import get_db
from app.models.payment import AuditLog, CtxType, PaymentIntent, PaymentStatus
from app.schemas.payment import (
    PaymentIntentCreate,
    PaymentIntentResponse,
    TransactionListItem,
    TransactionListResponse,
)
from app.services.hyperswitch import hyperswitch_client
from app.services.upi_qr import build_upi_uri, generate_qr_base64
from app.utils.checksum import compute_checksum, generate_transaction_id

router = APIRouter(tags=["Payment"])


@router.post("/createPayment", response_model=PaymentIntentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    body: PaymentIntentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    idempotent_key = body.idempotent_key or str(uuid.uuid4())
    transaction_id = generate_transaction_id(body.ctx_type.value)

    note = body.reference or body.description
    effective_vpa = body.upi_vpa or None  # None → build_upi_uri falls back to settings
    upi_uri = build_upi_uri(body.amount, transaction_id, note,
                            vpa=effective_vpa, display_name=body.upi_display_name)
    qr_base64 = generate_qr_base64(upi_uri)
    checksum = compute_checksum(transaction_id, body.amount, body.payer_id)

    expiry_at = datetime.now(timezone.utc) + timedelta(hours=body.expiry_hours)

    intent = PaymentIntent(
        transaction_id=transaction_id,
        idempotent_key=idempotent_key,
        ctx_type=body.ctx_type,
        payer_id=body.payer_id,
        amount=body.amount,
        reference=body.reference,
        upi_vpa=effective_vpa,
        upi_qr_data=qr_base64,
        checksum_hash=checksum,
        source_ip=body.source_ip or request.client.host,
        device_id=body.device_id,
        created_by=body.created_by or user.get("preferred_username"),
        # Extended fields
        description=body.description,
        flat_number=body.flat_number,
        member_id=body.member_id,
        payment_category=body.payment_category,
        expiry_at=expiry_at,
        notify_email=body.notify_email,
        payment_metadata=body.payment_metadata,
        tags=body.tags,
    )
    db.add(intent)
    await db.flush()

    hs_payment_id = None
    try:
        hs_resp = await hyperswitch_client.create_payment(
            amount_in_paise=int(body.amount * 100),
            currency="INR",
            transaction_id=transaction_id,
            metadata={
                "ctx_type": body.ctx_type.value,
                "payer_id": body.payer_id,
                "flat_number": body.flat_number,
                "member_id": body.member_id,
                "payment_category": body.payment_category,
            },
        )
        hs_payment_id = hs_resp.get("payment_id")
        intent.hyperswitch_payment_id = hs_payment_id
    except Exception:
        pass  # Hyperswitch unavailable — UPI QR path still valid

    audit = AuditLog(
        payment_intent_id=intent.id,
        action="CREATED",
        old_value=None,
        new_value={
            "transaction_id": transaction_id,
            "amount": float(body.amount),
            "ctx_type": body.ctx_type.value,
            "payer_id": body.payer_id,
            "flat_number": body.flat_number,
            "member_id": body.member_id,
            "payment_category": body.payment_category,
            "description": body.description,
            "expiry_at": expiry_at.isoformat(),
            "tags": body.tags,
        },
        created_by=intent.created_by,
        version_number=1,
    )
    db.add(audit)

    return PaymentIntentResponse(
        transaction_id=intent.transaction_id,
        idempotent_key=intent.idempotent_key,
        ctx_type=intent.ctx_type,
        amount=float(intent.amount),
        currency=intent.currency,
        status=intent.status,
        upi_qr_data=intent.upi_qr_data,
        upi_vpa=intent.upi_vpa,
        hyperswitch_payment_id=intent.hyperswitch_payment_id,
        created_at=intent.created_at,
        payer_id=intent.payer_id,
        reference=intent.reference,
        flat_number=intent.flat_number,
        member_id=intent.member_id,
        description=intent.description,
        payment_category=intent.payment_category,
        tags=intent.tags,
        expiry_at=intent.expiry_at,
        notify_email=intent.notify_email,
        payment_metadata=intent.payment_metadata,
        checksum_hash=intent.checksum_hash,
        error_code=intent.error_code,
        version_number=intent.version_number,
    )


@router.get("/transactions", response_model=TransactionListResponse)
async def list_transactions(
    status: PaymentStatus | None = Query(default=None),
    ctx_type: CtxType | None = Query(default=None),
    payer_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Search in txn_id, reference, description, payer_id, flat_number"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    q = select(PaymentIntent).where(PaymentIntent.is_deleted == False)

    user_roles = set(user.get("realm_access", {}).get("roles", []))
    is_privileged = bool(user_roles.intersection({"admin", "committee_member"}))
    if not is_privileged:
        caller_id = user.get("preferred_username") or user.get("sub")
        q = q.where(PaymentIntent.payer_id == caller_id)

    if status:
        q = q.where(PaymentIntent.status == status)
    if ctx_type:
        q = q.where(PaymentIntent.ctx_type == ctx_type)
    if payer_id:
        q = q.where(PaymentIntent.payer_id == payer_id)
    if search:
        q = q.where(or_(
            PaymentIntent.transaction_id.ilike(f"%{search}%"),
            PaymentIntent.reference.ilike(f"%{search}%"),
            PaymentIntent.description.ilike(f"%{search}%"),
            PaymentIntent.payer_id.ilike(f"%{search}%"),
            PaymentIntent.flat_number.ilike(f"%{search}%"),
            PaymentIntent.member_id.ilike(f"%{search}%"),
            PaymentIntent.payment_category.ilike(f"%{search}%"),
        ))
    if from_date:
        q = q.where(PaymentIntent.created_at >= from_date)
    if to_date:
        q = q.where(PaymentIntent.created_at <= to_date)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(PaymentIntent.created_at.desc()).offset((page - 1) * limit).limit(limit)
    items = (await db.execute(q)).scalars().all()

    return TransactionListResponse(
        total=total,
        page=page,
        limit=limit,
        items=[TransactionListItem.model_validate(item) for item in items],
    )


@router.get("/transactions/{transaction_id}", response_model=PaymentIntentResponse)
async def get_transaction(
    transaction_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    intent = await db.scalar(
        select(PaymentIntent).where(
            PaymentIntent.transaction_id == transaction_id,
            PaymentIntent.is_deleted == False,
        )
    )
    if not intent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found.")

    user_roles = set(user.get("realm_access", {}).get("roles", []))
    is_privileged = bool(user_roles.intersection({"admin", "committee_member"}))
    if not is_privileged:
        caller_id = user.get("preferred_username") or user.get("sub")
        if intent.payer_id != caller_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return PaymentIntentResponse(
        transaction_id=intent.transaction_id,
        idempotent_key=intent.idempotent_key,
        ctx_type=intent.ctx_type,
        amount=float(intent.amount),
        currency=intent.currency,
        status=intent.status,
        upi_qr_data=intent.upi_qr_data,
        upi_vpa=intent.upi_vpa,
        hyperswitch_payment_id=intent.hyperswitch_payment_id,
        created_at=intent.created_at,
        payer_id=intent.payer_id,
        reference=intent.reference,
        flat_number=intent.flat_number,
        member_id=intent.member_id,
        description=intent.description,
        payment_category=intent.payment_category,
        tags=intent.tags,
        expiry_at=intent.expiry_at,
        notify_email=intent.notify_email,
        payment_metadata=intent.payment_metadata,
        checksum_hash=intent.checksum_hash,
        error_code=intent.error_code,
        version_number=intent.version_number,
    )

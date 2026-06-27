from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.payment import AuditLog, PaymentIntent
from app.schemas.payment import AuditEntry, AuditTrailResponse

router = APIRouter(prefix="/auditTrail", tags=["Audit"])


@router.get("/{transaction_id}", response_model=AuditTrailResponse)
async def get_audit_trail(
    transaction_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    intent = await db.scalar(
        select(PaymentIntent)
        .where(PaymentIntent.transaction_id == transaction_id)
        .options(selectinload(PaymentIntent.audit_logs))
    )

    if not intent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found.",
        )

    user_roles = set(user.get("realm_access", {}).get("roles", []))
    is_privileged = bool(user_roles.intersection({"admin", "committee_member"}))
    if not is_privileged:
        # Residents may only view their own transactions.
        caller_id = user.get("preferred_username") or user.get("sub")
        if intent.payer_id != caller_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied to this transaction.")

    history = sorted(intent.audit_logs, key=lambda a: a.version_number)

    return AuditTrailResponse(
        transaction_id=intent.transaction_id,
        current_status=intent.status,
        version_number=intent.version_number,
        history=[
            AuditEntry(
                action=log.action,
                old_value=log.old_value,
                new_value=log.new_value,
                created_by=log.created_by,
                version_number=log.version_number,
                created_at=log.created_at,
            )
            for log in history
        ],
    )

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.database import get_db
from app.models.payment import PaymentStatus
from app.schemas.payment import ReconcileRequest, ReconcileResponse
from app.services.reconciliation import reconcile_payment

router = APIRouter(prefix="/reconcile", tags=["Reconcile"])


@router.put("/{transaction_id}", response_model=ReconcileResponse)
async def reconcile(
    transaction_id: str,
    body: ReconcileRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_roles("admin", "committee_member")),
):
    matched_by = body.matched_by or user.get("preferred_username")
    result = await reconcile_payment(transaction_id, body.parse_id, matched_by, db)

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment intent not found or parse record already reconciled.",
        )

    intent, parsed = result

    message = (
        "Payment reconciled successfully."
        if intent.status == PaymentStatus.RECONCILED
        else f"Reconciliation failed: {intent.error_code}"
    )

    return ReconcileResponse(
        transaction_id=intent.transaction_id,
        status=intent.status,
        message=message,
        reconciled_amount=float(parsed.extracted_amount) if parsed.extracted_amount else None,
        processor_ref_id=intent.processor_ref_id,
    )

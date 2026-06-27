import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import AuditLog, ParsedTransaction, PaymentIntent, PaymentStatus


async def reconcile_payment(
    transaction_id: str,
    parse_id: uuid.UUID,
    matched_by: str | None,
    db: AsyncSession,
) -> tuple[PaymentIntent, ParsedTransaction] | None:
    intent = await db.scalar(
        select(PaymentIntent).where(
            PaymentIntent.transaction_id == transaction_id,
            PaymentIntent.is_deleted == False,  # noqa: E712
        )
    )
    if not intent:
        return None

    parsed = await db.get(ParsedTransaction, parse_id)
    if not parsed or parsed.is_reconciled:
        return None

    old_status = intent.status

    # Tolerance: allow ±1 INR rounding difference
    amount_matches = (
        parsed.extracted_amount is not None
        and abs(float(parsed.extracted_amount) - float(intent.amount)) <= 1.0
    )

    if not amount_matches:
        intent.status = PaymentStatus.FAILED
        intent.error_code = "AMOUNT_MISMATCH"
        action = "RECONCILE_FAILED"
    else:
        intent.status = PaymentStatus.RECONCILED
        intent.processor_ref_id = parsed.extracted_upi_ref
        parsed.is_reconciled = True
        parsed.payment_intent_id = intent.id
        action = "RECONCILED"

    intent.version_number += 1

    audit = AuditLog(
        payment_intent_id=intent.id,
        action=action,
        old_value={"status": old_status},
        new_value={
            "status": intent.status,
            "processor_ref_id": intent.processor_ref_id,
            "matched_by": matched_by,
        },
        created_by=matched_by,
        version_number=intent.version_number,
    )
    db.add(audit)

    return intent, parsed

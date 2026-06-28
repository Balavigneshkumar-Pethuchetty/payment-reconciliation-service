import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import AuditLog, ParsedTransaction, PaymentIntent, PaymentStatus

# Confidence scores for each matching signal that fires
_SCORE_AMOUNT = 30       # amount within ±₹1
_SCORE_VPA = 40          # bank email VPA matches intent's upi_vpa
_SCORE_PAYER = 20        # payer_id matches logged-in user
_SCORE_RECENCY = 10      # intent was created within 24h of the bank email


async def find_matching_intents(
    amount: float,
    db: AsyncSession,
    payer_id: str | None = None,
    payee_vpa: str | None = None,
    payment_time: datetime | None = None,
    tolerance: float = 1.0,
    window_hours: int = 48,
) -> list[dict]:
    """
    Find PENDING PaymentIntents that could correspond to a verified bank notification.

    UTR only exists after payment succeeds — there is no direct FK to look up.
    Instead we score candidates across four independent signals:

      amount   (+30) bank email amount ≈ intent amount (±₹1)   [always checked]
      vpa      (+40) bank email "towards VPA" == intent.upi_vpa [strongest signal]
      payer    (+20) logged-in user == intent.payer_id
      recency  (+10) intent created_at < payment_time ≤ created_at + window_hours

    A score of 100 means all four signals fired — near-certain match.
    Returns candidates sorted by score descending (highest confidence first).
    """
    # Always filter by amount — base requirement
    q = select(PaymentIntent).where(
        PaymentIntent.status == PaymentStatus.PENDING,
        PaymentIntent.is_deleted == False,  # noqa: E712
        PaymentIntent.amount >= amount - tolerance,
        PaymentIntent.amount <= amount + tolerance,
    )
    q = q.order_by(PaymentIntent.created_at.desc())
    rows = (await db.scalars(q)).all()

    results = []
    for r in rows:
        score = _SCORE_AMOUNT
        signals: list[str] = [f"amount ≈ ₹{amount:.2f}"]

        # VPA match — most distinctive signal
        if payee_vpa and r.upi_vpa:
            if payee_vpa.lower().strip() == r.upi_vpa.lower().strip():
                score += _SCORE_VPA
                signals.append(f"VPA matched ({r.upi_vpa})")
            else:
                # VPA present in both but mismatch — strong negative signal
                score -= 20
                signals.append(f"VPA MISMATCH (intent={r.upi_vpa}, email={payee_vpa})")

        # Payer identity match
        if payer_id and r.payer_id:
            if payer_id.lower() == r.payer_id.lower():
                score += _SCORE_PAYER
                signals.append(f"payer_id matched ({r.payer_id})")

        # Recency: intent must exist before the payment and within the window
        if payment_time and r.created_at:
            intent_ts = r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at
            window_end = intent_ts + timedelta(hours=window_hours)
            if intent_ts <= payment_time <= window_end:
                score += _SCORE_RECENCY
                signals.append("created before payment, within window")

        results.append({
            "transaction_id": r.transaction_id,
            "amount": float(r.amount),
            "payer_id": r.payer_id,
            "upi_vpa": r.upi_vpa,
            "ctx_type": r.ctx_type.value,
            "reference": r.reference,
            "created_at": r.created_at.isoformat(),
            "match_score": max(score, 0),          # never negative
            "match_signals": signals,
            "auto_reconcile": score >= 70,          # high confidence threshold
        })

    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results


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

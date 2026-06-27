import hashlib
import hmac

from app.config import settings


def compute_checksum(transaction_id: str, amount: float, payer_id: str | None) -> str:
    payload = f"{transaction_id}:{amount}:{payer_id or ''}:{settings.SECRET_KEY}"
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_checksum(transaction_id: str, amount: float, payer_id: str | None, checksum: str) -> bool:
    expected = compute_checksum(transaction_id, amount, payer_id)
    return hmac.compare_digest(expected, checksum)


def generate_transaction_id(ctx_type: str) -> str:
    import uuid
    import time
    prefix = ctx_type[:3].upper()
    ts = int(time.time() * 1000)
    uid = uuid.uuid4().hex[:8].upper()
    return f"{prefix}-{ts}-{uid}"

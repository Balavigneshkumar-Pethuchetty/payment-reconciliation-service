import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.models.payment import CtxType, PaymentStatus, SourceType
from app.schemas.channel import EmailMetadata


class PaymentIntentCreate(BaseModel):
    # Core fields
    ctx_type: CtxType
    amount: float = Field(gt=0, description="Amount in INR")
    payer_id: str | None = None
    reference: str | None = None

    # Identity / society-specific
    flat_number: str | None = Field(None, description="Flat or unit number, e.g. A-101")
    member_id: str | None = Field(None, description="Society member ID")
    description: str | None = Field(None, description="Human-readable payment description")
    payment_category: str | None = Field(None, description="Granular category, e.g. maintenance_q1_2026")
    tags: list[str] | None = Field(None, description="Free-form tags for filtering")

    # UPI destination — overrides the global default from settings per transaction
    upi_vpa: str | None = Field(None, description="UPI VPA to receive payment, e.g. treasurer@upi")
    upi_display_name: str | None = Field(None, description="Name shown in UPI app, e.g. Society Treasurer")

    # Lifecycle
    expiry_hours: int = Field(default=24, ge=1, le=168, description="QR valid window in hours")
    notify_email: str | None = Field(None, description="Email to notify on reconciliation")

    # Flexible extension bucket
    payment_metadata: dict | None = Field(None, description="Arbitrary key-value context")

    # Request provenance (auto-populated by server if omitted)
    created_by: str | None = None
    source_ip: str | None = None
    device_id: str | None = None
    idempotent_key: str | None = None


class PaymentIntentResponse(BaseModel):
    transaction_id: str
    idempotent_key: str
    ctx_type: CtxType
    amount: float
    currency: str
    status: PaymentStatus
    upi_qr_data: str | None
    upi_vpa: str | None
    hyperswitch_payment_id: str | None
    created_at: datetime

    # Extended fields
    payer_id: str | None = None
    reference: str | None = None
    flat_number: str | None = None
    member_id: str | None = None
    description: str | None = None
    payment_category: str | None = None
    tags: list | None = None
    expiry_at: datetime | None = None
    notify_email: str | None = None
    payment_metadata: dict | None = None
    checksum_hash: str | None = None
    error_code: str | None = None
    version_number: int = 1

    model_config = {"from_attributes": True}


class TransactionListItem(BaseModel):
    transaction_id: str
    ctx_type: CtxType
    amount: float
    currency: str
    status: PaymentStatus
    payer_id: str | None
    reference: str | None
    flat_number: str | None
    member_id: str | None
    description: str | None
    payment_category: str | None
    tags: list | None
    created_by: str | None
    error_code: str | None
    expiry_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TransactionListResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: list[TransactionListItem]


class ParseRequest(BaseModel):
    source_type: SourceType
    raw_text: str = Field(min_length=5, description="Raw SMS or Email body text")
    email_meta: "EmailMetadata | None" = None


class ParseResponse(BaseModel):
    parse_id: uuid.UUID
    source_type: SourceType
    extracted_amount: float | None
    extracted_upi_ref: str | None
    extracted_bank: str | None
    extracted_timestamp: str | None
    extracted_status: str | None
    extracted_data: dict[str, Any] | None
    is_reconciled: bool

    model_config = {"from_attributes": True}


class ReconcileRequest(BaseModel):
    parse_id: uuid.UUID
    matched_by: str | None = None


class ReconcileResponse(BaseModel):
    transaction_id: str
    status: PaymentStatus
    message: str
    reconciled_amount: float | None
    processor_ref_id: str | None


class AuditEntry(BaseModel):
    action: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    created_by: str | None
    version_number: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditTrailResponse(BaseModel):
    transaction_id: str
    current_status: PaymentStatus
    version_number: int
    history: list[AuditEntry]


class RegisterSourceRequest(BaseModel):
    name: str
    source_type: SourceType
    identifier: str = Field(description="Phone number for SMS, email address for EMAIL")
    bank_name: str | None = None


class RegisterSourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    source_type: SourceType
    identifier: str
    bank_name: str | None
    is_active: bool

    model_config = {"from_attributes": True}

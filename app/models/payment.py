import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CtxType(str, PyEnum):
    BILLING = "BILLING"
    DONATION = "DONATION"
    EVENT = "EVENT"
    MAINTENANCE = "MAINTENANCE"


class PaymentStatus(str, PyEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class SourceType(str, PyEnum):
    SMS = "SMS"
    EMAIL = "EMAIL"
    IMAGE = "IMAGE"


class PaymentIntent(Base):
    __tablename__ = "payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    idempotent_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)

    ctx_type: Mapped[CtxType] = mapped_column(Enum(CtxType), nullable=False)
    payer_id: Mapped[str | None] = mapped_column(String(256))
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    reference: Mapped[str | None] = mapped_column(String(256))

    upi_vpa: Mapped[str | None] = mapped_column(String(128))
    upi_qr_data: Mapped[str | None] = mapped_column(Text)

    hyperswitch_payment_id: Mapped[str | None] = mapped_column(String(128))
    processor_ref_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))

    checksum_hash: Mapped[str | None] = mapped_column(String(64))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    device_id: Mapped[str | None] = mapped_column(String(128))

    created_by: Mapped[str | None] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Extended audit / identity fields
    description: Mapped[str | None] = mapped_column(Text)
    flat_number: Mapped[str | None] = mapped_column(String(32))
    member_id: Mapped[str | None] = mapped_column(String(64))
    payment_category: Mapped[str | None] = mapped_column(String(64))
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_email: Mapped[str | None] = mapped_column(String(256))
    payment_metadata: Mapped[dict | None] = mapped_column(JSONB)
    tags: Mapped[list | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="payment_intent", cascade="all, delete-orphan")
    parsed_transactions: Mapped[list["ParsedTransaction"]] = relationship("ParsedTransaction", back_populates="payment_intent")


class ParsedTransaction(Base):
    __tablename__ = "parsed_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("payment_intents.id"))

    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    extracted_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    extracted_upi_ref: Mapped[str | None] = mapped_column(String(128))
    extracted_bank: Mapped[str | None] = mapped_column(String(128))
    extracted_timestamp: Mapped[str | None] = mapped_column(String(64))
    extracted_status: Mapped[str | None] = mapped_column(String(32))
    extracted_data: Mapped[dict | None] = mapped_column(JSONB)

    is_reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    payment_intent: Mapped["PaymentIntent | None"] = relationship("PaymentIntent", back_populates="parsed_transactions")


class AuditLog(Base):
    """Append-only audit table — never update or delete rows."""
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_intent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("payment_intents.id"), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)

    created_by: Mapped[str | None] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    payment_intent: Mapped["PaymentIntent"] = relationship("PaymentIntent", back_populates="audit_logs")


class NotificationSource(Base):
    __tablename__ = "notification_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType), nullable=False)
    identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminUser(Base):
    """Local admin accounts — used when Keycloak is unavailable or for direct /docs access."""
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

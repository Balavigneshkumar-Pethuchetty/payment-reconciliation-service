import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelType(str, PyEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"


class ChannelConfig(Base):
    __tablename__ = "channel_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_type: Mapped[ChannelType] = mapped_column(Enum(ChannelType), nullable=False)

    # GMAIL_IMAP, IMAP, TWILIO, WEBHOOK, etc.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)

    # Provider-specific credentials stored as JSONB.
    # EMAIL/IMAP: { "host": "imap.gmail.com", "port": 993, "username": "...", "password": "...", "use_ssl": true }
    # GMAIL_IMAP uses same structure with host=imap.gmail.com and an App Password.
    # SMS/TWILIO: { "account_sid": "...", "auth_token": "...", "from_number": "..." }
    credentials: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Filter rules — applied before handing email/SMS to the AI parser.
    # { "from_filter": ["alerts@hdfcbank.net"], "subject_keywords": ["UPI", "debited"],
    #   "body_keywords": ["credited", "transaction"], "has_attachment": null }
    filter_rules: Mapped[dict | None] = mapped_column(JSONB)

    polling_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

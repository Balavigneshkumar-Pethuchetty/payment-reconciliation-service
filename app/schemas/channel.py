import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --- Credential helpers (for documentation / UI schema generation) ---

class ImapCredentials(BaseModel):
    """Credentials for IMAP and GMAIL_IMAP providers."""
    host: str = Field(default="imap.gmail.com")
    port: int = Field(default=993)
    username: str
    password: str = Field(description="IMAP password or Gmail App Password")
    use_ssl: bool = True


class EmailFilterRules(BaseModel):
    """Filter rules applied before passing an email to the AI parser."""
    from_filter: list[str] = Field(
        default=[],
        description="Sender addresses to accept. Supports simple substring match (e.g. '@hdfcbank.net').",
    )
    subject_keywords: list[str] = Field(
        default=[],
        description="At least one keyword must appear in the subject (case-insensitive).",
    )
    body_keywords: list[str] = Field(
        default=[],
        description="At least one keyword must appear in the body (case-insensitive).",
    )
    has_attachment: bool | None = Field(
        default=None,
        description="true = only emails with attachments; false = only without; null = any.",
    )


# --- CRUD schemas ---

class ChannelConfigCreate(BaseModel):
    name: str = Field(description="Human-readable label, e.g. 'HDFC Bank Gmail'")
    channel_type: str = Field(description="EMAIL or SMS")
    provider: str = Field(description="GMAIL_IMAP | IMAP | TWILIO | WEBHOOK")
    credentials: dict[str, Any] = Field(description="Provider-specific credentials object")
    filter_rules: dict[str, Any] | None = None
    polling_interval_seconds: int = Field(default=60, ge=30)


class ChannelConfigUpdate(BaseModel):
    name: str | None = None
    credentials: dict[str, Any] | None = None
    filter_rules: dict[str, Any] | None = None
    polling_interval_seconds: int | None = Field(default=None, ge=30)
    is_active: bool | None = None


class ChannelConfigResponse(BaseModel):
    id: uuid.UUID
    name: str
    channel_type: str
    provider: str
    # credentials intentionally excluded from response to avoid leaking secrets
    filter_rules: dict[str, Any] | None
    polling_interval_seconds: int
    is_active: bool
    last_polled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Test-fetch request / response ---

class EmailTestRequest(BaseModel):
    """One-shot IMAP fetch + Ollama parse for debugging. Nothing stored to DB."""
    # Source: saved channel OR inline credentials
    channel_id: uuid.UUID | None = None
    host: str = "imap.gmail.com"
    port: int = 993
    username: str | None = None
    password: str | None = None
    use_ssl: bool = True

    # Filters
    search_phrase: str | None = Field(None, description="Text to search in subject OR body (leave blank = no phrase filter)")
    from_filter: str | None = Field(None, description="Substring match on sender address")

    # Ollama context
    parse_hint: str | None = Field(
        None,
        description="Optional goal/context injected into the Ollama prompt so it knows what to look for.",
    )

    # Behaviour
    limit: int = Field(default=5, ge=1, le=20)
    include_seen: bool = Field(default=True, description="Include already-read emails")
    mark_seen: bool = Field(default=False, description="Mark fetched emails as read")


# --- Email metadata carried by the parse endpoints ---

class EmailMetadata(BaseModel):
    """Optional metadata for manually submitted email parse requests."""
    from_address: str | None = None
    subject: str | None = None
    received_at: str | None = None
    has_attachment: bool = False
    attachment_names: list[str] = []

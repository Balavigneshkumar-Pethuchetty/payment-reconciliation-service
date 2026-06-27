import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.config import settings
from app.database import get_db
from app.models.channel import ChannelConfig
from app.schemas.channel import ChannelConfigCreate, ChannelConfigResponse, ChannelConfigUpdate, EmailTestRequest
from app.services.email_monitor import _imap_fetch_test
from app.services.ollama_parser import parse_with_ollama

router = APIRouter(prefix="/channels", tags=["Channels"])


@router.post("/test-fetch", status_code=status.HTTP_200_OK)
async def test_email_fetch(
    body: EmailTestRequest,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    """
    Fetch emails live from IMAP and run Ollama on them. Nothing is stored to the DB.
    Intended for debugging channel config and Ollama parsing accuracy.
    """
    if body.channel_id:
        ch = await db.get(ChannelConfig, body.channel_id)
        if not ch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
        credentials = ch.credentials
    elif body.username and body.password:
        credentials = {
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "password": body.password,
            "use_ssl": body.use_ssl,
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide either channel_id or IMAP credentials (username + password)",
        )

    try:
        loop = asyncio.get_event_loop()
        messages = await loop.run_in_executor(
            None,
            _imap_fetch_test,
            credentials,
            body.search_phrase,
            body.from_filter,
            body.limit,
            body.include_seen,
            body.mark_seen,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    results = []
    for msg in messages:
        raw_text = (
            f"From: {msg['from']}\n"
            f"Subject: {msg['subject']}\n"
            f"Date: {msg['date']}\n\n"
            f"{msg['body']}"
        )
        ollama_result = await parse_with_ollama(raw_text, parse_hint=body.parse_hint)
        results.append({
            "from": msg["from"],
            "subject": msg["subject"],
            "date": msg["date"],
            "body_preview": msg["body"][:500],
            "body_full": msg["body"],
            "has_attachment": msg["has_attachment"],
            "attachment_names": msg["attachment_names"],
            "raw_text_sent": raw_text[:600],
            "ollama_result": ollama_result,
        })

    return {
        "fetched": len(results),
        "ollama_model": settings.OLLAMA_MODEL,
        "parse_hint": body.parse_hint or None,
        "results": results,
    }


@router.post("", response_model=ChannelConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelConfigCreate,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    ch = ChannelConfig(**body.model_dump())
    db.add(ch)
    await db.flush()
    return ChannelConfigResponse.model_validate(ch)


@router.get("", response_model=list[ChannelConfigResponse])
async def list_channels(
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    result = await db.execute(select(ChannelConfig).order_by(ChannelConfig.created_at))
    return [ChannelConfigResponse.model_validate(ch) for ch in result.scalars().all()]


@router.get("/{channel_id}", response_model=ChannelConfigResponse)
async def get_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    ch = await db.get(ChannelConfig, channel_id)
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return ChannelConfigResponse.model_validate(ch)


@router.patch("/{channel_id}", response_model=ChannelConfigResponse)
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    ch = await db.get(ChannelConfig, channel_id)
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(ch, field, value)
    await db.flush()
    return ChannelConfigResponse.model_validate(ch)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    ch = await db.get(ChannelConfig, channel_id)
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    await db.delete(ch)

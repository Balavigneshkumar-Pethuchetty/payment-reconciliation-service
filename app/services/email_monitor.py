"""Background email monitor.

Polls every active EMAIL ChannelConfig at its configured interval,
fetches unseen IMAP messages, parses them with Ollama, stores
ParsedTransaction records, and broadcasts SSE events.
"""

import asyncio
import html as html_module
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.channel import ChannelConfig, ChannelType
from app.models.payment import ParsedTransaction, SourceType
from app.services.ollama_parser import parse_with_ollama
from app.services.sse_bus import sse_bus

logger = logging.getLogger(__name__)

# Seconds between each iteration of the outer monitor loop.
_OUTER_INTERVAL = 30


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return readable plain text."""
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("style", "script", "head"):
            self._skip = True
        elif tag in ("br", "p", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)


def _html_to_text(html_content: str) -> str:
    """Convert HTML email body to plain text for Ollama parsing."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_content)
    text = html_module.unescape("".join(extractor._parts))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def _imap_fetch(credentials: dict, filter_rules: dict) -> list[dict]:
    """Synchronous IMAP fetch — run in a thread executor."""
    try:
        from imap_tools import AND, MailBox  # type: ignore[import]
    except ImportError:
        logger.error("imap-tools not installed; cannot poll email")
        return []

    from_filter: list[str] = (filter_rules or {}).get("from_filter", [])
    subject_kw: list[str] = (filter_rules or {}).get("subject_keywords", [])
    body_kw: list[str] = (filter_rules or {}).get("body_keywords", [])
    need_attachment: bool | None = (filter_rules or {}).get("has_attachment")

    host = credentials.get("host", "imap.gmail.com")
    port = int(credentials.get("port", 993))
    username = credentials["username"]
    password = credentials["password"]
    use_ssl = bool(credentials.get("use_ssl", True))

    messages: list[dict] = []
    try:
        ctx = MailBox(host, port) if not use_ssl else MailBox(host, port)
        with ctx.login(username, password) as mailbox:
            for msg in mailbox.fetch(AND(seen=False), limit=25, mark_seen=True):
                # from filter
                if from_filter and not any(f.lower() in msg.from_.lower() for f in from_filter):
                    continue
                # subject keyword filter
                subject = msg.subject or ""
                if subject_kw and not any(kw.lower() in subject.lower() for kw in subject_kw):
                    continue
                # body keyword filter
                raw_body = msg.text or msg.html or ""
                body = _html_to_text(raw_body) if raw_body.lstrip().startswith("<") else raw_body
                if body_kw and not any(kw.lower() in body.lower() for kw in body_kw):
                    continue
                # attachment filter
                has_att = len(msg.attachments) > 0
                if need_attachment is True and not has_att:
                    continue
                if need_attachment is False and has_att:
                    continue

                messages.append({
                    "from": msg.from_,
                    "subject": subject,
                    "date": msg.date_str,
                    "body": body[:4000],
                    "has_attachment": has_att,
                    "attachment_names": [a.filename for a in msg.attachments],
                })
    except Exception:
        logger.exception("IMAP fetch failed for %s@%s", username, host)
    return messages


def _imap_fetch_test(
    credentials: dict,
    search_phrase: str | None,
    from_filter: str | None,
    limit: int,
    include_seen: bool,
    mark_seen: bool,
) -> list[dict]:
    """
    IMAP fetch for the test endpoint — does NOT store anything, returns raw dicts.
    Fetches recent emails and applies client-side phrase / sender filtering.
    """
    try:
        from imap_tools import AND, MailBox  # type: ignore[import]
    except ImportError:
        raise RuntimeError("imap-tools is not installed on this server")

    host = credentials.get("host", "imap.gmail.com")
    port = int(credentials.get("port", 993))
    username = credentials["username"]
    password = credentials["password"]

    # Fetch more than limit so client-side filters have room to discard
    fetch_limit = min(limit * 20, 200)
    phrase_lower = search_phrase.lower().strip() if search_phrase else None
    from_lower = from_filter.lower().strip() if from_filter else None

    messages: list[dict] = []
    try:
        with MailBox(host, port).login(username, password) as mailbox:
            criteria = "ALL" if include_seen else AND(seen=False)
            for msg in mailbox.fetch(criteria, limit=fetch_limit, mark_seen=mark_seen, reverse=True):
                if from_lower and from_lower not in msg.from_.lower():
                    continue
                subject = msg.subject or ""
                raw_body = msg.text or msg.html or ""
                body = _html_to_text(raw_body) if raw_body.lstrip().startswith("<") else raw_body
                if phrase_lower and (
                    phrase_lower not in subject.lower() and phrase_lower not in body.lower()
                ):
                    continue
                messages.append({
                    "from": msg.from_,
                    "subject": subject,
                    "date": msg.date_str,
                    "body": body[:4000],
                    "has_attachment": len(msg.attachments) > 0,
                    "attachment_names": [a.filename for a in msg.attachments],
                })
                if len(messages) >= limit:
                    break
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"IMAP connection failed: {exc}") from exc

    return messages


async def _process_channel(
    channel_id: str,
    name: str,
    credentials: dict,
    filter_rules: dict | None,
) -> None:
    loop = asyncio.get_event_loop()
    messages = await loop.run_in_executor(
        None, _imap_fetch, credentials, filter_rules or {}
    )
    if not messages:
        return

    async with AsyncSessionLocal() as db:
        try:
            for msg in messages:
                raw_text = (
                    f"From: {msg['from']}\n"
                    f"Subject: {msg['subject']}\n"
                    f"Date: {msg['date']}\n\n"
                    f"{msg['body']}"
                )
                extracted = await parse_with_ollama(raw_text)

                parsed = ParsedTransaction(
                    source_type=SourceType.EMAIL,
                    raw_text=raw_text,
                    extracted_amount=extracted.get("amount"),
                    extracted_upi_ref=extracted.get("upi_ref"),
                    extracted_bank=extracted.get("bank"),
                    extracted_timestamp=extracted.get("timestamp"),
                    extracted_status=extracted.get("status"),
                    extracted_data={
                        **extracted,
                        "email_meta": {
                            "from": msg["from"],
                            "subject": msg["subject"],
                            "date": msg["date"],
                            "has_attachment": msg["has_attachment"],
                            "attachment_names": msg["attachment_names"],
                            "channel_id": channel_id,
                            "channel_name": name,
                        },
                    },
                )
                db.add(parsed)
                await db.flush()

                await sse_bus.publish(
                    "email_received",
                    {
                        "parse_id": str(parsed.id),
                        "channel": name,
                        "from": msg["from"],
                        "subject": msg["subject"],
                        "extracted_amount": (
                            float(parsed.extracted_amount) if parsed.extracted_amount else None
                        ),
                        "extracted_upi_ref": parsed.extracted_upi_ref,
                        "extracted_status": parsed.extracted_status,
                    },
                )

            await db.commit()
        except Exception:
            logger.exception("Failed to store parsed emails for channel %s", name)
            await db.rollback()


async def _update_last_polled(channel_id: str) -> None:
    async with AsyncSessionLocal() as db:
        try:
            ch = await db.get(ChannelConfig, channel_id)
            if ch:
                ch.last_polled_at = datetime.now(tz=timezone.utc)
                await db.commit()
        except Exception:
            logger.exception("Could not update last_polled_at for channel %s", channel_id)


async def monitor_loop() -> None:
    """Long-running background coroutine started at app startup."""
    # channel_id → monotonic time of last poll
    last_polled: dict[str, float] = {}

    logger.info("Email monitor started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.is_active.is_(True),
                        ChannelConfig.channel_type == ChannelType.EMAIL,
                    )
                )
                channels = result.scalars().all()
                # Read all column values while still in session scope.
                channel_data = [
                    {
                        "id": str(ch.id),
                        "name": ch.name,
                        "credentials": ch.credentials,
                        "filter_rules": ch.filter_rules,
                        "interval": ch.polling_interval_seconds,
                    }
                    for ch in channels
                ]

            now = asyncio.get_event_loop().time()
            tasks = []
            for cd in channel_data:
                cid = cd["id"]
                due_at = last_polled.get(cid, 0) + cd["interval"]
                if now >= due_at:
                    last_polled[cid] = now
                    tasks.append(
                        asyncio.create_task(
                            _process_channel(cid, cd["name"], cd["credentials"], cd["filter_rules"])
                        )
                    )
                    tasks.append(asyncio.create_task(_update_last_polled(cid)))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Email monitor stopped")
            return
        except Exception:
            logger.exception("Unexpected error in monitor loop")

        await asyncio.sleep(_OUTER_INTERVAL)

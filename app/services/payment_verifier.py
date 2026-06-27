"""
Cross-verify a UPI payment screenshot against Gmail bank notification emails.

Flow:
  1. Ollama vision (or OCR fallback) reads the screenshot
     → extracts: upi_ref, amount, timestamp, app/bank
  2. Connect to Gmail (IMAP) using the supplied channel credentials
     → server-side TEXT search for the extracted UPI ref ID in all emails
     → if no UPI ref: fall back to amount string search in recent emails
  3. Parse the matched bank email with Ollama
  4. Cross-check: amounts must be within ₹1 tolerance
  5. Return structured VerificationResult
"""

import asyncio
import logging
from dataclasses import dataclass, field

from app.services.image_parser import parse_image_bytes
from app.services.ollama_parser import parse_with_ollama

logger = logging.getLogger(__name__)


@dataclass
class EmailMatch:
    from_: str
    subject: str
    date: str
    body_preview: str
    extracted_amount: float | None
    extracted_upi_ref: str | None
    extracted_status: str | None


@dataclass
class VerificationResult:
    # Screenshot side
    screenshot_amount: float | None
    screenshot_upi_ref: str | None
    screenshot_bank: str | None
    screenshot_status: str | None
    screenshot_timestamp: str | None
    parse_method: str

    # Email side
    email_found: bool = False
    email_match: EmailMatch | None = None

    # Verdict
    # CONFIRMED       — email found AND amounts match
    # PENDING         — no matching email yet (may arrive in minutes)
    # AMOUNT_MISMATCH — email found but amounts differ (potential fraud/error)
    # NO_UPI_REF      — screenshot had no UPI ref; searched by amount, email not found
    # EXTRACTION_FAILED — Ollama/OCR could not read the screenshot
    verdict: str = "PENDING"
    confidence: str = "LOW"   # HIGH | MEDIUM | LOW
    message: str = ""
    search_term_used: str = ""


async def verify_screenshot_against_email(
    image_bytes: bytes,
    content_type: str,
    channel_credentials: dict,
    parse_hint: str | None = None,
    search_days: int = 3,
    manual_upi_ref: str | None = None,
    manual_amount: float | None = None,
) -> VerificationResult:
    # ── Step 1: parse screenshot ────────────────────────────────
    extracted, method = await parse_image_bytes(image_bytes, content_type, parse_hint)

    result = VerificationResult(
        screenshot_amount=extracted.get("amount"),
        screenshot_upi_ref=extracted.get("upi_ref"),
        screenshot_bank=extracted.get("bank"),
        screenshot_status=extracted.get("status"),
        screenshot_timestamp=extracted.get("timestamp"),
        parse_method=method,
    )

    if method == "failed" or (
        result.screenshot_amount is None and result.screenshot_upi_ref is None
    ):
        # Use manual override values if the user supplied them
        if manual_upi_ref or manual_amount is not None:
            result.screenshot_upi_ref = manual_upi_ref.strip() if manual_upi_ref else None
            result.screenshot_amount = manual_amount
            result.parse_method = "manual_entry"
            logger.info(
                "Extraction failed; using manual override — upi_ref=%s amount=%s",
                manual_upi_ref,
                manual_amount,
            )
        else:
            result.verdict = "EXTRACTION_FAILED"
            result.confidence = "LOW"
            err_detail = ""
            if "_vision_error" in extracted:
                err_detail += f" Vision error: {extracted['_vision_error'][:120]}."
            if "_ocr_error" in extracted:
                err_detail += f" OCR error: {extracted['_ocr_error'][:120]}."
            result.message = (
                "Could not extract payment details from the screenshot."
                + err_detail
                + " Use /setupCheck to see what needs installing,"
                " or fill in Manual Override (UTR + amount) and retry Verify."
            )
            return result

    # ── Step 2: search Gmail with multiple fallback terms ──────
    # Build a prioritised list of search candidates.
    # Try UTR/UPI ref first (most specific), then amount-based patterns.
    candidates: list[str] = []
    if result.screenshot_upi_ref:
        candidates.append(result.screenshot_upi_ref)
    if result.screenshot_amount is not None:
        amt = result.screenshot_amount
        # Banks format amounts differently — try the most common patterns
        candidates += [
            f"Rs.{amt:.2f}",
            f"Rs. {amt:.2f}",
            f"INR {amt:.2f}",
            str(int(amt)) if amt == int(amt) else f"{amt:.2f}",
        ]

    loop = asyncio.get_event_loop()
    raw_messages: list[dict] = []
    term_used = ""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            msgs = await loop.run_in_executor(
                None, _imap_search, channel_credentials, candidate, search_days
            )
            if msgs:
                raw_messages = msgs
                term_used = candidate
                logger.info("IMAP matched %d email(s) using search term '%s'", len(msgs), candidate)
                break
            logger.debug("IMAP: no results for '%s', trying next candidate", candidate)
        except Exception as exc:
            logger.warning("IMAP search for '%s' failed: %s", candidate, exc)

    if not raw_messages and not term_used:
        result.verdict = "PENDING"
        result.confidence = "LOW"
        result.message = "Gmail search failed for all candidates. Check channel credentials."
        return result

    result.search_term_used = term_used

    if not raw_messages:
        result.email_found = False
        if result.screenshot_upi_ref:
            result.verdict = "PENDING"
            result.confidence = "MEDIUM"
            result.message = (
                f"UTR {result.screenshot_upi_ref} not found in Gmail yet. "
                "Bank notifications can take 2–10 minutes — retry shortly. "
                f"Also tried amount patterns: {', '.join(candidates[1:])}."
            )
        else:
            result.verdict = "NO_UPI_REF"
            result.confidence = "LOW"
            result.message = (
                "No UTR found in screenshot and no matching bank email found by amount. "
                "Make sure Ollama extracted the UTR correctly (look for 'UTR:' field in PhonePe)."
            )
        return result

    # ── Step 3: parse the best matching email with Ollama ───────
    best = raw_messages[0]
    raw_text = (
        f"From: {best['from']}\n"
        f"Subject: {best['subject']}\n"
        f"Date: {best['date']}\n\n"
        f"{best['body']}"
    )
    email_hint = (
        "This is a bank transaction notification email. "
        "Extract the credited/debited amount, UPI reference ID, bank name, and status."
    )
    email_extracted = await parse_with_ollama(raw_text, email_hint)

    match = EmailMatch(
        from_=best["from"],
        subject=best["subject"],
        date=best["date"],
        body_preview=best["body"][:400],
        extracted_amount=email_extracted.get("amount"),
        extracted_upi_ref=email_extracted.get("upi_ref"),
        extracted_status=email_extracted.get("status"),
    )
    result.email_found = True
    result.email_match = match

    # ── Step 4: cross-check amounts ────────────────────────────
    scr_amt = result.screenshot_amount
    eml_amt = match.extracted_amount

    if scr_amt is not None and eml_amt is not None:
        if abs(scr_amt - eml_amt) <= 1.0:
            result.verdict = "CONFIRMED"
            result.confidence = "HIGH"
            result.message = (
                f"Bank email confirms ₹{scr_amt:,.2f} payment"
                + (f" via UPI ref {result.screenshot_upi_ref}" if result.screenshot_upi_ref else "")
                + f". Sender: {best['from']}."
            )
        else:
            result.verdict = "AMOUNT_MISMATCH"
            result.confidence = "HIGH"
            result.message = (
                f"Screenshot shows ₹{scr_amt:,.2f} but bank email shows ₹{eml_amt:,.2f}. "
                "Possible screenshot edit or wrong payment — do not auto-reconcile."
            )
    else:
        # Email found but amounts unclear — still a positive signal
        result.verdict = "CONFIRMED"
        result.confidence = "MEDIUM"
        result.message = (
            "Bank email found matching the UPI reference. "
            "Amount could not be fully cross-checked."
        )

    return result


def _amount_search_str(amount: float | None) -> str:
    if amount is None:
        return ""
    # Try common Indian formatting: 1500.00 and 1,500.00
    return str(int(amount)) if amount == int(amount) else f"{amount:.2f}"


def _imap_search(credentials: dict, search_term: str, search_days: int) -> list[dict]:
    """
    Synchronous IMAP TEXT search across all Gmail folders — run in a thread executor.
    Searches [Gmail]/All Mail so bank alerts in Promotions/Updates tabs are included.
    """
    try:
        from imap_tools import AND, MailBox  # type: ignore[import]
    except ImportError:
        raise RuntimeError("imap-tools is not installed")

    from datetime import date, timedelta

    host = credentials.get("host", "imap.gmail.com")
    port = int(credentials.get("port", 993))
    username = credentials["username"]
    password = credentials["password"]

    # Give one extra day of buffer on both sides to avoid timezone edge cases
    date_from = date.today() - timedelta(days=search_days + 1)

    messages: list[dict] = []

    # Folders to try in order: All Mail covers everything in Gmail;
    # INBOX is the fallback for non-Gmail IMAP servers.
    folders_to_try = ["[Gmail]/All Mail", "INBOX"]

    try:
        with MailBox(host, port).login(username, password) as mailbox:
            for folder in folders_to_try:
                try:
                    mailbox.folder.set(folder)
                except Exception:
                    continue  # folder doesn't exist on this server

                criteria = AND(text=search_term, date_gte=date_from)
                for msg in mailbox.fetch(criteria, limit=10, mark_seen=False, reverse=True):
                    body = msg.text or msg.html or ""
                    messages.append({
                        "from": msg.from_,
                        "subject": msg.subject or "",
                        "date": msg.date_str,
                        "body": body[:4000],
                    })

                if messages:
                    break  # found results in this folder — no need to check others

    except Exception as exc:
        raise RuntimeError(f"IMAP search failed: {exc}") from exc

    return messages

import json
import re

import httpx

from app.config import settings

_EXTRACT_PROMPT = """\
You are a bank notification parser. Extract structured payment data from the following SMS or email text.
{hint_section}
Return ONLY valid JSON with these fields (use null for missing values):
{{
  "amount": <float or null>,
  "upi_ref": "<string or null>",
  "bank": "<string or null>",
  "timestamp": "<string or null>",
  "status": "<credited|debited|failed|null>"
}}

Text:
{raw_text}
"""


async def parse_with_ollama(raw_text: str, parse_hint: str | None = None) -> dict:
    hint_section = f"\nContext: {parse_hint.strip()}\n" if parse_hint and parse_hint.strip() else ""
    prompt = _EXTRACT_PROMPT.format(raw_text=raw_text, hint_section=hint_section)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": settings.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            response_text = resp.json().get("response", "")
            return _extract_json(response_text)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
        return _regex_fallback(raw_text)


def _extract_json(text: str) -> dict:
    try:
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        pass
    return _empty_result()


def _regex_fallback(text: str) -> dict:
    """Basic regex extraction — runs when Ollama is not available."""
    amount_match = re.search(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", text, re.IGNORECASE)

    # "UPI transaction reference no.: 530574598139" (HDFC InstaAlerts)
    # "UTR: 586264656963" (PhonePe/bank receipts)
    ref_match = (
        re.search(r"reference\s+no\.?:?\s*(\d{10,20})", text, re.IGNORECASE) or
        re.search(r"(?:UTR|Txn\s*(?:ID|No)?|UPI\s+Ref)[\s:]*([A-Z0-9]{10,20})", text, re.IGNORECASE)
    )

    bank_match = re.search(r"(?:from|via|by)\s+([A-Z][a-z]+ ?(?:Bank|Pay|UPI)?)", text)

    if re.search(r"credit(?:ed)?|received|added", text, re.IGNORECASE):
        status: str | None = "credited"
    elif re.search(r"debit(?:ed)?|sent|paid", text, re.IGNORECASE):
        status = "debited"
    else:
        status = None

    amount_str = amount_match.group(1).replace(",", "") if amount_match else None

    return {
        "amount": float(amount_str) if amount_str else None,
        "upi_ref": ref_match.group(1) if ref_match else None,
        "bank": bank_match.group(1).strip() if bank_match else None,
        "timestamp": None,
        "status": status,
        "_source": "regex_fallback",
    }


def _empty_result() -> dict:
    return {"amount": None, "upi_ref": None, "bank": None, "timestamp": None, "status": None}

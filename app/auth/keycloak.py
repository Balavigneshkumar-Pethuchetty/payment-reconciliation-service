"""
JWKS cache for the society-events Keycloak realm.

Fetches public keys once at startup from:
  https://auth.gm-global-techies-town.club/realms/society-events/protocol/openid-connect/certs

On unknown kid the cache is refreshed once (handles key rotation).
No admin credentials needed — only the public JWKS endpoint is used.
"""
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_jwks: list[dict] = []


def _jwks_url() -> str:
    base = settings.KEYCLOAK_URL.rstrip("/")
    return f"{base}/realms/{settings.KEYCLOAK_REALM}/protocol/openid-connect/certs"


async def fetch_jwks() -> None:
    """Called once at app startup to prime the cache."""
    global _jwks
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_jwks_url())
            resp.raise_for_status()
            _jwks = resp.json().get("keys", [])
        log.info("Keycloak JWKS loaded — %d key(s) cached.", len(_jwks))
    except Exception as exc:
        log.warning("Keycloak unreachable at startup (%s). Running in dev-bypass mode.", exc)
        _jwks = []


async def refresh_jwks() -> None:
    """Re-fetch on unknown kid (handles key rotation without restart)."""
    global _jwks
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(_jwks_url())
        resp.raise_for_status()
        _jwks = resp.json().get("keys", [])
    log.info("Keycloak JWKS refreshed — %d key(s).", len(_jwks))


def get_cached_jwks() -> list[dict]:
    return _jwks


def find_key(kid: str) -> dict | None:
    return next((k for k in _jwks if k.get("kid") == kid), None)

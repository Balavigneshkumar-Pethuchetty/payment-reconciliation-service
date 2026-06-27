"""
FastAPI auth dependencies for the payment service.

Tokens are accepted from two sources (tried in order):
  1. Locally-signed HS256 JWT from POST /auth/login  (iss = payment-reconciliation-local)
  2. Keycloak RS256 JWT  (validated via cached JWKS)

If neither JWKS is cached AND the token has a local issuer, the local path handles it.
Dev-bypass (no token at all + no JWKS) still works for local development.

RBAC matrix:
  POST /createPayment      → admin, committee_member
  POST /parseSMS|Email     → admin, committee_member
  PUT  /reconcile          → admin, committee_member
  GET  /auditTrail         → admin, committee_member, resident (own tx only enforced in router)
  POST /registerSource     → admin
"""
import logging
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from app.auth import keycloak
from app.config import settings

log = logging.getLogger(__name__)

_LOCAL_ISSUER = "payment-reconciliation-local"

# HTTPBearer for raw Authorization: Bearer <token> headers
_bearer = HTTPBearer(auto_error=False)

# OAuth2PasswordBearer tells Swagger UI where to POST username/password to get a token.
# auto_error=False so it doesn't break routes that use _bearer fallback.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_DEV_USER: dict[str, Any] = {
    "sub": "dev",
    "preferred_username": "dev",
    "email": "dev@local",
    "name": "Dev User",
    "aud": ["payment-service"],
    "realm_access": {"roles": ["admin", "committee_member", "resident"]},
}


def _extract_roles(payload: dict) -> list[str]:
    return payload.get("realm_access", {}).get("roles", [])


def _check_audience(payload: dict) -> None:
    """
    Enforce that the token was explicitly scoped for this service.
    aud may be a string or a list depending on how Keycloak serialised it.
    """
    aud = payload.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]
    if settings.KEYCLOAK_AUDIENCE not in aud:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Token not scoped for this service. "
            f"Request a token with scope=payment-api. Got aud={aud}",
        )


def _validate_local_token(token: str) -> dict[str, Any] | None:
    """Try to decode as a locally-signed HS256 token. Returns None if it's not a local token."""
    try:
        unverified = jwt.get_unverified_claims(token)
    except JWTError:
        return None

    if unverified.get("iss") != _LOCAL_ISSUER:
        return None  # not a local token — fall through to Keycloak path

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Local token has expired.")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Local token invalid: {exc}")

    _check_audience(payload)
    return payload


async def _validate_keycloak_token(token: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token header: {exc}")

    kid = header.get("kid", "")
    key = keycloak.find_key(kid)

    if key is None:
        try:
            await keycloak.refresh_jwks()
        except Exception:
            pass
        key = keycloak.find_key(kid)

    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown signing key — token rejected.")

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={"verify_aud": False, "verify_iss": False},
        )
    except ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has expired.")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Token validation failed: {exc}")

    _check_audience(payload)
    return payload


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    oauth2_token: str | None = Depends(oauth2_scheme),
) -> dict[str, Any]:
    """
    Returns decoded token payload.

    Priority order:
      1. Local HS256 token (from POST /auth/login) — works even without Keycloak
      2. Keycloak RS256 token (from JWKS)
      3. Dev-bypass — if no JWKS cached and no token supplied
    """
    raw_token = (creds.credentials if creds else None) or oauth2_token

    if raw_token:
        # Try local token first (fast path, no network needed)
        local = _validate_local_token(raw_token)
        if local is not None:
            return local
        # Fall through to Keycloak
        return await _validate_keycloak_token(raw_token)

    if not keycloak.get_cached_jwks():
        log.debug("Dev-bypass: no JWKS cached and no token, returning dev user.")
        return _DEV_USER

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Bearer token required. Login at POST /auth/login or use Swagger Authorize button.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_roles(*roles: str):
    """
    Dependency factory — raises 403 if the caller's token lacks ALL of the given roles.
    Usage: Depends(require_roles("admin", "committee_member"))  → any one of these is enough.
    """
    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        user_roles = set(_extract_roles(user))
        if not user_roles.intersection(roles):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Required role(s): {list(roles)}. Your roles: {list(user_roles)}",
            )
        return user
    return _check

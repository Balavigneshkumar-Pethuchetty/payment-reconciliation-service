"""
Local auth endpoints — username/password login for Swagger UI and direct API access.

Login priority:
  1. Local admin_users table (username + bcrypt password)
  2. Keycloak password grant via the society-frontend public client
     — so any Keycloak user can login with their existing credentials

Both paths return a locally-signed JWT accepted everywhere in this service.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt as jose_jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import keycloak
from app.config import settings
from app.database import get_db
from app.models.payment import AdminUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

_LOCAL_ISSUER = "payment-reconciliation-local"
_TOKEN_EXPIRE_HOURS = 24


def _hash_password(plain: str) -> str:
    import bcrypt
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _make_local_jwt(username: str, sub: str, roles: list[str]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": _LOCAL_ISSUER,
        "sub": sub,
        "preferred_username": username,
        "aud": [settings.KEYCLOAK_AUDIENCE],
        "realm_access": {"roles": roles},
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_EXPIRE_HOURS),
    }
    return jose_jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _make_token(user: AdminUser) -> str:
    return _make_local_jwt(user.username, str(user.id), user.roles)


async def _keycloak_password_grant(username: str, password: str) -> dict | None:
    """
    Authenticate against Keycloak using the password (ROPC) grant.
    Returns the decoded token payload if successful, None if credentials are wrong.
    Raises HTTPException on server-side Keycloak errors.
    """
    token_url = (
        f"{settings.KEYCLOAK_URL.rstrip('/')}"
        f"/realms/{settings.KEYCLOAK_REALM}"
        f"/protocol/openid-connect/token"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": settings.KEYCLOAK_LOGIN_CLIENT_ID,
                    "username": username,
                    "password": password,
                },
            )
    except Exception as exc:
        log.warning("Keycloak unreachable during login: %s", exc)
        return None

    if resp.status_code == 401:
        return None  # wrong credentials

    if resp.status_code != 200:
        log.warning("Keycloak password grant returned %s: %s", resp.status_code, resp.text[:200])
        return None

    kc_token = resp.json().get("access_token", "")
    if not kc_token:
        return None

    # Decode and verify signature using cached JWKS
    jwks = keycloak.get_cached_jwks()
    if not jwks:
        # Try to refresh
        try:
            await keycloak.refresh_jwks()
            jwks = keycloak.get_cached_jwks()
        except Exception:
            pass

    try:
        header = jose_jwt.get_unverified_header(kc_token)
        kid = header.get("kid", "")
        key = keycloak.find_key(kid)
        if key is None:
            # Accept unverified if we can't find the key — Keycloak already validated the password
            payload = jose_jwt.get_unverified_claims(kc_token)
        else:
            payload = jose_jwt.decode(
                kc_token, key, algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False},
            )
        return payload
    except JWTError as exc:
        log.warning("Could not decode Keycloak token: %s", exc)
        return jose_jwt.get_unverified_claims(kc_token)


@router.post("/login")
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange username + password for a bearer token.

    Accepts **both** local admin accounts AND Keycloak (society-events) credentials.
    Use the returned `access_token` in the Swagger UI **Authorize** dialog.
    """
    # ── 1. Try local admin_users table ───────────────────────────────────────
    user = await db.scalar(
        select(AdminUser).where(
            AdminUser.username == form.username,
            AdminUser.is_active.is_(True),
        )
    )
    if user and _verify_password(form.password, user.hashed_password):
        log.info("Local login: %s", user.username)
        return {
            "access_token": _make_token(user),
            "token_type": "bearer",
            "expires_in": _TOKEN_EXPIRE_HOURS * 3600,
            "username": user.username,
            "roles": user.roles,
            "auth_source": "local",
        }

    # ── 2. Fall back to Keycloak password grant ───────────────────────────────
    kc_payload = await _keycloak_password_grant(form.username, form.password)
    if kc_payload:
        username = kc_payload.get("preferred_username") or form.username
        sub = kc_payload.get("sub", form.username)
        roles: list[str] = kc_payload.get("realm_access", {}).get("roles", [])
        # Keep only meaningful roles; strip Keycloak internals
        app_roles = [r for r in roles if r in ("admin", "committee_member", "resident")]
        if not app_roles:
            app_roles = ["committee_member"]  # give basic access to valid Keycloak users
        token = _make_local_jwt(username, sub, app_roles)
        log.info("Keycloak login: %s, roles=%s", username, app_roles)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": _TOKEN_EXPIRE_HOURS * 3600,
            "username": username,
            "roles": app_roles,
            "auth_source": "keycloak",
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get("/me")
async def me(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all local admin users (no passwords)."""
    rows = (await db.execute(select(AdminUser).order_by(AdminUser.created_at))).scalars().all()
    return {
        "users": [
            {
                "id": str(u.id),
                "username": u.username,
                "roles": u.roles,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in rows
        ]
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    username: str,
    password: str,
    roles: list[str] | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Create a new local admin user."""
    existing = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    user = AdminUser(
        username=username,
        hashed_password=_hash_password(password),
        roles=roles or ["admin", "committee_member"],
    )
    db.add(user)
    await db.flush()
    return {"id": str(user.id), "username": user.username, "roles": user.roles}

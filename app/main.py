import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.auth.keycloak import fetch_jwks
from app.config import settings
from app.database import Base, engine
# Import channel model so SQLAlchemy registers it with Base.metadata before create_all.
from app.models import channel as _channel_models  # noqa: F401
from app.routers import audit, auth, channels, image_parse, parse, payment, reconcile, source, sse
from app.services.email_monitor import monitor_loop

_UI_PATH = Path(__file__).parent / "static" / "index.html"


async def _run_ddl_migrations() -> None:
    """Idempotent DDL migrations run outside SQLAlchemy transactions.

    ALTER TYPE ADD VALUE and ADD COLUMN IF NOT EXISTS must run outside a
    transaction block; asyncpg connections are autocommit by default.
    """
    url = str(settings.DATABASE_URL).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        # Add enum values (no-op if already present)
        for ddl in [
            "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'IN_PROGRESS'",
            "ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS 'IMAGE'",
        ]:
            try:
                await conn.execute(ddl)
            except Exception:
                pass  # type doesn't exist yet — create_all will build it with the value

        # Add extended audit/identity columns (all idempotent)
        for stmt in [
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS description TEXT",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS flat_number VARCHAR(32)",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS member_id VARCHAR(64)",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS payment_category VARCHAR(64)",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS expiry_at TIMESTAMPTZ",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS notify_email VARCHAR(256)",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS payment_metadata JSONB",
            "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS tags JSONB",
        ]:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
    finally:
        await conn.close()


async def _seed_admin() -> None:
    """Create the default admin user from .env on first startup if it doesn't exist."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.database import AsyncSessionLocal
    from app.models.payment import AdminUser
    from app.routers.auth import _hash_password

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(AdminUser).where(AdminUser.username == settings.ADMIN_USERNAME))
        if not existing:
            user = AdminUser(
                username=settings.ADMIN_USERNAME,
                hashed_password=_hash_password(settings.ADMIN_PASSWORD),
                roles=["admin", "committee_member"],
            )
            db.add(user)
            await db.commit()
            import logging
            logging.getLogger(__name__).info(
                "Seeded default admin user '%s'", settings.ADMIN_USERNAME
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_ddl_migrations()
    await _seed_admin()
    await fetch_jwks()
    monitor_task = asyncio.create_task(monitor_loop())
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Payment Reconciliation Service",
    description=(
        "Zero-fee UPI payment wrapper with AI-driven SMS/Email reconciliation.\n\n"
        "**Login:** Use `POST /auth/login` with `username` + `password`, "
        "then click the **Authorize 🔓** button above and paste the `access_token`.\n\n"
        "Default credentials: `admin` / `admin123` (change via `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env`)."
    ),
    version="1.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(payment.router)
app.include_router(parse.router)
app.include_router(image_parse.router)
app.include_router(reconcile.router)
app.include_router(audit.router)
app.include_router(source.router)
app.include_router(channels.router)
app.include_router(sse.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "payment-reconciliation"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dev_console():
    return HTMLResponse(_UI_PATH.read_text())

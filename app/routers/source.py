from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_roles
from app.database import get_db
from app.models.payment import NotificationSource
from app.schemas.payment import RegisterSourceRequest, RegisterSourceResponse

router = APIRouter(prefix="/registerSource", tags=["Sources"])


@router.post("", response_model=RegisterSourceResponse, status_code=status.HTTP_201_CREATED)
async def register_source(
    body: RegisterSourceRequest,
    db: AsyncSession = Depends(get_db),
    _: dict[str, Any] = Depends(require_roles("admin")),
):
    source = NotificationSource(
        name=body.name,
        source_type=body.source_type,
        identifier=body.identifier,
        bank_name=body.bank_name,
    )
    db.add(source)
    await db.flush()
    return RegisterSourceResponse.model_validate(source)

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice
from app.security import get_current_user, require_member

router = APIRouter(dependencies=[Depends(get_current_user)])          # protected at router level
open_router = APIRouter()

CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get("/tenants/{tenant_id}/invoices")
async def list_invoices(tenant_id: str, session: AsyncSession, member=Depends(require_member)) -> list:
    rows = await session.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
    return list(rows.scalars())


@open_router.get("/me")
async def me(user: CurrentUser) -> dict:                               # auth via Annotated alias
    return user


@open_router.get("/public-docs", dependencies=[Depends(get_current_user)])
async def docs_with_route_dependency() -> dict:
    return {}

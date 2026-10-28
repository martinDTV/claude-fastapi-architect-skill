from fastapi import APIRouter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Invoice

router = APIRouter()


@router.get("/tenants/{tenant_id}/invoices")
async def list_invoices(tenant_id: str, session: AsyncSession) -> list:
    rows = await session.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
    return list(rows.scalars())


@router.post("/invoices")
async def create_invoice(payload: dict, session: AsyncSession) -> dict:
    return payload


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


async def set_tenant_session_level(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(text("SET app.tenant_id = :tenant").bindparams(tenant=tenant_id))      # persists on the pooled connection
    await session.execute(text("SELECT set_config('app.tenant_id', :tenant, false)"), {"tenant": tenant_id})


async def set_tenant_transaction_local(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(text("SET LOCAL app.tenant_id = :tenant").bindparams(tenant=tenant_id))   # transaction-local: fine
    await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})

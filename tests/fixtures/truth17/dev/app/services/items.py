from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import RequestContext


async def list_for(session: AsyncSession, tenant_id: UUID) -> list:
    """Filters by whatever tenant it is given."""
    rows = await session.execute(text("select id from items where tenant_id = :t"), {"t": tenant_id})
    return list(rows.scalars())


async def list_for_principal(session: AsyncSession, ctx: RequestContext) -> list:
    """Filters by the principal's own tenant."""
    rows = await session.execute(text("select id from items where tenant_id = :t"), {"t": ctx.user.tenant_id})
    return list(rows.scalars())


async def build_report(tenant_id: UUID) -> None:
    """Background work with its own session and its own tenant context (recreated)."""
    from ..core.db import AsyncSessionLocal, set_tenant_context

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        await session.execute(text("insert into reports (tenant_id) values (:t)"), {"t": tenant_id})
        await session.commit()


async def reindex(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(text("update items set indexed = false where tenant_id = :t"), {"t": tenant_id})
    await session.commit()


async def legacy_context(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(text(f"SET app.tenant_id = '{tenant_id}'"))                 # truth: SEC_RLS_SET_NOT_LOCAL

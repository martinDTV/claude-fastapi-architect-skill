from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.errors import Conflict, NotFound
from ledger.modules.tenants.dependencies import TenantContext
from ledger.modules.tenants.models import Membership, Tenant, User
from ledger.modules.tenants.security import Principal


async def create_tenant(session: AsyncSession, principal: Principal, slug: str, name: str) -> Tenant:
    tenant = Tenant(slug=slug, name=name)
    session.add(tenant)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"Tenant slug '{slug}' already exists") from exc
    session.add(Membership(tenant_id=tenant.id, user_id=principal.user_id, role="owner"))
    await session.commit()
    return tenant


async def list_tenants(session: AsyncSession, principal: Principal) -> list[tuple[Tenant, str]]:
    rows = await session.execute(
        select(Tenant, Membership.role)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == principal.user_id)
        .order_by(Tenant.slug)
    )
    return [(tenant, role) for tenant, role in rows.all()]


async def add_member(session: AsyncSession, ctx: TenantContext, username: str, role: str) -> tuple[User, str]:
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise NotFound(f"User '{username}' not found")
    existing = await session.scalar(
        select(Membership).where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user.id)
    )
    if existing is not None:
        raise Conflict("Already a member")
    session.add(Membership(tenant_id=ctx.tenant_id, user_id=user.id, role=role))
    await session.commit()
    return user, role


async def tenant_or_404(session: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Tenant not found")
    return tenant

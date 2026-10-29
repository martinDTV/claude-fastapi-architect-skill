from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select

from ledger.core.db import SessionDep, set_tenant_context
from ledger.core.errors import Forbidden, NotFound
from ledger.modules.tenants.models import Membership
from ledger.modules.tenants.security import Principal, PrincipalDep


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    principal: Principal
    role: str


async def require_member(tenant_id: UUID, principal: PrincipalDep, session: SessionDep) -> TenantContext:
    """Binds the tenant in the path to the authenticated principal.

    Membership is checked server-side (the memberships table carries no RLS: it is the
    platform's own data). Non-members get 404, not 403, so tenant ids cannot be enumerated.
    Only after the check is the transaction-local RLS context set.
    """
    membership = await session.scalar(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == principal.user_id)
    )
    if membership is None:
        raise NotFound("Tenant not found")
    await set_tenant_context(session, tenant_id)
    return TenantContext(tenant_id=tenant_id, principal=principal, role=membership.role)


MemberDep = Annotated[TenantContext, Depends(require_member)]


def require_owner(ctx: MemberDep) -> TenantContext:
    if ctx.role != "owner":
        raise Forbidden("Owner role required")
    return ctx


OwnerDep = Annotated[TenantContext, Depends(require_owner)]

from fastapi import APIRouter, status

from ledger.core.db import SessionDep
from ledger.modules.tenants import service
from ledger.modules.tenants.dependencies import OwnerDep
from ledger.modules.tenants.schemas import MemberAdd, MemberRead, TenantCreate, TenantRead
from ledger.modules.tenants.security import PrincipalDep

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_tenant(payload: TenantCreate, principal: PrincipalDep, session: SessionDep) -> TenantRead:
    tenant = await service.create_tenant(session, principal, payload.slug, payload.name)
    return TenantRead(id=tenant.id, slug=tenant.slug, name=tenant.name, role="owner")


@router.get("")
async def list_my_tenants(principal: PrincipalDep, session: SessionDep) -> list[TenantRead]:
    return [
        TenantRead(id=t.id, slug=t.slug, name=t.name, role=role)
        for t, role in await service.list_tenants(session, principal)
    ]


@router.post("/{tenant_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(payload: MemberAdd, ctx: OwnerDep, session: SessionDep) -> MemberRead:
    user, role = await service.add_member(session, ctx, payload.username, payload.role)
    return MemberRead(user_id=user.id, username=user.username, role=role)

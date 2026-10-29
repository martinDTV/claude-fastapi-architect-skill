from uuid import UUID

from fastapi import APIRouter, Depends

from ...core import SessionDep, get_context, require_member   # relative import three levels up
from ...core.security import RequestContext, User
from . import service                                          # relative import of a module
from .models import Account

router = APIRouter()


async def require_admin(ctx: RequestContext = Depends(get_context)) -> User:
    if ctx.user.role != "admin":
        raise PermissionError
    return ctx.user


@router.get("/tenants/{tenant_id}/accounts")
async def list_accounts(tenant_id: UUID, member: User = Depends(require_member), session: SessionDep = None) -> dict:
    return await service.summary(session, tenant_id)                                   # truth-edge: app.modules.accounts.service.summary


@router.get("/tenants/{tenant_id}/summary")
async def tenant_summary(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return await service.summary(session, tenant_id)                                   # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL


@router.get("/tenants/{tenant_id}/summary-bound")
async def tenant_summary_bound(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return await service.summary_for(session, ctx, tenant_id)


@router.get("/tenants/{tenant_id}/members")
async def members(tenant_id: UUID, member: User = Depends(require_member), session: SessionDep = None) -> dict:  # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return await service.summary(session, tenant_id)


@router.post("/admin/users/{user_id}/role")
async def set_role(user_id: UUID, payload: dict, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    user = await session.get(User, user_id)
    user.role = payload["role"]
    await session.commit()
    return {"ok": True}


@router.post("/admin/users/{user_id}/role-guarded")
async def set_role_guarded(user_id: UUID, payload: dict, admin: User = Depends(require_admin), session: SessionDep = None) -> dict:  # truth-absent: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    user = await session.get(User, user_id)
    user.role = payload["role"]
    await session.commit()
    return {"ok": True}


@router.get("/accounts/{account_id}")
async def read_account(account_id: int, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    account = await session.get(Account, account_id)
    return {"id": account.id}

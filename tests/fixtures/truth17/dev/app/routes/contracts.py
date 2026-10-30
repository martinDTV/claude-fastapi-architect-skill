"""Authentication and authorization contracts: public, authenticated, role, permission, scopes,
resource relation through a service, a policy call, or nothing visible."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Security
from starlette.authentication import requires

from ..core import RequestContext, RoleChecker, SessionDep, User, get_audit_context, get_context, require, scoped_user
from ..core.policy import authorize, policy
from ..services import accounts

router = APIRouter()
admin_router = APIRouter(prefix="/ops", dependencies=[Depends(RoleChecker("admin"))])


@router.get("/health")
async def health() -> dict:                                                           # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"ok": True}


@router.post("/webhooks/provider")
async def provider_webhook(payload: dict) -> dict:                                    # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"received": len(payload)}                                                 # declared public in .audit-fastapi.json


@router.get("/pricing")  # audit-fastapi: public
async def pricing() -> dict:                                                          # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"plan": "free"}


@router.get("/orders")
async def orders() -> list:                                                           # truth: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return []


@router.get("/me")
async def me(ctx: RequestContext = Depends(get_audit_context)) -> dict:               # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"id": str(ctx.user.id)}


@router.get("/admin/stats")
async def admin_stats(user: User = Depends(RoleChecker("admin"))) -> dict:            # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"role": user.role}


@router.get("/reports")
async def reports(user: User = Security(scoped_user, scopes=["reports:read"])) -> dict:  # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"user": str(user.id)}


@admin_router.get("/queues")
async def queues() -> dict:                                                           # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {}


@router.post("/accounts/{account_id}/close")
async def close_account(account_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth: SEC_RESOURCE_AUTHZ_UNVERIFIED evidence=unknown
    await accounts.close(session, account_id)
    return {"closed": True}


@router.post("/accounts/{account_id}/freeze")
async def freeze_account(account_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    await accounts.freeze(session, ctx, account_id)
    return {"frozen": True}


@router.post("/accounts/{account_id}/archive")
async def archive_account(account_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    authorize(ctx.user, "archive", account_id)
    await accounts.archive(session, account_id)
    return {"archived": True}


@router.post("/accounts/{account_id}/tag")
async def tag_account(account_id: UUID, label: str, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    if not policy.can(ctx.user, "tag", account_id):
        raise PermissionError
    await accounts.tag(session, account_id, label)
    return {"tagged": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: UUID, ctx: RequestContext = Depends(require("accounts:delete")), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    await accounts.close(session, account_id)
    return {"deleted": True}


@router.get("/accounts/{account_id}/summary")
async def account_summary(account_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    return await accounts.compute_summary(session)                                   # the id is never used: nothing to relate


@router.patch("/users/{user_id}/role")
async def set_role(user_id: UUID, payload: dict, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> dict:  # truth: SEC_PRIVILEGE_CHANGE_UNVERIFIED evidence=unknown
    user = await session.get(User, user_id)
    user.role = payload["role"]
    await session.commit()
    return {}


@router.patch("/users/{user_id}/role-by-admin")
async def set_role_by_admin(user_id: UUID, payload: dict, admin: User = Depends(RoleChecker("admin")), session: SessionDep = None) -> dict:  # truth-absent: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    user = await session.get(User, user_id)
    user.role = payload["role"]
    await session.commit()
    return {}


@router.patch("/users/{user_id}/role-decorated")
@requires("admin")
async def set_role_decorated(user_id: UUID, payload: dict, session: SessionDep = None) -> dict:  # truth-absent: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    user = await session.get(User, user_id)
    user.role = payload["role"]
    await session.commit()
    return {}

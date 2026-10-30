"""Requested tenant, authenticated tenant, authorized tenant; RLS context and background work."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends

from ..core import RequestContext, SessionDep, User, get_context, require_member, set_tenant_context
from ..services import items

router = APIRouter()


@router.get("/tenants/{tenant_id}/invoices")
async def invoices(tenant_id: UUID, member: User = Depends(require_member), session: SessionDep = None) -> list:  # truth-absent: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_tenant_context(session, tenant_id)                                      # bound by require_member: fine
    return await items.list_for(session, tenant_id)                                   # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@router.get("/tenants/{tenant_id}/export")
async def export(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> list:  # truth: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_tenant_context(session, tenant_id)                                      # the requested tenant decides what RLS shows
    return await items.list_for(session, tenant_id)                                   # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@router.get("/tenants/{tenant_id}/summary")
async def summary(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> list:  # truth-absent: SEC_RLS_CONTEXT_FROM_REQUEST
    if tenant_id != ctx.user.tenant_id:
        raise PermissionError
    await set_tenant_context(session, tenant_id)                                      # compared with the principal first
    return await items.list_for(session, tenant_id)                                   # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@router.get("/tenants/{tenant_id}/safe")
async def safe(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> list:  # truth-absent: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_tenant_context(session, ctx.user.tenant_id)                             # context from the principal, not the URL
    return await items.list_for_principal(session, ctx)                               # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@router.get("/tenants/{tenant_id}/items")
async def tenant_items(tenant_id: UUID, ctx: RequestContext = Depends(get_context), session: SessionDep = None) -> list:  # truth: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL evidence=unknown
    return await items.list_for(session, tenant_id)                                   # filtered by the requested tenant, nothing binds it


@router.post("/tenants/{tenant_id}/reports")
async def schedule_report(tenant_id: UUID, background_tasks: BackgroundTasks, member: User = Depends(require_member), session: SessionDep = None) -> dict:
    background_tasks.add_task(items.build_report, tenant_id)                          # truth-absent: BG_TASK_CONTEXT_UNVERIFIED
    return {"scheduled": True}                                                        # truth-absent: BG_REQUEST_SCOPED_RESOURCE


@router.post("/tenants/{tenant_id}/reindex")
async def schedule_reindex(tenant_id: UUID, background_tasks: BackgroundTasks, member: User = Depends(require_member), session: SessionDep = None) -> dict:
    background_tasks.add_task(items.reindex, session, tenant_id)                      # truth: BG_REQUEST_SCOPED_RESOURCE
    return {"scheduled": True}

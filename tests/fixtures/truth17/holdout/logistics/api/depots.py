from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends

from ..core import Driver, SessionDep, current_driver, depot_member, set_depot_context
from ..services import shipments

router = APIRouter(prefix="/api/depots/{depot_id}", dependencies=[Depends(depot_member)])
open_router = APIRouter(prefix="/api/open")


@router.get("/shipments")
async def depot_shipments(depot_id: UUID, session: SessionDep = None) -> list:        # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    await set_depot_context(session, depot_id)                                        # truth-absent: SEC_RLS_CONTEXT_FROM_REQUEST
    return await shipments.list_for_depot(session, depot_id)


@open_router.get("/depots/{depot_id}/shipments")
async def any_depot_shipments(depot_id: UUID, driver: Driver = Depends(current_driver), session: SessionDep = None) -> list:  # truth: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_depot_context(session, depot_id)
    return await shipments.list_for_depot(session, depot_id)                          # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


async def rebuild_index(depot_id: UUID) -> None:
    from ..core.db import Sessions

    async with Sessions() as session:
        await set_depot_context(session, depot_id)
        await shipments.list_for_depot(session, depot_id)


@router.post("/reindex")
async def reindex(depot_id: UUID, tasks: BackgroundTasks, session: SessionDep = None) -> dict:
    tasks.add_task(rebuild_index, depot_id)                                           # truth-absent: BG_REQUEST_SCOPED_RESOURCE
    return {}


@router.post("/reindex-shared")
async def reindex_shared(depot_id: UUID, tasks: BackgroundTasks, session: SessionDep = None) -> dict:
    tasks.add_task(shipments.list_for_depot, session, depot_id)                       # truth: BG_REQUEST_SCOPED_RESOURCE
    return {}

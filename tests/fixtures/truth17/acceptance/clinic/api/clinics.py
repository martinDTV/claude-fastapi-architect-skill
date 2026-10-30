from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends

from ..core import SessionDep, Staff, clinic_member, current_staff, set_clinic_context
from ..core.db import SessionFactory
from ..services import records

router = APIRouter()
member_router = APIRouter(prefix="/clinics/{clinic_id}/members", dependencies=[Depends(clinic_member)])


@router.get("/clinics/{clinic_id}/records")
async def clinic_records(clinic_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> list:  # truth: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL evidence=unknown
    return await records.list_for_clinic(db, clinic_id)


@router.get("/clinics/{clinic_id}/records-bound")
async def clinic_records_bound(clinic_id: UUID, staff: Staff = Depends(clinic_member), db: SessionDep = None) -> list:  # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return await records.list_for_clinic(db, clinic_id)


@router.get("/clinics/{clinic_id}/records-compared")
async def clinic_records_compared(clinic_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> list:  # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    if staff.clinic_id != clinic_id:
        raise PermissionError
    return await records.list_for_clinic(db, clinic_id)


@router.get("/clinics/{clinic_id}/rls")
async def clinic_rls(clinic_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> list:  # truth: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_clinic_context(db, clinic_id)
    return await records.list_for_clinic(db, clinic_id)                               # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@router.get("/clinics/{clinic_id}/rls-own")
async def clinic_rls_own(clinic_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> list:  # truth-absent: SEC_RLS_CONTEXT_FROM_REQUEST
    await set_clinic_context(db, staff.clinic_id)
    return await records.list_for_clinic(db, staff.clinic_id)                         # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL


@member_router.get("")
async def members(clinic_id: UUID, db: SessionDep = None) -> list:                    # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return await records.list_for_clinic(db, clinic_id)


async def export_clinic(clinic_id: UUID) -> None:
    async with SessionFactory() as db:
        await set_clinic_context(db, clinic_id)
        await records.list_for_clinic(db, clinic_id)


@router.post("/clinics/{clinic_id}/exports")
async def schedule_export(clinic_id: UUID, tasks: BackgroundTasks, staff: Staff = Depends(clinic_member), db: SessionDep = None) -> dict:
    tasks.add_task(export_clinic, clinic_id)                                          # truth-absent: BG_REQUEST_SCOPED_RESOURCE
    return {}


@router.post("/clinics/{clinic_id}/exports-shared")
async def schedule_export_shared(clinic_id: UUID, tasks: BackgroundTasks, staff: Staff = Depends(clinic_member), db: SessionDep = None) -> dict:
    tasks.add_task(records.list_for_clinic, db, clinic_id)                            # truth: BG_REQUEST_SCOPED_RESOURCE
    return {}

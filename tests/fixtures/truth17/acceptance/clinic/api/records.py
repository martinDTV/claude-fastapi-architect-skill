from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends

from ..core import Permission, SessionDep, Staff, current_staff, needs, staff_context, verify_patient_access
from ..services import records
from ..services.records import access

router = APIRouter()


@router.get("/public/schedule")
async def public_schedule() -> dict:                                                  # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {}


@router.get("/healthz")
async def healthz() -> dict:                                                          # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"ok": True}


@router.get("/directory")
async def directory() -> list:                                                        # truth: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return []


@router.get("/whoami")
async def whoami(ctx: dict = Depends(staff_context)) -> dict:                         # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"staff": str(ctx["staff"].id)}


@router.get("/records/{record_id}")
async def read_record(record_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> dict:  # truth: SEC_RESOURCE_AUTHZ_UNVERIFIED evidence=unknown
    return await records.get(db, record_id)


@router.get("/records/{record_id}/verified")
async def read_record_verified(record_id: UUID, staff: Staff = Depends(verify_patient_access), db: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    return await records.get(db, record_id)


@router.get("/records/{record_id}/scoped")
async def read_record_scoped(record_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    return await records.get_for(db, staff, record_id)


@router.get("/records/{record_id}/checked")
async def read_record_checked(record_id: UUID, staff: Staff = Depends(current_staff), db: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    access.check(staff, record_id)
    return await records.get(db, record_id)


@router.delete("/records/{record_id}")
async def delete_record(record_id: UUID, staff: Staff = Depends(Permission("records:delete")), db: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    await db.execute(records.text("delete from records where id = :id"), {"id": record_id})
    await db.commit()
    return {}


@router.post("/staff/{staff_id}/admin")
async def make_admin(staff_id: UUID, actor: Staff = Depends(current_staff), db: SessionDep = None) -> dict:  # truth: SEC_PRIVILEGE_CHANGE_UNVERIFIED evidence=unknown
    target = await db.get(Staff, staff_id)
    target.is_admin = True
    await db.commit()
    return {}


@router.post("/staff/{staff_id}/admin-scoped")
async def make_admin_scoped(staff_id: UUID, actor: Staff = Depends(needs("staff:admin")), db: SessionDep = None) -> dict:  # truth-absent: SEC_PRIVILEGE_CHANGE_UNVERIFIED
    target = await db.get(Staff, staff_id)
    target.is_admin = True
    await db.commit()
    return {}

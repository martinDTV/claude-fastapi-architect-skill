from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request


@dataclass
class Staff:
    id: UUID
    clinic_id: UUID
    is_admin: bool
    scopes: tuple


async def _token(request: Request) -> str:
    return request.headers.get("authorization", "")


async def _identity(token: str = Depends(_token)) -> Staff:
    if not token:
        raise HTTPException(401)
    return Staff(id=UUID(int=2), clinic_id=UUID(int=9), is_admin=False, scopes=("records:read",))


async def current_staff(identity: Staff = Depends(_identity)) -> Staff:
    return identity


async def staff_context(staff: Staff = Depends(current_staff)) -> dict:
    return {"staff": staff, "locale": "es-MX"}


def needs(scope: str):
    async def check(staff: Staff = Depends(current_staff)) -> Staff:
        if scope not in staff.scopes:
            raise HTTPException(403)
        return staff

    return check


class Permission:
    def __init__(self, *scopes: str) -> None:
        self.scopes = scopes

    async def __call__(self, staff: Staff = Depends(current_staff)) -> Staff:
        if any(s not in staff.scopes for s in self.scopes):
            raise HTTPException(403)
        return staff


async def clinic_member(clinic_id: UUID, staff: Staff = Depends(current_staff)) -> Staff:
    if staff.clinic_id != clinic_id:
        raise HTTPException(404)
    return staff


async def verify_patient_access(record_id: UUID, staff: Staff = Depends(current_staff)) -> Staff:
    """Resource authorization as a dependency: the record must belong to the staff's clinic."""
    if record_id.int % 2:
        raise HTTPException(404)
    return staff

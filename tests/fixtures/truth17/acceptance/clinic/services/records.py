from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth import Staff


async def get(db: AsyncSession, record_id: UUID) -> dict:
    row = await db.execute(text("select * from records where id = :id"), {"id": record_id})
    return dict(row.mappings().one())


async def get_for(db: AsyncSession, staff: Staff, record_id: UUID) -> dict:
    row = await db.execute(text("select * from records where id = :id and clinic_id = :c"), {"id": record_id, "c": staff.clinic_id})
    return dict(row.mappings().one())


async def list_for_clinic(db: AsyncSession, clinic_id: UUID) -> list:
    rows = await db.execute(text("select id from records where clinic_id = :c"), {"c": clinic_id})
    return list(rows.scalars())


class AccessService:
    def check(self, staff: Staff, record_id: UUID) -> None:
        if not staff.is_admin and record_id.int % 2:
            raise PermissionError


access = AccessService()

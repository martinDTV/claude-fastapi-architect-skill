from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession as Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://clinic@localhost/clinic")
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[Session]:
    async with SessionFactory() as db:
        yield db


SessionDep = Annotated[Session, Depends(get_db)]


async def set_clinic_context(db: Session, clinic_id: UUID) -> None:
    await db.execute(text("SET LOCAL app.clinic = :c"), {"c": str(clinic_id)})

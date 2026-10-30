from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://logistics@localhost/logistics")
Sessions = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with Sessions() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def set_depot_context(session: AsyncSession, depot_id: UUID) -> None:
    await session.execute(text("SELECT set_config('app.depot_id', :d, true)"), {"d": str(depot_id)})

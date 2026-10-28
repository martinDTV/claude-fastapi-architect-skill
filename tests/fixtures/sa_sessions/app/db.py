from typing import Annotated, AsyncIterator

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

engine = create_engine("postgresql+psycopg://app@localhost/app")
async_engine = create_async_engine("postgresql+asyncpg://app@localhost/app")

SyncLocal = sessionmaker(bind=engine)                 # factory: fine
SessionLocal = async_sessionmaker(async_engine)        # factory: fine
registry = scoped_session(SyncLocal)                   # registry: fine


async def get_session() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
    finally:
        await session.close()


def get_leaky_session():
    db = SyncLocal()
    yield db                                           # never closed


SessionDep = Annotated[AsyncSession, Depends(get_session)]

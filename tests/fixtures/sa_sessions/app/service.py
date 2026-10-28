import asyncio
from functools import lru_cache

from fastapi import APIRouter
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, SessionDep, SyncLocal, engine
from app.models import Item

router = APIRouter()


async def load(session: AsyncSession, item_id: int) -> Item:
    return await session.get(Item, item_id)


async def shared_across_tasks(session: AsyncSession) -> None:
    await asyncio.gather(load(session, 1), load(session, 2))        # same session, two tasks


async def shared_via_create_task(session: AsyncSession) -> None:
    task = asyncio.create_task(load(session, 1))                    # handed to a background task
    await session.execute(select(Item))
    await task


async def one_session_per_task() -> None:
    async def worker(item_id: int) -> None:
        async with SessionLocal() as session:
            await session.get(Item, item_id)

    await asyncio.gather(worker(1), worker(2))                      # correct: a session per task


async def missing_await(session: AsyncSession) -> None:
    session.execute(select(Item))                                   # coroutine never awaited
    await session.commit()
    session.add(Item())                                             # add() is sync: fine


async def sync_session_in_async(db: Session) -> None:
    db.execute(select(Item))                                        # blocking sync I/O on the loop
    await asyncio.to_thread(db.commit)                              # offloaded: fine


async def untyped(db) -> None:
    db.query(Item).all()                                            # legacy Query API: sync Session
    db.commit()                                                     # unknown receiver, no await


def returns_awaitable(session: AsyncSession):
    return session.execute(select(Item))                            # handed to the caller: fine


@router.get("/items")
async def route_creates_engine(session: SessionDep) -> list:
    local_engine = create_engine("sqlite://")                       # engine per request
    return []


@lru_cache
def get_engine():
    return create_engine("sqlite://")                               # cached factory: fine


def build_engine():
    return create_engine("sqlite://")                               # factory by name: fine


def init_db() -> None:
    Base.metadata.create_all(engine)                                # create_all in application code


def leaks_session() -> None:
    s = SyncLocal()
    s.add(Item())
    s.commit()                                                      # never closed


def closes_session() -> None:
    s = SyncLocal()
    try:
        s.commit()
    finally:
        s.close()


def hands_over() -> Session:
    s = SyncLocal()
    return s


def context_managed() -> None:
    with SyncLocal() as s:
        s.commit()

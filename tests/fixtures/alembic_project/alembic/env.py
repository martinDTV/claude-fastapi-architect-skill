import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine


def run_async_migrations() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost/app")   # by design in env.py
    asyncio.run(_run(engine))


async def _run(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: context.run_migrations())


run_async_migrations()

import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://wms:wms@localhost/wms")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Location:
    def __init__(self, code: str) -> None:
        self.code = code

    def describe(self) -> str:
        time.sleep(0.1)
        return self.code


async def load_location(session: AsyncSession, code: str) -> Location:
    return Location(code)


def normalise(code: str) -> str:
    return code.strip().upper()

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SyncLocal, async_engine

session = SyncLocal()                                  # instance shared as a global
async_session = AsyncSession(async_engine)             # instance shared as a global

if __name__ == "__main__":
    cli_session = SyncLocal()                          # script usage: not reported

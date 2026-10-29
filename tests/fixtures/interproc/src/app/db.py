from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://app@localhost/app")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    session = SessionLocal()
    try:
        yield session                      # no annotation: the kind comes from the yielded name
    finally:
        await session.close()

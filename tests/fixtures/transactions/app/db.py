from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://app@localhost/app")
SessionLocal = async_sessionmaker(engine)

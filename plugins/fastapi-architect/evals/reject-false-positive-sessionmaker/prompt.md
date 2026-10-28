---
name: reject-false-positive-sessionmaker
tags: [v1.4, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

A reviewer flagged this module of our FastAPI service as "shared global database session, must be removed":

engine = create_async_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

Is the reviewer right? What exactly would be wrong, and what would be fine?

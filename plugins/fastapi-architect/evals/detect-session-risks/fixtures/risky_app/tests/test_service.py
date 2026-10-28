from app.db import SessionLocal


async def test_smoke():
    async with SessionLocal() as session:
        assert session is not None

import asyncio


async def load_remote() -> dict:
    await asyncio.sleep(0)
    return {"status": "ok"}


def refresh_cache() -> dict:
    # Sync helper that drives the loop itself; fails when called from an async route.
    return asyncio.run(load_remote())

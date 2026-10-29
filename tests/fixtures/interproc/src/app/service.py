from app.models import Order
from app.repo import CachedOrderRepo


async def create(session, payload: dict) -> Order:
    order = Order()
    session.add(order)                      # add() is sync: fine
    session.commit()                        # missing await; type of `session` inferred from the route
    await session.flush()
    return order


async def bulk(session, items: list) -> None:
    for item in items:
        session.add(item)
        await session.commit()              # commit in loop on an inferred session


async def via_repo(session, oid: int):
    repo = CachedOrderRepo()
    return await repo.fetch(session, oid)   # instance of a project class, method through a base


def sync_helper(session) -> None:
    session.execute("select 1")             # sync def: not an event-loop concern

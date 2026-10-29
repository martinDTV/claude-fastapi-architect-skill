from app.models import Order


class OrderRepo:
    async def fetch(self, session, oid: int):
        return session.get(Order, oid)      # coroutine returned un-awaited from an async method


class CachedOrderRepo(OrderRepo):
    pass

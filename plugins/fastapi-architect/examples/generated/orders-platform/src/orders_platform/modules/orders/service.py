"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.core.errors import ConflictError, NotFoundError
from orders_platform.modules.orders.models import Order
from orders_platform.modules.orders.ports import PaymentsPort
from orders_platform.modules.orders.repository import OrderRepository
from orders_platform.modules.orders.schemas import OrderCreate


class OrderService:
    def __init__(self, repository: OrderRepository, session: AsyncSession, payments: PaymentsPort) -> None:
        self._repository = repository
        self._session = session
        self._payments = payments

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Order violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, tenant_id: UUID, data: OrderCreate) -> Order:
        order = Order(
            tenant_id=tenant_id,
            customer_id=data.customer_id,
            total_amount=data.total_amount,
            currency=data.currency,
            placed_at=data.placed_at,
        )
        await self._repository.add(order)
        await self._commit()
        await self._payments.send({"event": "order.created", "id": str(order.id)})
        return order

    async def get(self, tenant_id: UUID, order_id: UUID) -> Order:
        order = await self._repository.get(tenant_id, order_id)
        if order is None:
            raise NotFoundError(f"Order {order_id} not found")
        return order

    async def list(self, tenant_id: UUID, limit: int = 50, offset: int = 0) -> list[Order]:
        return await self._repository.list(tenant_id, limit, offset)

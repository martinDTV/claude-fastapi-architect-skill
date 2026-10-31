"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.core.errors import ConflictError, NotFoundError
from orders_platform.modules.customers.models import Customer
from orders_platform.modules.customers.repository import CustomerRepository
from orders_platform.modules.customers.schemas import CustomerCreate, CustomerUpdate


class CustomerService:
    def __init__(self, repository: CustomerRepository, session: AsyncSession) -> None:
        self._repository = repository
        self._session = session

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Customer violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, tenant_id: UUID, data: CustomerCreate) -> Customer:
        customer = Customer(tenant_id=tenant_id, email=data.email, display_name=data.display_name, active=data.active)
        await self._repository.add(customer)
        await self._commit()
        return customer

    async def get(self, tenant_id: UUID, customer_id: UUID) -> Customer:
        customer = await self._repository.get(tenant_id, customer_id)
        if customer is None:
            raise NotFoundError(f"Customer {customer_id} not found")
        return customer

    async def list(self, tenant_id: UUID, limit: int = 50, offset: int = 0) -> list[Customer]:
        return await self._repository.list(tenant_id, limit, offset)

    async def update(self, tenant_id: UUID, customer_id: UUID, data: CustomerUpdate) -> Customer:
        customer = await self.get(tenant_id, customer_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(customer, name, value)
        await self._repository.save(customer)
        await self._commit()
        return customer

    async def delete(self, tenant_id: UUID, customer_id: UUID) -> None:
        removed = await self._repository.delete(tenant_id, customer_id)
        if not removed:
            raise NotFoundError(f"Customer {customer_id} not found")
        await self._commit()

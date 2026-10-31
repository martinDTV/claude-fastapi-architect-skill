"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.core.errors import ConflictError, NotFoundError
from orders_platform.modules.billing.models import Invoice
from orders_platform.modules.billing.ports import NotificationsPort
from orders_platform.modules.billing.repository import InvoiceRepository
from orders_platform.modules.billing.schemas import InvoiceCreate


class InvoiceService:
    def __init__(self, repository: InvoiceRepository, session: AsyncSession, notifications: NotificationsPort) -> None:
        self._repository = repository
        self._session = session
        self._notifications = notifications

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Invoice violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, tenant_id: UUID, data: InvoiceCreate) -> Invoice:
        invoice = Invoice(tenant_id=tenant_id, order_id=data.order_id, amount=data.amount, issued_on=data.issued_on)
        await self._repository.add(invoice)
        await self._commit()
        await self._notifications.send({"event": "invoice.created", "id": str(invoice.id)})
        return invoice

    async def get(self, tenant_id: UUID, invoice_id: UUID) -> Invoice:
        invoice = await self._repository.get(tenant_id, invoice_id)
        if invoice is None:
            raise NotFoundError(f"Invoice {invoice_id} not found")
        return invoice

    async def list(self, tenant_id: UUID, limit: int = 50, offset: int = 0) -> list[Invoice]:
        return await self._repository.list(tenant_id, limit, offset)

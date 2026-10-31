"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from orders_platform.core.db import SessionDep
from orders_platform.modules.billing.ports import NotificationsPort
from orders_platform.modules.billing.repository import InvoiceRepository, SqlInvoiceRepository
from orders_platform.modules.billing.service import InvoiceService


def get_notifications_port(request: Request) -> NotificationsPort:
    return request.app.state.ports["notifications"]


def get_invoice_repository(session: SessionDep) -> InvoiceRepository:
    return SqlInvoiceRepository(session)


def get_invoice_service(
    repository: Annotated[InvoiceRepository, Depends(get_invoice_repository)],
    session: SessionDep,
    notifications: Annotated[NotificationsPort, Depends(get_notifications_port)],
) -> InvoiceService:
    return InvoiceService(repository, session, notifications)


InvoiceServiceDep = Annotated[InvoiceService, Depends(get_invoice_service)]

"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from orders_platform.core.db import SessionDep
from orders_platform.modules.orders.ports import PaymentsPort
from orders_platform.modules.orders.repository import OrderRepository, SqlOrderRepository
from orders_platform.modules.orders.service import OrderService


def get_payments_port(request: Request) -> PaymentsPort:
    return request.app.state.ports["payments"]


def get_order_repository(session: SessionDep) -> OrderRepository:
    return SqlOrderRepository(session)


def get_order_service(
    repository: Annotated[OrderRepository, Depends(get_order_repository)],
    session: SessionDep,
    payments: Annotated[PaymentsPort, Depends(get_payments_port)],
) -> OrderService:
    return OrderService(repository, session, payments)


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]

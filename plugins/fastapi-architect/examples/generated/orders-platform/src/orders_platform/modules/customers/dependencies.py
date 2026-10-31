"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends

from orders_platform.core.db import SessionDep
from orders_platform.modules.customers.repository import CustomerRepository, SqlCustomerRepository
from orders_platform.modules.customers.service import CustomerService


def get_customer_repository(session: SessionDep) -> CustomerRepository:
    return SqlCustomerRepository(session)


def get_customer_service(
    repository: Annotated[CustomerRepository, Depends(get_customer_repository)],
    session: SessionDep,
) -> CustomerService:
    return CustomerService(repository, session)


CustomerServiceDep = Annotated[CustomerService, Depends(get_customer_service)]

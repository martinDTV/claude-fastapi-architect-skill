"""Async engine, request-scoped sessions and the declarative base (SQLAlchemy 2, asyncpg).

Tables are created by Alembic migrations only (``alembic upgrade head``); nothing here calls ``create_all``.
The naming convention gives every constraint and index a deterministic name, so the migration scripts and the
models agree and the test suite can compare them.

Row-level security: the application connects as a role without BYPASSRLS, and ``get_tenant_session`` sets the
tenant context for the current transaction only (``set_config(..., true)``), from the authenticated principal
and never from the request. The policies see that tenant until commit or rollback; a connection returned to
the pool carries no context (reset on return), so the next request starts closed.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from tenant_projects.core.security import TenantPrincipalDep

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
TENANT_CONTEXT_KEY = "app.tenant_id"  # the GUC the policies read; set per transaction


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request (unit of work): the service commits; any error rolls the unit back."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_tenant_session(session: SessionDep, principal: TenantPrincipalDep) -> AsyncSession:
    """The request session with the tenant context of the authenticated principal, for this transaction only."""
    params = {"key": TENANT_CONTEXT_KEY, "tenant": str(principal.tenant_id)}
    await session.execute(text("SELECT set_config(:key, :tenant, true)"), params)
    return session


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]

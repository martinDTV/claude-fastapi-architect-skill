"""DEMO authentication: NOT FOR PRODUCTION.

The caller identifies itself with the ``X-Demo-User`` header and nothing else. It exists so
the example can demonstrate authorization, tenant isolation and concurrency without an
identity provider. Replace it with OIDC/JWT validation (see references/authz-multitenancy.md)
before any real deployment.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select

from ledger.core.db import SessionDep
from ledger.core.errors import Unauthenticated
from ledger.modules.tenants.models import User

DEMO_HEADER = "X-Demo-User"


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    username: str


async def get_principal(
    session: SessionDep,
    x_demo_user: Annotated[str | None, Header(alias=DEMO_HEADER)] = None,
) -> Principal:
    if not x_demo_user:
        raise Unauthenticated(f"Missing {DEMO_HEADER} header (demo authentication, not production)")
    user = await session.scalar(select(User).where(User.username == x_demo_user))
    if user is None:
        raise Unauthenticated("Unknown demo user")
    return Principal(user_id=user.id, username=user.username)


PrincipalDep = Annotated[Principal, Depends(get_principal)]

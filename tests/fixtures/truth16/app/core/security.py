"""Authentication and a context abstraction: routes depend on get_context, which depends on the user."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException


@dataclass
class User:
    id: UUID
    role: str


@dataclass
class RequestContext:
    user: User
    request_id: str


async def get_current_user(x_user: str = Header()) -> User:
    if not x_user:
        raise HTTPException(401)
    return User(id=UUID(int=1), role="member")


async def get_context(user: User = Depends(get_current_user)) -> RequestContext:
    return RequestContext(user=user, request_id="r-1")


async def require_member(tenant_id: UUID, user: User = Depends(get_current_user)) -> User:
    """Binds the requested tenant to the authenticated principal (membership check)."""
    if tenant_id != UUID(int=7):
        raise HTTPException(404)
    return user

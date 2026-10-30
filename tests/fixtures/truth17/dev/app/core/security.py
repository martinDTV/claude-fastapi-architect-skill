"""Authentication and several authorization styles: composed dependencies, a permission factory,
a class-based role checker, Security scopes, and a membership dependency that binds the tenant."""

from dataclasses import dataclass, field
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from fastapi.security import SecurityScopes


@dataclass
class User:
    id: UUID
    tenant_id: UUID
    role: str
    scopes: list[str] = field(default_factory=list)


@dataclass
class RequestContext:
    user: User
    request_id: str


async def get_current_user(x_user: str = Header()) -> User:
    if not x_user:
        raise HTTPException(401)
    return User(id=UUID(int=1), tenant_id=UUID(int=7), role="member")


async def get_context(user: User = Depends(get_current_user)) -> RequestContext:
    return RequestContext(user=user, request_id="r-1")


async def get_audit_context(ctx: RequestContext = Depends(get_context)) -> RequestContext:
    """Third level of the chain: routes that depend on it are still authenticated."""
    return ctx


def require(permission: str):
    """Permission factory: Depends(require("accounts:delete"))."""

    async def dependency(ctx: RequestContext = Depends(get_context)) -> RequestContext:
        if permission not in ctx.user.scopes:
            raise HTTPException(403)
        return ctx

    return dependency


class RoleChecker:
    """Class-based dependency: Depends(RoleChecker("admin"))."""

    def __init__(self, role: str) -> None:
        self.role = role

    async def __call__(self, user: User = Depends(get_current_user)) -> User:
        if user.role != self.role:
            raise HTTPException(403)
        return user


async def scoped_user(scopes: SecurityScopes, user: User = Depends(get_current_user)) -> User:
    """Security(scoped_user, scopes=[...])."""
    for scope in scopes.scopes:
        if scope not in user.scopes:
            raise HTTPException(403)
    return user


async def require_member(tenant_id: UUID, user: User = Depends(get_current_user)) -> User:
    """Binds the requested tenant to the authenticated principal."""
    if user.tenant_id != tenant_id:
        raise HTTPException(404)
    return user

"""Authorization policy declared by the contract (model roles_and_ownership): per-operation roles checked by a route
dependency before the use case runs, ownership of the entities declared owned checked by the service against the row
it loaded and by the repository query that loaded it. The router never decides; it only carries the principal. A
denial is 403; a resource outside the caller's visibility is 404 because the query does not return it, so existence
is not revealed across owners or tenants.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends

from tenant_projects.core.errors import ForbiddenError
from tenant_projects.core.security import Principal, TenantPrincipalDep

# operations absent from this map are open to every authenticated member of the tenant
OPERATION_ROLES: dict[str, frozenset[str]] = {
    "delete": frozenset({"admin"}),
}
ADMIN_ROLES: frozenset[str] = frozenset({"admin"})
OWNER_VISIBILITY = "tenant"  # tenant: members read, owner/admin modify; owner: only owner/admin read or modify


def allows_operation(principal: Principal, operation: str) -> bool:
    required = OPERATION_ROLES.get(operation)
    return not required or bool(principal.roles & required)


def is_admin(principal: Principal) -> bool:
    return bool(principal.roles & ADMIN_ROLES)


def read_scope(principal: Principal) -> str | None:
    """The owner a repository read must filter by; None when the principal sees the whole tenant."""
    if OWNER_VISIBILITY == "tenant" or is_admin(principal):
        return None
    return principal.subject


def deny(principal: Principal, operation: str, resource: str, reason: str) -> None:
    raise ForbiddenError(f"{operation} {resource}: {reason}")


def ensure_owner(principal: Principal, owner_id: str, operation: str, resource: str) -> None:
    """The caller owns the loaded row or holds an admin role; otherwise 403 (the row was visible, 404 would lie)."""
    if owner_id == principal.subject or is_admin(principal):
        return
    deny(principal, operation, resource, "not the owner")


def require_operation(operation: str) -> Callable[..., Principal]:
    def dependency(principal: TenantPrincipalDep) -> Principal:
        if not allows_operation(principal, operation):
            deny(principal, operation, "operation", f"needs one of {sorted(OPERATION_ROLES[operation])}")
        return principal

    return dependency


CanCreate = Annotated[Principal, Depends(require_operation("create"))]
CanGet = Annotated[Principal, Depends(require_operation("get"))]
CanList = Annotated[Principal, Depends(require_operation("list"))]
CanUpdate = Annotated[Principal, Depends(require_operation("update"))]
CanDelete = Annotated[Principal, Depends(require_operation("delete"))]

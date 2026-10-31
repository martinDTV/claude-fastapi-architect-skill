"""API keys bound to tenants (binding api_key_registry).

Every configured key is a grant: the tenant it belongs to, the subject it acts as and the roles it holds.
``require_api_key`` authenticates the caller and yields the principal; ``require_tenant_member`` additionally resolves
the tenant path parameter and refuses, with 403, any request that addresses another tenant. This is authentication
plus tenant membership; per-operation and per-resource authorization live in core/authz.py (when the contract
declares a policy) and row-level security in the database (when the contract declares it).
"""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from tenant_projects.core.config import ApiKeyGrant, SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the subject it acts as, the tenant it belongs to and the roles it holds."""

    subject: str
    tenant_id: UUID
    roles: frozenset[str]


TenantPrincipal = Principal  # name used by projects generated before the identity model


def _grant_for(presented: str, keys: dict[str, ApiKeyGrant]) -> tuple[str, ApiKeyGrant] | None:
    matched: tuple[str, ApiKeyGrant] | None = None
    for key, grant in keys.items():  # every key is compared, so timing does not reveal which keys exist
        if secrets.compare_digest(key, presented):
            matched = (key, grant)
    return matched


def _subject(key: str, grant: ApiKeyGrant) -> str:
    return grant.subject or "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> Principal:
    if not settings.api_keys:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API keys are not configured")
    found = _grant_for(x_api_key, settings.api_keys) if x_api_key else None
    if found is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
    key, grant = found
    return Principal(subject=_subject(key, grant), tenant_id=grant.tenant_id, roles=frozenset(grant.roles))


AuthenticatedPrincipal = Annotated[Principal, Depends(require_api_key)]


def require_tenant_member(tenant_id: UUID, principal: AuthenticatedPrincipal) -> Principal:
    if principal.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the API key is not a member of this tenant")
    return principal


TenantPrincipalDep = Annotated[Principal, Depends(require_tenant_member)]

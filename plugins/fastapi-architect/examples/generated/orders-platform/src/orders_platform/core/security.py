"""API keys bound to tenants (binding api_key_registry).

Every configured key belongs to exactly one tenant. ``require_api_key`` authenticates the caller and yields the
principal (the tenant its key is bound to); ``require_tenant_member`` additionally resolves the tenant path
parameter and refuses, with 403, any request that addresses another tenant. This is authentication plus
membership; it is not resource-level authorization and it is not row-level security.
"""

import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from orders_platform.core.config import SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


@dataclass(frozen=True)
class TenantPrincipal:
    tenant_id: UUID


def _tenant_for(presented: str, keys: dict[str, UUID]) -> UUID | None:
    matched: UUID | None = None
    for key, tenant_id in keys.items():  # every key is compared, so timing does not reveal which keys exist
        if secrets.compare_digest(key, presented):
            matched = tenant_id
    return matched


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> TenantPrincipal:
    if not settings.api_keys:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API keys are not configured")
    tenant_id = _tenant_for(x_api_key, settings.api_keys) if x_api_key else None
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
    return TenantPrincipal(tenant_id=tenant_id)


Principal = Annotated[TenantPrincipal, Depends(require_api_key)]


def require_tenant_member(tenant_id: UUID, principal: Principal) -> TenantPrincipal:
    if principal.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the API key is not a member of this tenant")
    return principal


TenantPrincipalDep = Annotated[TenantPrincipal, Depends(require_tenant_member)]

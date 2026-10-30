"""Tenant-scoped router, two levels below the protected router."""

from fastapi import APIRouter

router = APIRouter(tags=["tenants"])


@router.get("/tenants/{tenant_id}/things")
async def list_things(tenant_id: str, limit: int = 20) -> dict:
    return {"tenant": tenant_id, "limit": limit}

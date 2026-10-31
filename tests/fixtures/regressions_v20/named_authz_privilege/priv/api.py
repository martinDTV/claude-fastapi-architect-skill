from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from priv import store


async def get_current_user(x_user: str = Header()) -> dict:
    if not x_user:
        raise HTTPException(401)
    return {"sub": x_user, "role": "member"}


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Named like a policy; decides nothing."""
    return user


async def require_member(user: dict = Depends(get_current_user)) -> dict:
    """Named like a membership check; never receives the tenant."""
    return user


async def require_real_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403)
    return user


router = APIRouter()


@router.post("/users/{user_id}/role")
async def set_role(user_id: UUID, payload: dict, admin: dict = Depends(require_admin)) -> dict:
    await store.set_role(user_id, payload["role"])
    return {}


@router.post("/users/{user_id}/role-guarded")
async def set_role_guarded(user_id: UUID, payload: dict, admin: dict = Depends(require_real_admin)) -> dict:
    await store.set_role(user_id, payload["role"])
    return {}


@router.get("/tenants/{tenant_id}/items")
async def tenant_items(tenant_id: UUID, member: dict = Depends(require_member)) -> list:
    return await store.list_for(tenant_id)

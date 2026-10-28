from fastapi import Depends, HTTPException


def get_current_user():
    return {"id": 1, "tenants": {"t1"}}


def require_member(tenant_id: str, user=Depends(get_current_user)):
    if tenant_id not in user["tenants"]:
        raise HTTPException(status_code=404)
    return user

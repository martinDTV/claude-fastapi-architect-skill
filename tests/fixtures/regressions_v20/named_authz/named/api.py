"""Routes whose only protection is a dependency: one route per dependency of named/security.py."""

from uuid import UUID

from fastapi import APIRouter, Depends

from external_identity import current_superuser  # not part of this project: nothing about it can be verified
from named import service
from named.security import (
    RoleChecker,
    User,
    gate,
    get_admin_ctx,
    get_ctx,
    guard,
    require_admin,
    require_admin_none,
    require_any_token,
    require_authenticated,
    require_editor,
    require_nothing,
    require_owner,
    require_scope,
)

router = APIRouter(prefix="/api/items")
ops_router = APIRouter(prefix="/api/ops", dependencies=[Depends(require_admin)])
named_router = APIRouter(prefix="/api/named", dependencies=[Depends(require_nothing)])


@router.patch("/{item_id}/nothing")
async def patch_nothing(item_id: UUID, payload: dict, user: User = Depends(require_nothing)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/none")
async def patch_none(item_id: UUID, payload: dict, _: None = Depends(require_admin_none)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/any")
async def patch_any(item_id: UUID, payload: dict, user: User = Depends(require_any_token)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/authn")
async def patch_authn(item_id: UUID, payload: dict, user: User = Depends(require_authenticated)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/editor")
async def patch_editor(item_id: UUID, payload: dict, user: User = Depends(require_editor)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/owner")
async def patch_owner(item_id: UUID, payload: dict, _: None = Depends(require_owner)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/nested")
async def patch_nested(item_id: UUID, payload: dict, ctx: dict = Depends(get_ctx)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/external")
async def patch_external(item_id: UUID, payload: dict, user: User = Depends(current_superuser)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/admin")
async def patch_admin(item_id: UUID, payload: dict, admin: User = Depends(require_admin)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/scope")
async def patch_scope(item_id: UUID, payload: dict, user: User = Depends(require_scope("items:write"))) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/checker")
async def patch_checker(item_id: UUID, payload: dict, user: User = Depends(RoleChecker("admin"))) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/nested-ok")
async def patch_nested_ok(item_id: UUID, payload: dict, ctx: dict = Depends(get_admin_ctx)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/guard")
async def patch_guard(item_id: UUID, payload: dict, user: User = Depends(guard)) -> dict:
    return await service.update(item_id, payload)


@router.patch("/{item_id}/gate")
async def patch_gate(item_id: UUID, payload: dict, user: User = Depends(gate)) -> dict:
    return await service.update(item_id, payload)


@ops_router.patch("/{item_id}")
async def ops_patch(item_id: UUID, payload: dict) -> dict:
    return await service.update(item_id, payload)


@named_router.patch("/{item_id}")
async def named_patch(item_id: UUID, payload: dict) -> dict:
    return await service.update(item_id, payload)

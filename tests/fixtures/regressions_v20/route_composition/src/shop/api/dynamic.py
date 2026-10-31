"""Mounted with a prefix read from settings: the effective path cannot be known statically."""

from fastapi import APIRouter

router = APIRouter(tags=["dynamic"])


@router.get("/dyn")
async def dyn() -> dict:
    return {}

"""The protected root of the business API: dependencies here apply to every router included below."""

from fastapi import APIRouter, Depends

from shop.api.v1 import v1_router
from shop.security import require_api_key

api_router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
api_router.include_router(v1_router)

from fastapi import APIRouter

from app.modules.billing.public import price_for       # cross-module through the facade: fine

router = APIRouter()

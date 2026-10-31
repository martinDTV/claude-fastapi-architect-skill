"""Mounted twice by the application (/internal and /partner): only the partner mount is declared public."""

from fastapi import APIRouter

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{report_id}")
async def get_report(report_id: int) -> dict:
    return {"id": report_id}

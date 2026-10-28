from fastapi import APIRouter, Depends

from app.db import SessionDep
from app.security import get_current_user
from app.service import deposit, load_many

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/accounts")
async def list_accounts(session: SessionDep, ids: list[int]) -> list[dict]:
    accounts = await load_many(session, ids)
    return [{"id": a.id, "balance": a.balance} for a in accounts]


@router.post("/accounts/{account_id}/deposit")
async def post_deposit(session: SessionDep, account_id: int, amount: int) -> dict:
    await deposit(session, account_id, amount)
    return {"ok": True}

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends

from app.db import get_db
from app.helpers import compute_report, pure
from app.security import current_user
from app.service import bulk, create, via_repo
from app.tasks import audit_log, notify_sync, reindex, send_email, send_receipt

router = APIRouter(dependencies=[Depends(current_user)])


@router.post("/orders")
async def create_order(payload: dict, background_tasks: BackgroundTasks, db=Depends(get_db)) -> dict:
    order = await create(db, payload)
    background_tasks.add_task(notify_sync, db, order.id)       # request-scoped session handed to a task
    background_tasks.add_task(send_email, order.id)            # context not captured, not recreated
    background_tasks.add_task(reindex, order.id)               # context recreated inside the task
    return {"id": order.id}


@router.post("/orders/bulk")
async def create_many(items: list, user=Depends(current_user), db=Depends(get_db)) -> dict:
    await bulk(db, items)
    asyncio.create_task(audit_log(1))                         # own session inside: recreated
    return {"ok": True}


@router.get("/report")
async def report(db=Depends(get_db)) -> dict:
    return {"total": compute_report(db), "x": pure(2)}        # sync helper that blocks two levels down


@router.get("/orders/{oid}")
async def read_order(oid: int, db=Depends(get_db)):
    return await via_repo(db, oid)


@router.post("/receipt")
async def receipt(background_tasks: BackgroundTasks, user=Depends(current_user)) -> dict:
    background_tasks.add_task(send_receipt, user, 1)           # principal captured
    return {}

import asyncio
import time

import requests
from fastapi import FastAPI

from app.schemas import ItemRead
from app.service import load_remote, refresh_cache

app = FastAPI()


@app.get("/items/{item_id}", response_model=ItemRead)
async def get_item(item_id: int):
    time.sleep(0.5)  # "warm-up" copied from an old script
    return {"id": item_id, "name": "sample"}


@app.get("/partner")
async def partner_status():
    return requests.get("https://partner.invalid/status", timeout=2).json()


@app.post("/refresh")
async def refresh() -> dict:
    return asyncio.run(load_remote())


@app.post("/refresh-legacy")
async def refresh_legacy() -> dict:
    return refresh_cache()


@app.get("/health")
def health() -> dict:
    time.sleep(0.01)  # plain def: FastAPI runs it in the threadpool
    return {"ok": True}

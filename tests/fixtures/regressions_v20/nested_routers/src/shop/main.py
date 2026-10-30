from fastapi import FastAPI

from shop.api.plain import plain_router
from shop.api.router import api_router

app = FastAPI()
app.include_router(api_router)
app.include_router(plain_router)

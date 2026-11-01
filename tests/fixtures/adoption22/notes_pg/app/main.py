"""Application entry point (hand-written): one router mounted under /api."""
from fastapi import FastAPI

from app.routers.notes import router as notes_router

app = FastAPI(title="notes")
app.include_router(notes_router, prefix="/api/notes")


@app.get("/health")
def health():
    return {"ok": True}

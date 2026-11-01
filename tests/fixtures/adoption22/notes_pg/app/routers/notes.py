"""Notes router (hand-written): async handlers on the project's session dependency."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Note
from app.schemas import NoteIn, NoteOut

router = APIRouter(tags=["notes"])


@router.post("", response_model=NoteOut, status_code=201)
async def create_note(payload: NoteIn, session: AsyncSession = Depends(get_session)):
    note = Note(**payload.model_dump())
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


@router.get("", response_model=list[NoteOut])
async def list_notes(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Note).order_by(Note.created_at))
    return list(result.scalars().all())


@router.get("/{note_id}", response_model=NoteOut)
async def get_note(note_id: UUID, session: AsyncSession = Depends(get_session)):
    note = await session.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="no such note")   # explicit 404
    return note

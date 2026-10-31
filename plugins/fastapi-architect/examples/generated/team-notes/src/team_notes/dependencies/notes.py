"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from team_notes.repositories.notes import NoteRepository, TagRepository
from team_notes.services.notes import NoteService, TagService


def get_note_repository(request: Request) -> NoteRepository:
    return request.app.state.repositories["note"]


def get_note_service(repository: Annotated[NoteRepository, Depends(get_note_repository)]) -> NoteService:
    return NoteService(repository)


NoteServiceDep = Annotated[NoteService, Depends(get_note_service)]


def get_tag_repository(request: Request) -> TagRepository:
    return request.app.state.repositories["tag"]


def get_tag_service(repository: Annotated[TagRepository, Depends(get_tag_repository)]) -> TagService:
    return TagService(repository)


TagServiceDep = Annotated[TagService, Depends(get_tag_service)]

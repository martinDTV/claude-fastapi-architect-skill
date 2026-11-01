"""Adversarial fixture for span edits (v2.2): an application factory in the shapes hand-written projects use.

Decorators with keyword arguments, ``Annotated`` dependencies, a ``TYPE_CHECKING`` import block, nested routers,
a call with a comment inside it, a multi-line call with a trailing comma, a signature with ``*`` and defaults, a
nested class, an async handler, a ``**kwargs`` signature. Every line that is not touched by a primitive must
survive byte for byte; the tests compare the whole file outside the edited span.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, status

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

API_PREFIX = "/api/v1"
health = APIRouter(tags=["health"])
items = APIRouter(prefix="/items", tags=["items"])
admin = APIRouter(prefix="/admin")


def get_settings() -> dict[str, Any]:
    return {"debug": False}  # a trailing comment on a statement


SettingsDep = Annotated[dict[str, Any], Depends(get_settings)]


@health.get("/health", include_in_schema=False)
async def read_health() -> dict[str, str]:
    return {"status": "ok"}


@items.get("", response_model=list[dict], summary="List items")
async def list_items(
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return [{"limit": limit, "debug": settings["debug"]}]


@items.post("", status_code=status.HTTP_201_CREATED)
async def create_item(payload: dict, settings: SettingsDep) -> dict:
    return payload


def plain(a, b=2):
    return a + b


def keyword_only(a, *, flag: bool = False):
    return (a, flag)


def with_kwargs(a, **options):
    return options


def with_comment_in_call() -> APIRouter:
    router = APIRouter(
        prefix="/commented",  # the prefix is explained here
        tags=["commented"],
    )
    return router


class Container:
    class Inner:
        value = 1

    def method(self, x: int) -> int:
        return x * self.Inner.value

    def mount(self, app: FastAPI) -> None:
        app.include_router(admin)


def create_app() -> FastAPI:
    app = FastAPI(title="fixture")
    app.include_router(health)
    app.include_router(
        items,
        prefix=API_PREFIX,
    )
    app.state.ready = True
    return app

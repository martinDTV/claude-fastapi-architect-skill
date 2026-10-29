from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class DomainError(Exception):
    status = 400
    title = "Bad request"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail or self.title


class Unauthenticated(DomainError):
    status, title = 401, "Unauthenticated"


class Forbidden(DomainError):
    status, title = 403, "Forbidden"


class NotFound(DomainError):
    status, title = 404, "Not found"


class Conflict(DomainError):
    status, title = 409, "Conflict"


class PreconditionFailed(DomainError):
    status, title = 412, "Precondition failed"


class PreconditionRequired(DomainError):
    status, title = 428, "Precondition required"


def problem(status: int, title: str, detail: str, instance: str) -> JSONResponse:
    """RFC 9457 Problem Details body."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": instance,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail, str(request.url.path))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return problem(422, "Validation failed", detail, str(request.url.path))

"""Domain errors and their RFC 9457 problem-details responses: 403 denied, 404 unknown or invisible, 409 conflict, 503
infrastructure (a database that cannot be reached is an I/O failure, never a 401 or a 404).
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError


class DomainError(Exception):
    status = 400
    title = "Domain error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status = 404
    title = "Not found"


class ForbiddenError(DomainError):
    """The caller is authenticated but the policy denies this operation or this resource."""

    status = 403
    title = "Forbidden"


class ConflictError(DomainError):
    status = 409
    title = "Conflict"


class UnavailableError(DomainError):
    """An infrastructure dependency failed: distinct from a denial (403) and from a missing resource (404)."""

    status = 503
    title = "Service unavailable"


def problem(status: int, title: str, detail: str, instance: str) -> JSONResponse:
    content = {"type": "about:blank", "title": title, "status": status, "detail": detail, "instance": instance}
    return JSONResponse(status_code=status, content=content, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail, str(request.url.path))

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(OSError)
    async def _unavailable(request: Request, exc: Exception) -> JSONResponse:
        return problem(503, "Service unavailable", "the database is unavailable", str(request.url.path))

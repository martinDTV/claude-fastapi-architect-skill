"""Domain errors and their RFC 9457 problem-details responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    status = 400
    title = "Domain error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status = 404
    title = "Not found"


class ConflictError(DomainError):
    status = 409
    title = "Conflict"


def problem(status: int, title: str, detail: str, instance: str) -> JSONResponse:
    content = {"type": "about:blank", "title": title, "status": status, "detail": detail, "instance": instance}
    return JSONResponse(status_code=status, content=content, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail, str(request.url.path))

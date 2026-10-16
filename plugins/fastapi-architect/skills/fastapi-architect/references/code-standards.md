# Code standards

## Contents
- Tooling
- Typing
- Dependencies with Annotated
- Async correctness
- Schemas (Pydantic v2)
- Routers and API design
- Error handling (Problem Details)
- Services
- Background work
- Style rules

## Tooling

| Tool | Purpose | Command |
|---|---|---|
| `uv` | Dependencies, virtualenv, lockfile | `uv sync`, `uv add`, `uv run` |
| `ruff` | Formatting and linting (replaces black, isort, flake8, pyupgrade, bandit basics) | `uv run ruff format . && uv run ruff check --fix .` |
| `mypy` (strict, pydantic plugin) | Static typing | `uv run mypy src` |
| `import-linter` | Architectural boundaries | `uv run lint-imports` |
| `pytest` + `pytest-cov` | Tests | `uv run pytest --cov=app` |
| `pre-commit` | Run the above before each commit | `uv run pre-commit install` |

Configuration for all of them is in `assets/pyproject.toml` and `assets/pre-commit-config.yaml`. Commit `uv.lock`. CI must run the same commands as pre-commit, plus migrations against a real database.

## Typing

- Every function, including tests' fixtures and private helpers, has parameter and return annotations. `mypy --strict` passes with no blanket `ignore_errors`.
- Use built-in generics and unions: `list[int]`, `dict[str, Any]`, `X | None`. No `typing.List`, `Optional`.
- `Any` only at true boundaries (raw JSON from a third party) and converted to a model immediately.
- `# type: ignore` always with the specific error code and a reason comment.
- Prefer `collections.abc` (`Sequence`, `Mapping`, `AsyncIterator`) for parameters; return concrete types.

## Dependencies with Annotated

```python
from typing import Annotated

from fastapi import Depends

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
CustomerServiceDep = Annotated[CustomerService, Depends(get_customer_service)]


@router.get("/{customer_id}")
async def get_customer(
    customer_id: int, service: CustomerServiceDep, user: CurrentUserDep
) -> CustomerRead: ...
```

Why: the alias is declared once, reused everywhere, typed correctly for mypy, and swappable in tests with `app.dependency_overrides[get_customer_service] = ...`.

Rules:
- Dependencies that need cleanup use `yield` (session, external client lease).
- Dependencies are cached per request by default; the same `SessionDep` used by several dependencies in one request is the same session. This is what makes the service and the repository share a transaction.
- Do not use a DI container library. FastAPI's dependency system already provides scoping, overrides and composition.
- Router-level dependencies for cross-cutting checks: `APIRouter(dependencies=[Depends(require_admin)])`.

## Async correctness

FastAPI runs `async def` path operations on the event loop and `def` path operations in a threadpool.

| Inside the function | Declare it as |
|---|---|
| Only awaited non-blocking I/O (asyncpg, httpx.AsyncClient, aioboto3, redis.asyncio) | `async def` |
| Any blocking call (requests, psycopg2, boto3, sync SDKs, file I/O on large files, `time.sleep`) | `def`, or `async def` with `await anyio.to_thread.run_sync(fn, ...)` around the blocking part |
| CPU-heavy work (image processing, large pandas operations, crypto loops) | A background worker or a process pool, never the request path |

One blocking call inside `async def` freezes every concurrent request on that worker. Ruff's `ASYNC` rules catch common cases; review the rest manually.

Pick one database stack per project. With async SQLAlchemy, use an async driver (`asyncpg`, `aiosqlite`, `aiomysql`). Do not mix a sync session into async code paths.

## Schemas (Pydantic v2)

- One schema per intent: `XCreate` (input on POST), `XUpdate` (input on PATCH, all fields optional), `XRead` (output), `XPage` (paginated output). Never reuse an input model as output: output often has server-generated fields and input often must not accept them.
- `XRead` uses `model_config = ConfigDict(from_attributes=True)` and is built with `XRead.model_validate(orm_obj)`.
- Use constrained types and `Field` for validation at the edge: `EmailStr`, `HttpUrl`, `Field(min_length=..., max_length=..., ge=..., le=...)`, `Literal[...]` or `StrEnum` for closed sets.
- For PATCH, apply `data.model_dump(exclude_unset=True)` so omitted fields are not overwritten with `None`.
- Money is `Decimal`, never `float`. Timestamps are timezone-aware `datetime` in UTC.
- Do not put business logic in validators. Validators check shape and format; rules that depend on state (uniqueness, permissions, limits) belong in the service.
- Declare the return type of every path operation (`-> CustomerRead`). FastAPI uses it as the response model: it validates, filters extra attributes and documents the response. Use `response_model=` only when the returned object's type differs from the documented model.

## Routers and API design

- Routers are thin: parse input, call one service method, convert to the response schema. No queries, no business rules, no `try/except` mapping errors.
- One `APIRouter` per module with `prefix` and `tags`. All business routers mounted under `/api/v1`.
- Resource-oriented paths: `GET /customers`, `POST /customers`, `GET /customers/{id}`, `PATCH /customers/{id}`, `DELETE /customers/{id}`. Actions that are not CRUD become sub-resources (`POST /orders/{id}/cancellation`) rather than verbs in the path.
- Status codes: 200 read/update, 201 create (consider a `Location` header), 202 accepted for async work, 204 delete, 400/422 validation, 401 unauthenticated, 403 forbidden, 404 not found, 409 conflict, 429 rate limited.
- Every list endpoint is paginated with bounded `limit` (`Query(ge=1, le=100)`). Use offset pagination for small, admin-style lists; use cursor (keyset) pagination for large or frequently-written tables.
- Function names are descriptive verbs (`create_customer`) because they become OpenAPI `operationId`s and generated client method names.
- Use `summary`/docstrings and `responses={404: {...}}` on endpoints whose error cases matter to API consumers.

## Error handling (Problem Details)

Domain exceptions carry no HTTP knowledge. One place maps them to responses.

```python
# core/exceptions.py
class DomainError(Exception):
    """Base class for expected business errors."""

    code: str = "domain_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class PermissionDeniedError(DomainError):
    code = "permission_denied"


class BusinessRuleViolationError(DomainError):
    code = "business_rule_violation"
```

```python
# api/errors.py
import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    BusinessRuleViolationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
)

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    BusinessRuleViolationError: 422,  # Unprocessable Content; Starlette renamed this constant
}


def _problem(status_code: int, title: str, detail: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status_code,
            "detail": detail,
            "code": code,
        },
    )


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # pragma: no cover - registered for DomainError only
        raise exc
    status_code = next(
        (code for cls, code in _STATUS_BY_ERROR.items() if isinstance(exc, cls)),
        status.HTTP_400_BAD_REQUEST,
    )
    return _problem(status_code, type(exc).__name__, exc.detail, exc.code)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _problem(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "InternalServerError",
        "An unexpected error occurred.",
        "internal_error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
```

Rules:
- Never leak stack traces, SQL or internal identifiers in error responses.
- Log unexpected errors once, at the handler, with the request ID (see `operations.md`). Do not log-and-reraise at every layer.
- Validation errors (422) keep FastAPI's default format unless the project has a reason to unify them; if it does, add a handler for `RequestValidationError` with the same Problem Details shape.
- Use `HTTPException` only for purely HTTP concerns inside dependencies (missing auth header → 401).

## Services

- One public method per use case (`register`, `deactivate`, `place_order`), named after the business action, not after CRUD verbs when the action has meaning.
- The service is the transaction boundary: it calls `await session.commit()` once, at the end of a successful write use case. Repositories never commit.
- Services receive dependencies through `__init__`, built in `dependencies.py`. They do not reach for globals or `get_settings()` in the middle of logic; pass the settings value they need.
- Services return ORM objects or domain entities; routers convert to schemas. In Stage 2 modules, services return domain entities.
- Side effects on external systems (emails, webhooks, third-party calls) happen after commit, or through the outbox, so a rollback cannot leave an email sent for an order that does not exist.

## Background work

- `BackgroundTasks` only for short, best-effort work tied to the response (send a non-critical notification). It runs in the same process, after the response; it is lost if the process dies.
- Anything that must not be lost, takes more than a few seconds, or needs retries goes to a real queue worker (arq, Celery, Dramatiq, SQS consumer). Workers live in the same repo, import module `public.py` APIs, and have their own entry point (`src/app/worker.py`).
- Scheduled jobs: a scheduler process or the platform's scheduler (Kubernetes CronJob, cloud scheduler) invoking a CLI entry point. Not a loop started in `lifespan`.

## Style rules

- Absolute imports only (`from app.modules.customers.service import ...`).
- No `print`; use the configured logger. No f-strings in log calls: `logger.info("Created %s", customer_id)`.
- No bare `except:` or `except Exception: pass`.
- No mutable default arguments.
- No commented-out code in commits.
- Constants in `UPPER_SNAKE_CASE` at module top. No magic numbers in business rules; name them.
- Docstrings on public service methods and on anything non-obvious. Comments explain *why*, not *what*.
- Functions over ~40 lines or with more than ~5 parameters are a signal to split or introduce a parameter object.

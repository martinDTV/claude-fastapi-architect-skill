# Operations

## Contents
- Logging
- Request IDs and middleware
- Tracing and metrics
- Health checks
- Running the server
- Docker
- CI pipeline
- Deployment notes

## Logging

- Structured (JSON) logs in deployed environments, human-readable locally. Configure once in `core/logging.py`, called from `create_app()`.
- `structlog` or the standard library with a JSON formatter are both fine; pick one per project.
- Module-level loggers: `logger = logging.getLogger(__name__)`.
- Every log line in a request carries the request ID and, when available, the user subject and tenant (via `contextvars`).
- Levels: `DEBUG` for diagnostics off by default, `INFO` for business events (order placed), `WARNING` for recoverable anomalies, `ERROR`/`exception` once at the error handler.
- Never log secrets, tokens or personal data.

## Request IDs and middleware

Prefer pure ASGI middleware for anything on the hot path; `BaseHTTPMiddleware` adds overhead and has known limitations with streaming responses and background tasks.

```python
# core/request_id.py
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp, header: str = "x-request-id") -> None:
        self.app = app
        self.header = header.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(self.header)
        request_id = incoming.decode() if incoming else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((self.header, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
```

Register with `app.add_middleware(RequestIdMiddleware)`. Include `request_id_var.get()` in every log record through a logging filter or structlog processor. If a gateway already generates the ID, trust its header.

## Tracing and metrics

- OpenTelemetry for traces and metrics: `opentelemetry-instrumentation-fastapi`, plus instrumentation for SQLAlchemy and httpx. Export over OTLP to whatever backend the platform uses.
- Configure through standard `OTEL_*` environment variables; do not hard-code exporters.
- Propagate trace context on outgoing HTTP calls and in message headers, so Stage 3 traces connect across services.
- Minimum metrics: request rate, error rate and latency per route (RED), DB pool usage, queue depth for workers.

## Health checks

```python
# api/health.py
from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import SessionDep

router = APIRouter(prefix="/health", tags=["health"], include_in_schema=False)


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep, response: Response) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
```

- Liveness checks only the process. It must not depend on the database, or a database outage restarts every pod.
- Readiness checks critical dependencies so traffic stops while they are down.
- Health routes are not versioned and not in the public OpenAPI schema.

## Running the server

- Local: `uv run fastapi dev src/app/main.py`.
- Containers orchestrated by Kubernetes or similar: one Uvicorn process per container (`uvicorn app.main:app --host 0.0.0.0 --port 8000`), and scale with replicas. The orchestrator handles restarts and distribution.
- A single VM without an orchestrator: multiple workers (`fastapi run --workers N` or `uvicorn --workers N`), N around the number of cores.
- Behind a reverse proxy or load balancer: enable `--proxy-headers` and set `--forwarded-allow-ips` to the proxy's addresses so client IP and scheme are correct.
- Graceful shutdown: the `lifespan` cleanup disposes engines and closes clients; configure the orchestrator's termination grace period above the longest request.

## Docker

Use `assets/Dockerfile`: multi-stage build with `uv`, dependencies installed in a cached layer before the source is copied, runtime image without build tools, non-root user. Add a `.dockerignore` excluding `.venv`, `.git`, tests' caches and `.env`.

Migrations run from the same image with a different command (`alembic upgrade head`) as a separate job, before the new version receives traffic.

## CI pipeline

Minimum stages, failing fast:

1. `uv sync --frozen`
2. `ruff format --check` and `ruff check`
3. `mypy src`
4. `lint-imports`
5. `pytest --cov=app` with a PostgreSQL service or Testcontainers
6. `alembic upgrade head` on an empty database and `alembic check` for model/migration drift
7. Dependency vulnerability scan
8. Build and scan the image; push on the main branch

## Deployment notes

- Configuration only through environment variables and secrets; the same image runs in every environment.
- Expose `/api/v1` through a gateway or ingress handling TLS, rate limiting and, in Stage 3, routing between services.
- Keep the OpenAPI schema as an artifact of each release; diff it in CI to catch breaking API changes.
- Serverless targets (for example AWS Lambda) run the same app through an ASGI adapter. Create clients in `lifespan` or at cold start, keep the pool small (one connection per instance) and put a connection proxy in front of the database.

# Testing

## Contents
- Strategy
- Layout
- Fixtures: app, client, database
- Unit tests for services
- Integration tests for endpoints
- Faking external systems
- What not to do

## Strategy

| Test type | Scope | Database | Share of suite |
|---|---|---|---|
| Unit | Services, domain logic, pure functions | None (fakes) or real DB through a session fixture | Most tests in Stage 2 modules |
| Integration | HTTP endpoint → service → real database | Real PostgreSQL (container) with per-test rollback | Most tests in Stage 1 modules |
| Contract | Published OpenAPI and event schemas | None | Stage 3 |
| End-to-end | Deployed system | Real | A few smoke tests |

In Stage 1 the highest value per test is the integration test through HTTP against a real database: it covers routing, validation, the service and the SQL in one go. Do not mock SQLAlchemy.

Test against the same database engine as production. SQLite hides differences in types, constraints, locking and SQL dialect.

## Layout

```
tests/
├── conftest.py
├── factories.py          # builders for test data
├── unit/
│   └── customers/test_service.py
└── integration/
    └── customers/test_api.py
```

Mirror the module structure. Name tests after behavior: `test_register_rejects_duplicate_email`, not `test_register_2`.

## Fixtures: app, client, database

FastAPI's docs and this guide use the AnyIO pytest plugin, installed with FastAPI. Mark async tests with `@pytest.mark.anyio`, or set it for the whole directory with `pytestmark`.

```python
# tests/conftest.py
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.core.db import Base, get_session
from app.main import create_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    # Commits inside the code under test become SAVEPOINT releases; the outer
    # transaction is rolled back after each test, so tests never see each other's data.
    async with AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        yield session


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
```

Notes:
- The savepoint-per-test rollback works on PostgreSQL (and MySQL). With SQLite drivers it silently leaks data between tests unless extra event hooks are added, which is one more reason to test against the production engine.
- `ASGITransport` does not run the `lifespan`. That is fine here because the session dependency is overridden. If a test needs lifespan resources, wrap the app with `asgi-lifespan`'s `LifespanManager`.
- `create_all` is acceptable in tests for speed. Add one CI job that runs `alembic upgrade head` against an empty database to make sure migrations match the models (`alembic check` detects drift).
- Settings for tests come from environment variables set in `conftest.py` or a `.env.test`, with `environment=test`. Clear `get_settings.cache_clear()` if a test changes them.
- Session-scoped async fixtures require the AnyIO backend fixture to be session-scoped as shown.

## Unit tests for services

```python
# tests/unit/customers/test_service.py
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.customers.exceptions import EmailAlreadyRegisteredError
from app.modules.customers.repository import CustomerRepository
from app.modules.customers.schemas import CustomerCreate
from app.modules.customers.service import CustomerService

pytestmark = pytest.mark.anyio


@pytest.fixture
def service(session: AsyncSession) -> CustomerService:
    return CustomerService(session, CustomerRepository(session))


async def test_register_normalizes_email(service: CustomerService) -> None:
    customer = await service.register(CustomerCreate(email="Ana@Example.com", full_name="Ana"))
    assert customer.email == "ana@example.com"


async def test_register_rejects_duplicate_email(service: CustomerService) -> None:
    await service.register(CustomerCreate(email="ana@example.com", full_name="Ana"))
    with pytest.raises(EmailAlreadyRegisteredError):
        await service.register(CustomerCreate(email="ana@example.com", full_name="Ana 2"))
```

For Stage 2 modules, test the domain without any database and test the service with in-memory fakes implementing the ports:

```python
class FakePaymentGateway:
    def __init__(self) -> None:
        self.charges: list[tuple[Decimal, str]] = []

    async def charge(self, *, amount: Decimal, currency: str, token: str) -> str:
        self.charges.append((amount, currency))
        return f"fake-{len(self.charges)}"
```

Fakes are preferable to `unittest.mock` for ports: they are type-checked against the `Protocol` and do not couple tests to call details.

## Integration tests for endpoints

```python
# tests/integration/customers/test_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio


async def test_create_customer_returns_201(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/customers", json={"email": "ana@example.com", "full_name": "Ana"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ana@example.com"
    assert "id" in body


async def test_duplicate_email_returns_problem_details(client: AsyncClient) -> None:
    payload = {"email": "ana@example.com", "full_name": "Ana"}
    await client.post("/api/v1/customers", json=payload)

    response = await client.post("/api/v1/customers", json=payload)

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "email_already_registered"


async def test_unknown_customer_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/customers/999999")
    assert response.status_code == 404
```

Cover for each endpoint: the success path, validation failure, each domain error it can raise, and authorization (401 without a token, 403 with the wrong role).

## Faking external systems

- Override the dependency that provides the client or adapter: `app.dependency_overrides[get_payment_gateway] = lambda: FakePaymentGateway()`.
- For code that uses `httpx` directly, use `respx` or `httpx.MockTransport`.
- Never call real third-party services in the default test run. Put those tests behind a marker (`@pytest.mark.external`) excluded by default.

## What not to do

- Mocking the session, the repository's SQL, or SQLAlchemy objects. It tests the mock.
- Tests depending on execution order or on data created by another test.
- Sleeping in tests to wait for background work. Call the worker function directly.
- Asserting on full response bodies that include generated values (IDs, timestamps). Assert on the fields that matter.
- Chasing 100% coverage with assertion-free tests. Aim for every business rule and every error path to be covered; coverage around 85% to 90% is a healthy result, not a target to game.

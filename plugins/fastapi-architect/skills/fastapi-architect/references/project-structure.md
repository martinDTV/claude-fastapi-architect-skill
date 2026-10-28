# Project structure by stage

## Contents
- Stage 0: prototype
- Stage 1: modular monolith (default)
- Core files (main.py, config, db, errors)
- Anatomy of a module, with code
- Adding a new module: checklist
- Stage 2: a module with ports and adapters
- Stage 3: multiple services
- Naming conventions

All layouts use the `src/` layout: it prevents tests from accidentally importing the working directory instead of the installed package, and keeps tooling configuration simple.

## Stage 0: prototype

```
project/
├── src/app/
│   ├── __init__.py
│   ├── main.py        # app + routes
│   ├── schemas.py
│   └── config.py
├── tests/test_api.py
└── pyproject.toml
```

Still use `pydantic-settings`, typed schemas and a test file. Move to Stage 1 as soon as any Stage 1 signal appears; the migration is cheap at this size and expensive later.

## Stage 1: modular monolith (default)

```
project/
├── src/app/
│   ├── __init__.py
│   ├── main.py                 # create_app(), lifespan
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # Settings + get_settings()
│   │   ├── db.py               # Base, engine factory, get_session, SessionDep
│   │   ├── exceptions.py       # DomainError hierarchy (no HTTP)
│   │   ├── logging.py          # logging configuration
│   │   └── security.py         # token verification, CurrentUserDep
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py           # aggregates module routers under /api/v1
│   │   ├── errors.py           # exception handlers -> Problem Details
│   │   └── health.py           # /health/live, /health/ready
│   └── modules/
│       ├── __init__.py
│       ├── customers/
│       │   ├── __init__.py
│       │   ├── router.py
│       │   ├── schemas.py
│       │   ├── service.py
│       │   ├── repository.py
│       │   ├── models.py
│       │   ├── dependencies.py
│       │   ├── exceptions.py
│       │   └── public.py
│       └── orders/
│           └── ...
├── migrations/                 # Alembic (env.py imports every module's models)
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py             # app, client, db session fixtures
│   ├── unit/                   # services and pure logic, no I/O
│   │   └── customers/
│   └── integration/            # HTTP + real database
│       └── customers/
├── docs/adr/                   # architecture decision records
├── alembic.ini
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── .pre-commit-config.yaml
├── .env.example                # every setting, no real secrets
└── README.md
```

Files that are empty or unnecessary for a module can be omitted. A module with no persistence has no `models.py` or `repository.py`. Do not create files "for consistency".

## Core files

### `core/config.py`

```python
from enum import StrEnum
from functools import lru_cache

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "app"
    environment: Environment = Environment.LOCAL
    database_url: PostgresDsn
    db_pool_size: int = 5
    db_max_overflow: int = 10
    cors_origins: list[str] = []
    jwt_secret: SecretStr

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

### `core/db.py`

```python
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
```

Leaving the `async with` block closes the session, which rolls back anything not committed. That is why services commit explicitly and errors never leave half-written data.

### `main.py`

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.errors import register_exception_handlers
from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import create_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_engine(get_settings())
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
```

`expire_on_commit=False` is required with async sessions: otherwise touching an attribute after commit triggers an implicit lazy load, which fails outside a greenlet.

### `api/router.py`

```python
from fastapi import APIRouter

from app.modules.customers.router import router as customers_router

api_router = APIRouter()
api_router.include_router(customers_router)
```

### `core/exceptions.py` and `api/errors.py`

See `references/code-standards.md`, section "Error handling".

## Anatomy of a module

Example module `customers`. Every other module follows the same shape.

### `models.py`

```python
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

### `schemas.py`

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class CustomerCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)


class CustomerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    created_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerRead]
    total: int
    limit: int
    offset: int
```

### `exceptions.py`

```python
from app.core.exceptions import ConflictError, NotFoundError


class CustomerNotFoundError(NotFoundError):
    code = "customer_not_found"


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_already_registered"
```

### `repository.py`

```python
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.customers.models import Customer


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, customer_id: int) -> Customer | None:
        return await self._session.get(Customer, customer_id)

    async def get_by_email(self, email: str) -> Customer | None:
        stmt = select(Customer).where(Customer.email == email)
        return await self._session.scalar(stmt)

    async def list(self, *, limit: int, offset: int) -> tuple[Sequence[Customer], int]:
        total = await self._session.scalar(select(func.count()).select_from(Customer))
        stmt = select(Customer).order_by(Customer.id).limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return rows, total or 0

    async def add(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer
```

Repositories expose intention-revealing methods. Avoid generic `BaseRepository[T]` classes with `filter(**kwargs)`: every non-trivial query ends up bypassing them.

### `service.py`

```python
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.customers.exceptions import (
    CustomerNotFoundError,
    EmailAlreadyRegisteredError,
)
from app.modules.customers.models import Customer
from app.modules.customers.repository import CustomerRepository
from app.modules.customers.schemas import CustomerCreate, CustomerUpdate


class CustomerService:
    def __init__(self, session: AsyncSession, repository: CustomerRepository) -> None:
        self._session = session
        self._repository = repository

    async def register(self, data: CustomerCreate) -> Customer:
        email = data.email.lower()
        if await self._repository.get_by_email(email) is not None:
            raise EmailAlreadyRegisteredError(f"{email} is already registered")
        customer = await self._repository.add(Customer(email=email, full_name=data.full_name))
        await self._session.commit()
        return customer

    async def list(self, *, limit: int, offset: int) -> tuple[Sequence[Customer], int]:
        return await self._repository.list(limit=limit, offset=offset)

    async def get(self, customer_id: int) -> Customer:
        customer = await self._repository.get(customer_id)
        if customer is None:
            raise CustomerNotFoundError(f"Customer {customer_id} not found")
        return customer

    async def update(self, customer_id: int, data: CustomerUpdate) -> Customer:
        customer = await self.get(customer_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(customer, field, value)
        await self._session.commit()
        return customer
```

### `dependencies.py`

```python
from typing import Annotated

from fastapi import Depends

from app.core.db import SessionDep
from app.modules.customers.repository import CustomerRepository
from app.modules.customers.service import CustomerService


def get_customer_service(session: SessionDep) -> CustomerService:
    return CustomerService(session, CustomerRepository(session))


CustomerServiceDep = Annotated[CustomerService, Depends(get_customer_service)]
```

### `router.py`

```python
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.modules.customers.dependencies import CustomerServiceDep
from app.modules.customers.schemas import (
    CustomerCreate,
    CustomerPage,
    CustomerRead,
    CustomerUpdate,
)

router = APIRouter(prefix="/customers", tags=["customers"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_customer(payload: CustomerCreate, service: CustomerServiceDep) -> CustomerRead:
    customer = await service.register(payload)
    return CustomerRead.model_validate(customer)


@router.get("")
async def list_customers(
    service: CustomerServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CustomerPage:
    items, total = await service.list(limit=limit, offset=offset)
    return CustomerPage(
        items=[CustomerRead.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{customer_id}")
async def get_customer(customer_id: int, service: CustomerServiceDep) -> CustomerRead:
    return CustomerRead.model_validate(await service.get(customer_id))


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: int, payload: CustomerUpdate, service: CustomerServiceDep
) -> CustomerRead:
    return CustomerRead.model_validate(await service.update(customer_id, payload))
```

### `public.py`

Only create it when another module needs something from this one.

```python
from app.core.db import SessionDep
from app.modules.customers.repository import CustomerRepository


async def customer_is_active(session: SessionDep, customer_id: int) -> bool:
    customer = await CustomerRepository(session).get(customer_id)
    return customer is not None and customer.is_active
```

Keep the public surface small and return primitives or dedicated DTOs, never ORM objects: the moment another module holds your ORM object, it is coupled to your schema.

## Adding a new module: checklist

1. Create `modules/<name>/` with only the files it needs.
2. Register its router in `api/router.py`.
3. Import its `models` in `migrations/env.py` (or in a `models` registry module) so Alembic sees it.
4. Generate and review a migration: `uv run alembic revision --autogenerate -m "add <name>"`.
5. Add it to the import-linter `independence` and `layers` contracts.
6. Add unit tests for the service and integration tests for the endpoints.

## Stage 2: a module with ports and adapters

```
modules/payments/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── entities.py       # @dataclass(slots=True) Payment with behavior
│   └── rules.py          # pure functions: fees, state transitions
├── ports.py              # Protocols
├── adapters/
│   ├── __init__.py
│   ├── sqlalchemy_repository.py
│   ├── stripe_gateway.py
│   └── fake_gateway.py   # used by tests and local development
├── models.py             # ORM models, mapped to/from domain entities in the adapter
├── service.py
├── router.py
├── schemas.py
├── dependencies.py       # chooses adapters based on settings
├── exceptions.py
└── public.py
```

```python
# ports.py
from decimal import Decimal
from typing import Protocol

from app.modules.payments.domain.entities import Payment


class PaymentGateway(Protocol):
    async def charge(self, *, amount: Decimal, currency: str, token: str) -> str: ...


class PaymentRepository(Protocol):
    async def get(self, payment_id: int) -> Payment | None: ...
    async def save(self, payment: Payment) -> Payment: ...
```

The domain package imports nothing from FastAPI, SQLAlchemy, Pydantic or the adapters.

## Stage 3: multiple services

Prefer a monorepo while the same people work on several services; move to separate repos when ownership is genuinely separate.

```
platform/
├── services/
│   ├── core-api/           # Stage 1 or 2 layout, owns its database
│   └── billing-service/    # Stage 1 or 2 layout, owns its database
├── contracts/
│   ├── openapi/            # published HTTP contracts
│   └── events/             # event schemas (JSON Schema or AsyncAPI), versioned
├── libs/
│   └── observability/      # tiny, stable, infrastructure-only shared code
└── deploy/                 # Helm charts, Terraform, compose files
```

Never share a "common models" or "common domain" library between services: it recreates the coupling the split was meant to remove. Share contracts, not code.

## Naming conventions

- Packages and modules: `snake_case`, plural business nouns for modules (`customers`, `orders`).
- Classes: `PascalCase`. Schemas: `<Entity>Create`, `<Entity>Update`, `<Entity>Read`, `<Entity>Page`.
- Services: `<Entity>Service`. Repositories: `<Entity>Repository`. Ports: noun describing the capability (`PaymentGateway`), no `I` prefix.
- Dependency aliases: `<Thing>Dep` (`SessionDep`, `CurrentUserDep`, `CustomerServiceDep`).
- Exceptions: `<What>Error`, inheriting from the base classes in `core/exceptions.py`.
- Tables: plural `snake_case`. URL paths: plural, kebab-case for multiword (`/order-items`).

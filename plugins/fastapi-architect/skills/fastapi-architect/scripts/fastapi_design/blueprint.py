"""Blueprint: the files of a FastAPI backend derived from a contract and a recommendation (v2.0).

Pure functions from (spec, recommendation) to ``{relative path: content}``; nothing is written here. The same
business code is laid out per style:

* ``layered``: ``src/<pkg>/{api,schemas,services,repositories,models,dependencies}/<module>.py``;
* ``modular_monolith`` and ``microservices`` (one monolith per context as the first step):
  ``src/<pkg>/modules/<module>/{router,schemas,service,repository,models,dependencies}.py``;
* ``hexagonal``: the modular layout plus ``ports.py`` and ``adapters/`` in the integrating modules.

Every generated project has an app factory with lifespan, RFC 9457 problem details, a health route, settings
through pydantic-settings, an ``.audit-fastapi.json`` that declares what the design decided (public routes
when there is no authentication, tenant parameters, forbidden imports between modules, the layer that commits),
and tests through the HTTP boundary for every declared operation. Only the operations declared for an entity
are generated (schemas, repository methods, use cases, routes and tests). The generated code passes the ruff
rules it ships with (E, F, I, B, UP, ASYNC at 120 columns).

PostgreSQL contracts (increment 2) also get: SQLAlchemy 2 mapped classes with a naming convention, foreign keys
from declared ``references`` (composite ``(tenant_id, id)`` between tenant tables), an Alembic environment with a
deterministic initial migration (tables are created only by ``alembic upgrade head``), a request-scoped session
that rolls back on errors, services that turn a constraint violation on commit into a 409, and a test suite that
migrates an empty database, truncates between tests, and checks that the migrated schema matches the models.

Multi-tenant contracts are generated only with ``binding: api_key_registry``: every API key is bound to one
tenant, a dependency resolves the tenant path parameter and refuses any other tenant with 403, and every
repository call is scoped. Output is deterministic: sorted iteration, no timestamps, no random values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .advisor import STYLE_HEXAGONAL, STYLE_LAYERED, Recommendation
from .spec import EntitySpec, FieldSpec, IntegrationSpec, ModuleSpec, Spec

PY_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool", "uuid": "UUID", "datetime": "datetime", "date": "date", "decimal": "Decimal"}
SA_TYPES = {"str": "String", "int": "Integer", "float": "Float", "bool": "Boolean", "uuid": "Uuid", "datetime": "DateTime", "date": "Date", "decimal": "Numeric"}
SAMPLE = {"str": '"sample"', "int": "1", "float": "1.5", "bool": "True", "uuid": '"11111111-1111-4111-8111-111111111111"', "datetime": '"2026-01-01T00:00:00Z"', "date": '"2026-01-01"', "decimal": '"9.99"'}
WRONG = {"str": "123", "int": '"not-a-number"', "float": '"not-a-number"', "bool": '"maybe"', "uuid": '"not-a-uuid"', "datetime": '"yesterday"', "date": '"someday"', "decimal": '"many"'}
ECHOED_TYPES = ("str", "int", "bool", "uuid", "date")  # JSON round-trips these unchanged, so tests can compare them
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LINE = 120


@dataclass
class Blueprint:
    style: str
    package: str
    files: Dict[str, str]
    layout: List[str] = field(default_factory=list)  # tree lines for the report
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"style": self.style, "package": self.package, "files": sorted(self.files), "layout": list(self.layout), "notes": list(self.notes)}


class Layout:
    FOLDERS = {"router": "api", "schemas": "schemas", "service": "services", "repository": "repositories", "models": "models", "dependencies": "dependencies", "ports": "ports", "adapters": "adapters"}

    def __init__(self, spec: Spec, rec: Recommendation) -> None:
        self.pkg = spec.package
        self.style = rec.style
        self.by_layer = rec.style == STYLE_LAYERED

    def path(self, component: str, module: str) -> str:
        if self.by_layer:
            return f"src/{self.pkg}/{self.FOLDERS[component]}/{module}.py"
        if component == "adapters":
            return f"src/{self.pkg}/modules/{module}/adapters/__init__.py"
        return f"src/{self.pkg}/modules/{module}/{component}.py"

    def adapter_path(self, module: str, integration: str) -> str:
        if self.by_layer:
            return f"src/{self.pkg}/adapters/{module}_{integration}.py"
        return f"src/{self.pkg}/modules/{module}/adapters/{integration}.py"

    def imp(self, component: str, module: str) -> str:
        if self.by_layer:
            return f"{self.pkg}.{self.FOLDERS[component]}.{module}"
        return f"{self.pkg}.modules.{module}.{component}"

    def adapter_imp(self, module: str, integration: str) -> str:
        if self.by_layer:
            return f"{self.pkg}.adapters.{module}_{integration}"
        return f"{self.pkg}.modules.{module}.adapters.{integration}"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _sorted_imports(lines: Sequence[str]) -> List[str]:
    """isort order inside a group: ``import x`` statements first, then ``from x import y``, each by module name."""
    plain = sorted({line for line in lines if line.startswith("import ")}, key=lambda line: line.split()[1])
    froms = sorted({line for line in lines if line.startswith("from ")}, key=lambda line: (line.split()[1], line))
    return plain + froms


def _header(doc: str, stdlib: Sequence[str] = (), third_party: Sequence[str] = (), local: Sequence[str] = (), *, before_code: bool = False) -> List[str]:
    """Module docstring plus import groups (stdlib, third party, project), separated by blank lines. The block ends
    with one blank line; callers that continue with a ``def``/``class`` add the second one."""
    lines = [f'"""{doc}"""', ""]
    for group in (stdlib, third_party, local):
        if group:
            lines.extend(_sorted_imports(group))
            lines.append("")
    if before_code and lines[-1] == "" and len(lines) > 2:
        return lines
    return lines


def _call(head: str, items: Sequence[str], tail: str, indent: str = "") -> List[str]:
    """``head(item, item)tail`` on one line when it fits, one item per line otherwise."""
    one = f"{indent}{head}{', '.join(items)}{tail}"
    if len(one) <= LINE:
        return [one]
    return [f"{indent}{head}", *[f"{indent}    {item}," for item in items], f"{indent}{tail}"]


def _type_imports(fields: Sequence[FieldSpec], *, uuid: Sequence[str] = ("UUID",), with_created_at: bool = True) -> List[str]:
    types = {f.type for f in fields}
    out: List[str] = []
    dt = set()
    if with_created_at or "datetime" in types:
        dt.add("datetime")
    if "date" in types:
        dt.add("date")
    if dt:
        out.append("from datetime import " + ", ".join(sorted(dt)))
    if "decimal" in types:
        out.append("from decimal import Decimal")
    if uuid:
        out.append("from uuid import " + ", ".join(uuid))
    return out


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _constraints(f: FieldSpec) -> str:
    parts: List[str] = []
    if f.type == "str":
        parts.append("min_length=1")
        if f.max_length:
            parts.append(f"max_length={f.max_length}")
    if f.min_value is not None:
        parts.append(f"ge={f.min_value}")
    if f.max_value is not None:
        parts.append(f"le={f.max_value}")
    if f.description:
        parts.append(f"description={json.dumps(f.description)}")
    return ", ".join(parts)


def _annotation(f: FieldSpec, optional: bool = False) -> str:
    base = PY_TYPES[f.type]
    return f"{base} | None" if optional or not f.required else base


def _sample(f: FieldSpec) -> str:
    if f.type == "str" and f.max_length and f.max_length < 6:
        return '"' + "s" * f.max_length + '"'
    if f.type in ("int", "float", "decimal") and f.min_value is not None and f.min_value > (1.5 if f.type == "float" else 1):
        return f'"{f.min_value}"' if f.type == "decimal" else str(f.min_value)
    return SAMPLE[f.type]


def _changed_sample(f: FieldSpec) -> str:
    """A second valid value, different from ``_sample`` whenever the type allows it (used by the update test)."""
    if f.type == "str":
        return '"' + "u" * min(f.max_length or 6, 6) + '"'
    if f.type in ("int", "float"):
        base = float(_sample(f)) if f.type == "float" else int(_sample(f))
        nxt = base + 1
        if f.max_value is not None and nxt > f.max_value:
            nxt = base
        return str(nxt)
    if f.type == "bool":
        return "False"
    if f.type == "uuid":
        return '"22222222-2222-4222-8222-222222222222"'
    if f.type == "date":
        return '"2026-02-02"'
    if f.type == "datetime":
        return '"2026-02-02T00:00:00Z"'
    return '"19.99"'


def _tenant(spec: Spec) -> str:
    return spec.security["multi_tenant"]["tenant_param"] if spec.multi_tenant else ""


def _registry(spec: Spec) -> bool:
    return spec.multi_tenant and spec.security["multi_tenant"]["binding"] == "api_key_registry"


def _entity_index(spec: Spec) -> Dict[str, Tuple[ModuleSpec, EntitySpec]]:
    return {f"{m.name}.{e.name}": (m, e) for m in spec.modules for e in m.entities}


def _fk_fields(e: EntitySpec) -> List[FieldSpec]:
    return [f for f in e.fields if f.references]


def _ordered_entities(spec: Spec) -> List[Tuple[ModuleSpec, EntitySpec]]:
    """Entities in creation order: referenced tables before the tables that reference them (declaration order otherwise)."""
    index = _entity_index(spec)
    order: List[Tuple[ModuleSpec, EntitySpec]] = []
    done: set = set()

    def visit(key: str, stack: Tuple[str, ...]) -> None:
        if key in done or key in stack:
            return
        m, e = index[key]
        for f in _fk_fields(e):
            if f.references in index:
                visit(f.references, stack + (key,))
        done.add(key)
        order.append((m, e))

    for m in spec.modules:
        for e in m.entities:
            visit(f"{m.name}.{e.name}", ())
    return order


def _fk_name(table: str, columns: Sequence[str], referred: str) -> str:
    return f"fk_{table}_{'_'.join(columns)}_{referred}"


def _payload_literal(fields: Sequence[FieldSpec], name: str, indent: str = "") -> List[str]:
    items = [f'"{f.name}": {_sample(f)}' for f in fields]
    return _call(f"{name} = {{", items, "}", indent) if len(f"{name} = {{{', '.join(items)}}}") > LINE - len(indent) else [f"{indent}{name} = {{{', '.join(items)}}}"]


# --------------------------------------------------------------------------- #
# project files
# --------------------------------------------------------------------------- #


def _pyproject(spec: Spec, rec: Recommendation) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    deps = ['"fastapi>=0.115,<1"', '"uvicorn[standard]>=0.30"', '"pydantic>=2.7,<3"', '"pydantic-settings>=2.4,<3"']
    if pg:
        deps += ['"sqlalchemy[asyncio]>=2.0.36,<2.1"', '"asyncpg>=0.30,<0.31"', '"alembic>=1.13,<2"']
    if rec.style == STYLE_HEXAGONAL and any(i.kind == "http" for i in spec.integrations):
        deps.append('"httpx>=0.27,<1"')
    dev = ['"pytest>=8.3"', '"httpx>=0.27,<1"', '"anyio>=4.4"', '"ruff>=0.6"']
    modular = rec.style != STYLE_LAYERED and len(spec.modules) > 1
    if modular:
        dev.append('"import-linter>=2.0"')
    lines = [
        "[project]",
        f'name = "{spec.package.replace("_", "-")}"',
        'version = "0.1.0"',
        f"description = {json.dumps(spec.description)}",
        'requires-python = ">=3.12"',
        "dependencies = [",
        *[f"  {d}," for d in deps],
        "]",
        "",
        "[dependency-groups]",
        "dev = [",
        *[f"  {d}," for d in dev],
        "]",
        "",
        "[build-system]",
        'requires = ["hatchling>=1.25"]',
        'build-backend = "hatchling.build"',
        "",
        "[tool.hatch.build.targets.wheel]",
        f'packages = ["src/{spec.package}"]',
        "",
        "[tool.pytest.ini_options]",
        'testpaths = ["tests"]',
        'addopts = "-p no:cacheprovider"',
        'pythonpath = ["src", "."]',
        "",
        "[tool.ruff]",
        f"line-length = {LINE}",
        'target-version = "py312"',
        'src = ["src", "tests"]',
        "",
        "[tool.ruff.lint]",
        'select = ["E", "F", "I", "B", "UP", "ASYNC"]',
    ]
    if modular:
        lines += ["", "[tool.importlinter]", f'root_package = "{spec.package}"']
        for m in spec.modules:
            forbidden = [o.name for o in spec.modules if o.name != m.name and o.name not in m.depends_on]
            if not forbidden:
                continue
            name = f"{m.name} depends only on {', '.join(m.depends_on)}" if m.depends_on else f"{m.name} does not import other modules"
            lines += [
                "",
                "[[tool.importlinter.contracts]]",
                f'name = "{name}"',
                'type = "forbidden"',
                f'source_modules = ["{spec.package}.modules.{m.name}"]',
                "forbidden_modules = [",
                *[f'  "{spec.package}.modules.{o}",' for o in forbidden],
                "]",
            ]
    return "\n".join(lines) + "\n"


def _audit_config(spec: Spec, rec: Recommendation) -> str:
    config: Dict[str, object] = {}
    # effective paths (the auditor composes include_router prefixes since v2.0); without authentication every
    # business route is public by design and is declared as such
    config["public_routes"] = ["/health", "/api/v1/*"] if spec.security["authentication"] == "none" else ["/health"]
    if spec.multi_tenant:
        config["tenant_params"] = [_tenant(spec)]
    contracts: Dict[str, object] = {}
    if rec.style != STYLE_LAYERED and len(spec.modules) > 1:
        forbidden = []
        for m in spec.modules:
            for o in spec.modules:
                if o.name != m.name and o.name not in m.depends_on:
                    forbidden.append({"from": f"{spec.package}.modules.{m.name}", "to": f"{spec.package}.modules.{o.name}"})
        if forbidden:
            contracts["forbidden_imports"] = forbidden
    if spec.persistence["kind"] == "postgresql":
        contracts["commit_layers"] = ["service"]
    if spec.security["authentication"] != "none":
        contracts["route_auth_required"] = ["/api/*"]
    if contracts:
        config["contracts"] = contracts
    config["x-generated-by"] = "fastapi-architect design (v2.0): declarations of the design, reviewed by the auditor"
    return json.dumps(config, indent=2, sort_keys=True) + "\n"


def _gitignore() -> str:
    return ".venv/\n__pycache__/\n*.py[cod]\n.env\n.pytest_cache/\n.ruff_cache/\ndist/\n"


def _env_example(spec: Spec) -> str:
    prefix = spec.package.upper()
    lines = [f"{prefix}_APP_NAME={spec.name}", f"{prefix}_ENVIRONMENT=local"]
    if _registry(spec):
        lines.append(f'{prefix}_API_KEYS={{"replace-with-a-generated-key": "{TENANT_A}"}}')
    elif spec.security["authentication"] == "api_key":
        lines.append(f"{prefix}_API_KEY=replace-with-a-generated-key")
    if spec.persistence["kind"] == "postgresql":
        lines.append(f"{prefix}_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/{spec.package}")
    for i in spec.integrations:
        if i.kind == "http":
            lines.append(f"{prefix}_{i.name.upper()}_BASE_URL=http://localhost:8080")
    return "\n".join(lines) + "\n"


def _config(spec: Spec) -> str:
    prefix = spec.package.upper()
    api_key = spec.security["authentication"] == "api_key"
    registry = _registry(spec)
    fields = [f'    app_name: str = "{spec.name}"', '    environment: str = "local"']
    if registry:
        fields += [f"    # JSON object in {prefix}_API_KEYS mapping each key to the one tenant it is bound to (empty: nobody)", "    api_keys: dict[str, UUID] = {}"]
    elif api_key:
        fields += [f"    # no default secret: set {prefix}_API_KEY (the dependency refuses an empty key)", '    api_key: SecretStr = SecretStr("")']
    if spec.persistence["kind"] == "postgresql":
        fields += [f"    # credentials come from {prefix}_DATABASE_URL, never from defaults", f'    database_url: str = "postgresql+asyncpg://localhost:5432/{spec.package}"']
    for i in spec.integrations:
        if i.kind == "http":
            fields.append(f'    {i.name}_base_url: str = "http://localhost:8080"')
            fields.append(f"    {i.name}_timeout_s: float = 5.0")
    stdlib = ["from functools import lru_cache", "from typing import Annotated"] + (["from uuid import UUID"] if registry else [])
    third = ["from fastapi import Depends, Request"] + (["from pydantic import SecretStr"] if api_key and not registry else []) + ["from pydantic_settings import BaseSettings, SettingsConfigDict"]
    lines = _header(f"Settings: environment variables prefixed {prefix}_ (and an optional .env file); no secret has a default.", stdlib=stdlib, third_party=third)
    lines += [
        "",
        "class Settings(BaseSettings):",
        f'    model_config = SettingsConfigDict(env_prefix="{prefix}_", env_file=".env", extra="ignore")',
        "",
        *fields,
        "",
        "",
        "@lru_cache",
        "def get_settings() -> Settings:",
        '    """Settings read from the environment, used when the application is created without explicit settings."""',
        "    return Settings()",
        "",
        "",
        "def current_settings(request: Request) -> Settings:",
        '    """The settings the running application was created with (tests pass their own to create_app)."""',
        "    return request.app.state.settings",
        "",
        "",
        "SettingsDep = Annotated[Settings, Depends(current_settings)]",
    ]
    return "\n".join(lines) + "\n"


def _errors() -> str:
    return '''"""Domain errors and their RFC 9457 problem-details responses."""

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
'''


def _security(spec: Spec) -> str:
    if not _registry(spec):
        return f'''"""API-key authentication: a constant-time comparison against the configured key (header X-API-Key)."""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from {spec.package}.core.config import SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> None:
    expected = settings.api_key.get_secret_value()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API key is not configured")
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
'''
    tenant = _tenant(spec)
    return f'''"""API keys bound to tenants (binding api_key_registry).

Every configured key belongs to exactly one tenant. ``require_api_key`` authenticates the caller and yields the
principal (the tenant its key is bound to); ``require_tenant_member`` additionally resolves the tenant path
parameter and refuses, with 403, any request that addresses another tenant. This is authentication plus
membership; it is not resource-level authorization and it is not row-level security.
"""

import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from {spec.package}.core.config import SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


@dataclass(frozen=True)
class TenantPrincipal:
    tenant_id: UUID


def _tenant_for(presented: str, keys: dict[str, UUID]) -> UUID | None:
    matched: UUID | None = None
    for key, tenant_id in keys.items():  # every key is compared, so timing does not reveal which keys exist
        if secrets.compare_digest(key, presented):
            matched = tenant_id
    return matched


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> TenantPrincipal:
    if not settings.api_keys:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API keys are not configured")
    tenant_id = _tenant_for(x_api_key, settings.api_keys) if x_api_key else None
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
    return TenantPrincipal(tenant_id=tenant_id)


Principal = Annotated[TenantPrincipal, Depends(require_api_key)]


def require_tenant_member({tenant}: UUID, principal: Principal) -> TenantPrincipal:
    if principal.tenant_id != {tenant}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the API key is not a member of this tenant")
    return principal


TenantPrincipalDep = Annotated[TenantPrincipal, Depends(require_tenant_member)]
'''


def _db() -> str:
    return '''"""Async engine, request-scoped sessions and the declarative base (SQLAlchemy 2, asyncpg).

Tables are created by Alembic migrations only (``alembic upgrade head``); nothing here calls ``create_all``.
The naming convention gives every constraint and index a deterministic name, so the migration scripts and the
models agree and the test suite can compare them.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request (unit of work): the service commits; any error rolls the unit back."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
'''


def _ports_for(spec: Spec, rec: Recommendation, module: ModuleSpec) -> List[IntegrationSpec]:
    """Integrations that become ports of ``module`` under the hexagonal style (the integrating module, or the
    first ports module when the integration does not name one)."""
    if rec.style != STYLE_HEXAGONAL or module.name not in rec.ports_modules:
        return []
    names = {m.name for m in spec.modules}
    out: List[IntegrationSpec] = []
    for i in spec.integrations:
        target = i.module if i.module in names else rec.ports_modules[0]
        if target == module.name:
            out.append(i)
    return out


def _main(spec: Spec, rec: Recommendation, layout: Layout) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    ports = [(m, i) for m in spec.modules for i in _ports_for(spec, rec, m)]
    local: List[str] = [
        f"from {spec.package}.api.router import api_router",
        f"from {spec.package}.core.config import Settings, get_settings",
        f"from {spec.package}.core.errors import install_error_handlers",
    ]
    body: List[str] = []
    teardown: List[str] = []
    if pg:
        local.append(f"from {spec.package}.core.db import make_engine")
        body.append("app.state.engine = make_engine(settings.database_url)")
        body.append("app.state.session_factory = async_sessionmaker(app.state.engine, expire_on_commit=False)")
        teardown.append("await app.state.engine.dispose()")
    else:
        body.append("app.state.repositories = {}")
        for m in spec.modules:
            for e in m.entities:
                local.append(f"from {layout.imp('repository', m.name)} import InMemory{e.name}Repository")
                body.append(f'app.state.repositories["{e.snake}"] = InMemory{e.name}Repository()')
    for m, i in ports:
        cls = f"Http{_camel(i.name)}Adapter" if i.kind == "http" else f"Recording{_camel(i.name)}Adapter"
        local.append(f"from {layout.adapter_imp(m.name, i.name)} import {cls}")
        if i.kind == "http":
            body.append(f"{i.name}_adapter = {cls}(settings.{i.name}_base_url, settings.{i.name}_timeout_s)")
            body.append(f'app.state.ports.setdefault("{i.name}", {i.name}_adapter)')
        else:
            body.append(f'app.state.ports.setdefault("{i.name}", {cls}())')
    lines = _header(
        "Application factory: settings, lifespan-owned resources, error handlers, routers.",
        stdlib=["from collections.abc import AsyncIterator", "from contextlib import asynccontextmanager"],
        third_party=["from fastapi import FastAPI"] + (["from sqlalchemy.ext.asyncio import async_sessionmaker"] if pg else []),
        local=local,
    )
    params = ["settings: Settings | None = None"] + (["ports: dict[str, object] | None = None"] if ports else [])
    lines += [
        "",
        *_call("def create_app(", params, ") -> FastAPI:"),
        "    settings = settings or get_settings()",
        "",
        "    @asynccontextmanager",
        "    async def lifespan(app: FastAPI) -> AsyncIterator[None]:",
        *[f"        {line}" for line in body],
        "        yield",
        *[f"        {line}" for line in teardown],
        "",
        "    app = FastAPI(title=settings.app_name, lifespan=lifespan)",
        "    app.state.settings = settings",
        *(["    app.state.ports = dict(ports or {})"] if ports else []),
        "    install_error_handlers(app)",
        "    app.include_router(api_router)",
        "",
        '    @app.get("/health", tags=["ops"])',
        "    async def health() -> dict[str, str]:",
        '        return {"status": "ok"}',
        "",
        "    return app",
        "",
        "",
        "app = create_app()",
    ]
    return "\n".join(lines) + "\n"


def _api_router(spec: Spec, layout: Layout) -> str:
    api_key = spec.security["authentication"] == "api_key"
    local = [f"from {layout.imp('router', m.name)} import router as {m.name}_router" for m in spec.modules]
    if api_key:
        local.append(f"from {spec.package}.core.security import require_api_key")
    lines = _header(
        "Versioned API router: every business router is mounted under /api/v1" + (" behind the API key." if api_key else "."),
        third_party=["from fastapi import APIRouter, Depends" if api_key else "from fastapi import APIRouter"],
        local=local,
    )
    lines.append(f'api_router = APIRouter(prefix="/api/v1"{", dependencies=[Depends(require_api_key)]" if api_key else ""})')
    lines += [f"api_router.include_router({m.name}_router)" for m in spec.modules]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Alembic
# --------------------------------------------------------------------------- #


def _alembic_ini(spec: Spec) -> str:
    prefix = spec.package.upper()
    return f"""# Alembic configuration of {spec.name}. The database URL comes from {prefix}_DATABASE_URL (migrations/env.py);
# the value below is a credential-free fallback for `alembic ... --sql` (offline mode).
[alembic]
script_location = migrations
prepend_sys_path = src
file_template = %%(rev)s_%%(slug)s
sqlalchemy.url = postgresql+asyncpg://localhost:5432/{spec.package}

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
"""


def _alembic_env(spec: Spec, layout: Layout) -> str:
    prefix = spec.package.upper()
    model_imports = sorted(f"from {layout.imp('models', m.name)} import Base as _base_{m.name}  # noqa: F401" for m in spec.modules)
    return f'''"""Alembic environment: async engine, URL from {prefix}_DATABASE_URL, metadata of every generated model."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from {spec.package}.core.db import Base

# importing the model modules registers their tables on Base.metadata
{chr(10).join(model_imports)}

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("{prefix}_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={{"paramstyle": "named"}},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
'''


def _alembic_mako() -> str:
    return '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''


def _sa_type(f: FieldSpec) -> str:
    if f.type == "str":
        return f"sa.String(length={f.max_length or 255})"
    if f.type == "decimal":
        return "sa.Numeric(precision=18, scale=2)"
    if f.type == "datetime":
        return "sa.DateTime(timezone=True)"
    return f"sa.{SA_TYPES[f.type]}()"


def initial_revision(spec: Spec) -> str:
    """Deterministic revision id of the initial migration: derived from the contract digest, not from the clock."""
    return hashlib.sha1(("initial|" + spec.digest).encode("utf-8")).hexdigest()[:12]


def _fk_constraint(table: str, f: FieldSpec, referred: str, tenant: str, sa: str, indent: str) -> List[str]:
    """``ForeignKeyConstraint`` for a reference, composite over the tenant id between tenant tables."""
    cols = [tenant, f.name] if tenant else [f.name]
    refs = [f"{referred}.{c}" if c == tenant else f"{referred}.id" for c in cols]
    args = [json.dumps(cols), json.dumps(refs), f'name="{_fk_name(table, cols, referred)}"', f'ondelete="{f.on_delete.upper().replace("_", " ")}"']
    lines = _call(f"{sa}ForeignKeyConstraint(", args, ")", indent)
    lines[-1] += ","
    return lines


def _migration(spec: Spec) -> str:
    tenant = _tenant(spec)
    index = _entity_index(spec)
    revision = initial_revision(spec)
    up: List[str] = []
    down: List[str] = []
    for _m, e in _ordered_entities(spec):
        table = e.plural
        cols = ['        sa.Column("id", sa.Uuid(), nullable=False),']
        if tenant:
            cols.append(f'        sa.Column("{tenant}", sa.Uuid(), nullable=False),')
        for f in e.fields:
            cols.append(f'        sa.Column("{f.name}", {_sa_type(f)}, nullable={not f.required}),')
        cols.append('        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),')
        cols.append(f'        sa.PrimaryKeyConstraint("id", name="pk_{table}"),')
        if tenant:
            cols.append(f'        sa.UniqueConstraint("{tenant}", "id", name="uq_{table}_{tenant}_id"),')
        for f in _fk_fields(e):
            _tm, te = index[f.references]  # type: ignore[index]
            cols += _fk_constraint(table, f, te.plural, tenant, "sa.", "        ")
        up += ["    op.create_table(", f'        "{table}",', *cols, "    )"]
        if tenant:
            up.append(f'    op.create_index("ix_{table}_{tenant}", "{table}", ["{tenant}"], unique=False)')
        for f in _fk_fields(e):
            up.append(f'    op.create_index("ix_{table}_{f.name}", "{table}", ["{f.name}"], unique=False)')
        drop = [f'    op.drop_table("{table}")']
        for f in reversed(_fk_fields(e)):
            drop.insert(0, f'    op.drop_index("ix_{table}_{f.name}", table_name="{table}")')
        if tenant:
            drop.insert(0, f'    op.drop_index("ix_{table}_{tenant}", table_name="{table}")')
        down = drop + down
    lines = [
        f'"""Initial schema of {spec.name}: generated from the architecture contract (digest {spec.digest[:12]}).',
        "",
        f"Revision ID: {revision}",
        "Revises: none (initial)",
        "Create Date: derived from the contract, not from the clock (reproducible)",
        '"""',
        "",
        "import sqlalchemy as sa",
        "from alembic import op",
        "",
        f'revision = "{revision}"',
        "down_revision = None",
        "branch_labels = None",
        "depends_on = None",
        "",
        "",
        "def upgrade() -> None:",
        *up,
        "",
        "",
        "def downgrade() -> None:",
        *down,
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# per module
# --------------------------------------------------------------------------- #


def _ops(e: EntitySpec) -> Dict[str, bool]:
    ops = set(e.operations)
    return {"create": "create" in ops, "get": bool(ops & {"get", "update", "delete"}), "get_route": "get" in ops, "list": "list" in ops, "update": "update" in ops, "delete": "delete" in ops}


def _schemas(spec: Spec, module: ModuleSpec) -> str:
    tenant = _tenant(spec)
    all_fields = [f for e in module.entities for f in e.fields]
    lines = _header(
        "Request and response schemas (pydantic v2); unknown properties are rejected.",
        stdlib=_type_imports(all_fields),
        third_party=["from pydantic import BaseModel, ConfigDict, Field"],
    )
    for e in module.entities:
        ops = _ops(e)
        if ops["create"]:
            create_lines = []
            for f in e.fields:
                constraints = _constraints(f)
                if f.required and not constraints:
                    create_lines.append(f"    {f.name}: {_annotation(f)}")
                else:
                    args = ([] if f.required else ["default=None"]) + ([constraints] if constraints else [])
                    create_lines += _call(f"{f.name}: {_annotation(f)} = Field(", args, ")", "    ")
            lines += ["", f"class {e.name}Create(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *create_lines, ""]
        if ops["update"]:
            update_lines = []
            for f in e.fields:
                update_lines += _call(f"{f.name}: {_annotation(f, optional=True)} = Field(", ["default=None"] + ([_constraints(f)] if _constraints(f) else []), ")", "    ")
            lines += ["", f"class {e.name}Update(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *update_lines, ""]
        read_lines = ["    id: UUID"] + ([f"    {tenant}: UUID"] if tenant else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields] + ["    created_at: datetime"]
        lines += ["", f"class {e.name}Read(BaseModel):", "    model_config = ConfigDict(from_attributes=True)", *read_lines, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _models(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    all_fields = [f for e in module.entities for f in e.fields]
    if not pg:
        stdlib = ["from dataclasses import dataclass, field"]
        for line in _type_imports(all_fields, uuid=("UUID", "uuid4")):
            if line.startswith("from datetime import"):
                names = sorted(set(line[len("from datetime import ") :].split(", ")) | {"UTC"})
                line = "from datetime import " + ", ".join(names)
            stdlib.append(line)
        lines = _header("Entities of the module (plain dataclasses; the in-memory repository stores them as they are).", stdlib=stdlib)
        for e in module.entities:
            body = ([f"    {tenant}: UUID"] if tenant else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields if f.required]
            body += [f"    {f.name}: {_annotation(f)} = None" for f in e.fields if not f.required]
            body += ["    id: UUID = field(default_factory=uuid4)", "    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))"]
            lines += ["", "@dataclass", f"class {e.name}:", *body, ""]
        return "\n".join(lines).rstrip("\n") + "\n"
    index = _entity_index(spec)
    sa_names = {SA_TYPES[f.type] for f in all_fields} | {"DateTime", "Uuid"}
    if any(_fk_fields(e) for e in module.entities):
        sa_names.add("ForeignKeyConstraint")
    if tenant:
        sa_names.add("UniqueConstraint")
    lines = _header(
        "SQLAlchemy 2 mapped classes of the module (tables are created by Alembic, never by create_all).",
        stdlib=_type_imports(all_fields, uuid=("UUID", "uuid4")),
        third_party=[f"from sqlalchemy import {', '.join(sorted(sa_names))}, func", "from sqlalchemy.orm import Mapped, mapped_column"],
        local=[f"from {spec.package}.core.db import Base"],
    )
    for e in module.entities:
        table = e.plural
        body = [f'    __tablename__ = "{table}"']
        args: List[str] = []
        if tenant:
            args.append(f'        UniqueConstraint("{tenant}", "id", name="uq_{table}_{tenant}_id"),')
        for f in _fk_fields(e):
            _tm, te = index[f.references]  # type: ignore[index]
            args += _fk_constraint(table, f, te.plural, tenant, "", "        ")
        if args:
            body += ["    __table_args__ = (", *args, "    )"]
        body.append("    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)")
        if tenant:
            body.append(f"    {tenant}: Mapped[UUID] = mapped_column(Uuid, index=True)")
        for f in e.fields:
            col = SA_TYPES[f.type]
            arg = f"String({f.max_length or 255})" if f.type == "str" else ("Numeric(18, 2)" if f.type == "decimal" else ("DateTime(timezone=True)" if f.type == "datetime" else col))
            extras = ("" if f.required else ", nullable=True") + (", index=True" if f.references else "")
            body.append(f"    {f.name}: Mapped[{_annotation(f)}] = mapped_column({arg}{extras})")
        body.append("    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())")
        lines += ["", f"class {e.name}(Base):", *body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _repository(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    t_sig = f"{tenant}: UUID, " if tenant else ""
    t_arg = f"{tenant}, " if tenant else ""
    lines = _header(
        "Repositories: a Protocol per entity and the implementation chosen by the design.",
        stdlib=["from typing import Protocol", "from uuid import UUID"],
        third_party=["from sqlalchemy import select", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=[f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}"],
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        proto: List[str] = []
        if ops["create"]:
            proto.append(f"    async def add(self, {x}: {E}) -> {E}: ...")
        if ops["get"]:
            proto.append(f"    async def get(self, {t_sig}{x}_id: UUID) -> {E} | None: ...")
        if ops["list"]:
            proto.append(f"    async def list(self, {t_sig}limit: int, offset: int) -> list[{E}]: ...")
        if ops["update"]:
            proto.append(f"    async def save(self, {x}: {E}) -> {E}: ...")
        if ops["delete"]:
            proto.append(f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool: ...")
        lines += ["", f"class {E}Repository(Protocol):"] + "\n\n".join(proto).split("\n") + [""]
        impl: List[str] = []
        if not pg:
            impl += [f"class InMemory{E}Repository:", '    """Process-local store; the lifespan creates one per application, tests get a fresh one per app."""', "", "    def __init__(self) -> None:", f"        self._items: dict[UUID, {E}] = {{}}"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["get"]:
                if tenant:
                    impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E} | None:", f"        item = self._items.get({x}_id)", f"        return item if item is not None and item.{tenant} == {tenant} else None"]
                else:
                    impl += ["", f"    async def get(self, {x}_id: UUID) -> {E} | None:", f"        return self._items.get({x}_id)"]
            if ops["list"]:
                source = [f"        scoped = [item for item in self._items.values() if item.{tenant} == {tenant}]"] if tenant else ["        scoped = list(self._items.values())"]
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int) -> list[{E}]:", *source, "        items = sorted(scoped, key=lambda item: (item.created_at, str(item.id)))", "        return items[offset : offset + limit]"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", f"        del self._items[{x}_id]", "        return True"]
        else:
            where = f".where({E}.{tenant} == {tenant})" if tenant else ""
            impl += [f"class Sql{E}Repository:", '    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""', "", "    def __init__(self, session: AsyncSession) -> None:", "        self._session = session"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._session.add({x})", f"        return {x}"]
            if ops["get"]:
                impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E} | None:", f"        statement = select({E}).where({E}.id == {x}_id){where}", "        return await self._session.scalar(statement)"]
            if ops["list"]:
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int) -> list[{E}]:", f"        statement = select({E}){where}.order_by({E}.created_at, {E}.id)", "        rows = await self._session.scalars(statement.limit(limit).offset(offset))", "        return list(rows)"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        return {x}  # tracked by the session; written at commit"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", "        await self._session.delete(item)", "        return True"]
        lines += ["", *impl, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _service(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    t_sig = f"{tenant}: UUID, " if tenant else ""
    t_arg = f"{tenant}, " if tenant else ""
    schema_names: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + ([f"{e.name}Update"] if ops["update"] else [])
    local = [
        f"from {spec.package}.core.errors import {'ConflictError, ' if pg else ''}NotFoundError",
        f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}",
        f"from {layout.imp('repository', module.name)} import {', '.join(e.name + 'Repository' for e in module.entities)}",
    ]
    if schema_names:
        local.append(f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    lines = _header(
        "Use cases of the module: validation beyond the schema, domain errors, one commit per use case.",
        stdlib=["from uuid import UUID"],
        third_party=["from sqlalchemy.exc import IntegrityError", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=local,
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        ctor = [f"repository: {E}Repository"] + (["session: AsyncSession"] if pg else []) + [f"{p.name}: {_camel(p.name)}Port" for p in ports]
        assigns = ["        self._repository = repository"] + (["        self._session = session"] if pg else []) + [f"        self._{p.name} = {p.name}" for p in ports]
        commit = ["        await self._commit()"] if pg else []
        body = [f"class {E}Service:", *_call("def __init__(self, ", ctor, ") -> None:", "    "), *assigns]
        if pg:
            body += [
                "",
                "    async def _commit(self) -> None:",
                '        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""',
                "        try:",
                "            await self._session.commit()",
                "        except IntegrityError as exc:",
                "            await self._session.rollback()",
                f'            detail = f"{E} violates a database constraint: {{exc.orig}}"',
                "            raise ConflictError(detail) from exc",
            ]
        if ops["create"]:
            kwargs = ([f"{tenant}={tenant}"] if tenant else []) + [f"{f.name}=data.{f.name}" for f in e.fields]
            notify = [f'        await self._{p.name}.send({{"event": "{x}.created", "id": str({x}.id)}})' for p in ports]
            body += ["", f"    async def create(self, {t_sig}data: {E}Create) -> {E}:", *_call(f"{x} = {E}(", kwargs, ")", "        "), f"        await self._repository.add({x})", *commit, *notify, f"        return {x}"]
        if ops["get"]:
            body += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E}:", f"        {x} = await self._repository.get({t_arg}{x}_id)", f"        if {x} is None:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', f"        return {x}"]
        if ops["list"]:
            body += ["", f"    async def list(self, {t_sig}limit: int = 50, offset: int = 0) -> list[{E}]:", f"        return await self._repository.list({t_arg}limit, offset)"]
        if ops["update"]:
            body += ["", f"    async def update(self, {t_sig}{x}_id: UUID, data: {E}Update) -> {E}:", f"        {x} = await self.get({t_arg}{x}_id)", "        for name, value in data.model_dump(exclude_unset=True).items():", f"            setattr({x}, name, value)", f"        await self._repository.save({x})", *commit, f"        return {x}"]
        if ops["delete"]:
            body += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> None:", f"        removed = await self._repository.delete({t_arg}{x}_id)", "        if not removed:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', *commit]
        lines += ["", *body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _dependencies(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    local = [
        f"from {layout.imp('repository', module.name)} import {', '.join(f'{e.name}Repository' + (f', Sql{e.name}Repository' if pg else '') for e in module.entities)}",
        f"from {layout.imp('service', module.name)} import {', '.join(e.name + 'Service' for e in module.entities)}",
    ]
    if pg:
        local.append(f"from {spec.package}.core.db import SessionDep")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    needs_request = (not pg) or bool(ports)
    lines = _header(
        "Composition of the module: repositories, ports and services as FastAPI dependencies.",
        stdlib=["from typing import Annotated"],
        third_party=["from fastapi import Depends, Request" if needs_request else "from fastapi import Depends"],
        local=local,
    )
    for p in ports:
        lines += ["", f"def get_{p.name}_port(request: Request) -> {_camel(p.name)}Port:", f'    return request.app.state.ports["{p.name}"]', ""]
    for e in module.entities:
        E, x = e.name, e.snake
        port_args = [f"{p.name}: Annotated[{_camel(p.name)}Port, Depends(get_{p.name}_port)]" for p in ports]
        port_pass = "".join(f", {p.name}" for p in ports)
        if pg:
            repo = [f"def get_{x}_repository(session: SessionDep) -> {E}Repository:", f"    return Sql{E}Repository(session)"]
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]", "session: SessionDep"] + port_args
            call = f"{E}Service(repository, session{port_pass})"
        else:
            repo = [f"def get_{x}_repository(request: Request) -> {E}Repository:", f'    return request.app.state.repositories["{x}"]']
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]"] + port_args
            call = f"{E}Service(repository{port_pass})"
        lines += ["", *repo, "", "", *_call(f"def get_{x}_service(", args, f") -> {E}Service:"), f"    return {call}", "", "", f"{E}ServiceDep = Annotated[{E}Service, Depends(get_{x}_service)]", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _router(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    tenant = _tenant(spec)
    registry = _registry(spec)
    # with a tenant binding the tenant comes from the authenticated principal, never from a bare path parameter
    t_params = ["principal: TenantPrincipalDep"] if registry else ([f"{tenant}: UUID"] if tenant else [])
    t_arg = "principal.tenant_id, " if registry else (f"{tenant}, " if tenant else "")
    schema_names: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + [f"{e.name}Read"] + ([f"{e.name}Update"] if ops["update"] else [])
    local = [f"from {layout.imp('dependencies', module.name)} import {', '.join(e.name + 'ServiceDep' for e in module.entities)}", f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}"]
    if registry:
        local.append(f"from {spec.package}.core.security import TenantPrincipalDep")
    lines = _header(
        f"HTTP boundary of the {module.name} module: schema validation, status codes, problem details for domain errors.",
        stdlib=["from typing import Annotated", "from uuid import UUID"],
        third_party=["from fastapi import APIRouter, Query, status"],
        local=local,
    )
    lines.append(f'router = APIRouter(tags=["{module.name}"])')
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        prefix = (f"/tenants/{{{tenant}}}" if tenant else "") + f"/{e.plural}"
        if ops["create"]:
            lines += ["", "", f'@router.post("{prefix}", status_code=status.HTTP_201_CREATED)', *_call(f"async def create_{x}(", t_params + [f"payload: {E}Create", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.create({t_arg}payload))"]
        if ops["list"]:
            lines += ["", "", f'@router.get("{prefix}")', f"async def list_{e.plural}(", *[f"    {p}," for p in t_params], f"    service: {E}ServiceDep,", "    limit: Annotated[int, Query(ge=1, le=200)] = 50,", "    offset: Annotated[int, Query(ge=0)] = 0,", f") -> list[{E}Read]:", f"    return [{E}Read.model_validate(item) for item in await service.list({t_arg}limit, offset)]"]
        if ops["get_route"]:
            lines += ["", "", f'@router.get("{prefix}/{{{x}_id}}")', *_call(f"async def get_{x}(", t_params + [f"{x}_id: UUID", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.get({t_arg}{x}_id))"]
        if ops["update"]:
            lines += ["", "", f'@router.patch("{prefix}/{{{x}_id}}")', *_call(f"async def update_{x}(", t_params + [f"{x}_id: UUID", f"payload: {E}Update", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.update({t_arg}{x}_id, payload))"]
        if ops["delete"]:
            lines += ["", "", f'@router.delete("{prefix}/{{{x}_id}}", status_code=status.HTTP_204_NO_CONTENT)', *_call(f"async def delete_{x}(", t_params + [f"{x}_id: UUID", f"service: {E}ServiceDep"], ") -> None:"), f"    await service.delete({t_arg}{x}_id)"]
    return "\n".join(lines) + "\n"


def _ports(module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    lines = _header(f"Ports of the {module.name} module: what the use cases need from the outside, as Protocols.", stdlib=["from typing import Protocol"])
    for p in ports:
        lines += ["", f"class {_camel(p.name)}Port(Protocol):", f'    """{p.kind} integration ({p.direction}, criticality {p.criticality})."""', "", "    async def send(self, payload: dict[str, object]) -> None: ...", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _adapter(p: IntegrationSpec) -> str:
    cls = _camel(p.name)
    if p.kind == "http":
        return f'''"""HTTP adapter of the {p.name} port: one client per call with an explicit timeout; errors reach the use case."""

import httpx


class Http{cls}Adapter:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def send(self, payload: dict[str, object]) -> None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_s) as client:
            response = await client.post("/events", json=payload)
            response.raise_for_status()


class Recording{cls}Adapter:
    """Development and test adapter: records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
'''
    return f'''"""Adapter of the {p.name} port ({p.kind}): a recording implementation until the real transport is decided."""


class Recording{cls}Adapter:
    """Records what would have been sent; replace with the {p.kind} client once the integration is specified."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
'''


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def _conftest(spec: Spec, ports: List[Tuple[ModuleSpec, IntegrationSpec]], layout: Layout) -> str:
    api_key = spec.security["authentication"] == "api_key"
    registry = _registry(spec)
    pg = spec.persistence["kind"] == "postgresql"
    prefix = spec.package.upper()
    settings_kwargs = ['app_name="test"']
    if registry:
        settings_kwargs.append('api_keys={"key-a": TENANT_A, "key-b": TENANT_B}')
    elif api_key:
        settings_kwargs.append('api_key=SecretStr("test-key")')
    if pg:
        settings_kwargs.append("database_url=migrated")
    local = [f"from {spec.package}.core.config import Settings", f"from {spec.package}.main import create_app"]
    local += sorted({f"from {layout.adapter_imp(m.name, i.name)} import Recording{_camel(i.name)}Adapter" for m, i in ports})
    if pg:
        local.append(f"from {spec.package}.core.db import Base")
    ports_items = [f'"{i.name}": Recording{_camel(i.name)}Adapter()' for _m, i in ports]
    stdlib = (["import asyncio", "import os", "import subprocess", "import sys", "from pathlib import Path"] if pg else []) + (["from uuid import UUID"] if registry else [])
    third = ["import pytest", "from fastapi.testclient import TestClient"] + (["from pydantic import SecretStr"] if api_key and not registry else [])
    if pg:
        third += ["from sqlalchemy import inspect, text", "from sqlalchemy.ext.asyncio import create_async_engine"]
    doc = "Test fixtures: one application per test" + (" on a PostgreSQL database migrated by Alembic and truncated between tests." if pg else " with its own in-memory state.")
    lines = _header(doc, stdlib=stdlib, third_party=third, local=local)
    if registry:
        lines += [f'TENANT_A = UUID("{TENANT_A}")', f'TENANT_B = UUID("{TENANT_B}")']
    if pg:
        lines += [
            "PROJECT_ROOT = Path(__file__).resolve().parents[1]",
            "",
            "",
            "def run(coro):",
            "    return asyncio.run(coro)",
            "",
            "",
            "async def table_names(url: str) -> list[str]:",
            "    engine = create_async_engine(url)",
            "    try:",
            "        async with engine.connect() as connection:",
            "            return sorted(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))",
            "    finally:",
            "        await engine.dispose()",
            "",
            "",
            "async def truncate_all(url: str) -> None:",
            "    engine = create_async_engine(url)",
            "    try:",
            "        async with engine.begin() as connection:",
            '            tables = ", ".join(f\'"{t.name}"\' for t in Base.metadata.sorted_tables)',
            "            if tables:",
            '                await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))',
            "    finally:",
            "        await engine.dispose()",
            "",
            "",
            "def alembic(url: str, *args: str) -> subprocess.CompletedProcess:",
            f"    env = dict(os.environ, {prefix}_DATABASE_URL=url)",
            '    command = [sys.executable, "-m", "alembic", *args]',
            "    return subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)",
            "",
            "",
            '@pytest.fixture(scope="session")',
            "def database_url() -> str:",
            '    url = os.environ.get("DATABASE_URL")',
            "    if not url:",
            '        pytest.skip("set DATABASE_URL to an empty, disposable PostgreSQL database to run the API tests")',
            "    return url",
            "",
            "",
            '@pytest.fixture(scope="session")',
            "def migrated(database_url: str):",
            '    """Only an empty database is accepted: the suite migrates, truncates and downgrades it."""',
            "    existing = run(table_names(database_url))",
            "    if existing:",
            '        pytest.fail(f"DATABASE_URL must point at an empty database; found tables: {existing}")',
            '    result = alembic(database_url, "upgrade", "head")',
            "    assert result.returncode == 0, result.stderr",
            "    yield database_url",
            '    alembic(database_url, "downgrade", "base")',
            "",
            "",
            "@pytest.fixture(autouse=True)",
            "def clean_tables(migrated: str):",
            "    yield",
            "    run(truncate_all(migrated))",
        ]
    if lines[-1] != "":
        lines.append("")
    lines += ["", "@pytest.fixture", f"def app({'migrated: str' if pg else ''}):", *_call("settings = Settings(", settings_kwargs, ")", "    ")]
    if ports:
        lines += [*_call("ports = {", ports_items, "}", "    "), "    return create_app(settings, ports=ports)"]
    else:
        lines.append("    return create_app(settings)")
    lines += [
        "",
        "",
        "@pytest.fixture",
        "def client(app):",
        "    with TestClient(app) as test_client:",
        "        yield test_client",
        "",
        "",
        "@pytest.fixture",
        "def headers() -> dict[str, str]:",
        f"    return {'{' + chr(34) + 'X-API-Key' + chr(34) + ': ' + chr(34) + ('key-a' if registry else 'test-key') + chr(34) + '}' if api_key else '{}'}",
    ]
    if registry:
        lines += ["", "", "@pytest.fixture", "def tenant() -> UUID:", '    """The tenant bound to key-a (the default headers); key-b is bound to TENANT_B."""', "    return TENANT_A"]
    return "\n".join(lines) + "\n"


def _tests(spec: Spec, module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    api_key = spec.security["authentication"] == "api_key"
    registry = _registry(spec)
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    index = _entity_index(spec)
    item = 'f"{url}/{created[\'id\']}"'
    caller = "client, headers, tenant" if tenant else "client, headers"
    # helpers of other modules: create_<parent>(client, headers[, tenant]) builds a valid row with its own parents
    parent_helpers = sorted({(index[f.references][0].name, index[f.references][1].snake) for e in module.entities for f in _fk_fields(e) if pg and index[f.references][0].name != module.name})  # type: ignore[index]
    local = [f"from tests.test_{m} import {', '.join('create_' + x for _m, x in group)}" for m, group in _group(parent_helpers)]
    lines = _header(f"API tests of the {module.name} module through the HTTP boundary.", stdlib=["import uuid"], local=local)
    if registry:
        lines.append(f'OTHER_TENANT = uuid.UUID("{TENANT_B}")')
    first = True
    for e in module.entities:
        ops = _ops(e)
        x, up = e.snake, e.snake.upper()
        required = [f for f in e.fields if f.required]
        invalid = "{}" if required else "{" + f'"{e.fields[0].name}": {WRONG[e.fields[0].type]}' + "}"
        fk_fields = _fk_fields(e) if pg else []
        lines += ([] if first and not registry else [""]) + _payload_literal(e.fields, f"PAYLOAD_{up}")
        first = False
        if tenant:
            lines += ["", "", f"def {x}_url(tenant: uuid.UUID) -> str:", f'    return f"/api/v1/tenants/{{tenant}}/{e.plural}"']
            setup = [f"    url = {x}_url(tenant)"] if registry else ["    tenant = uuid.uuid4()", f"    url = {x}_url(tenant)"]
            fixtures_default = "client, headers, tenant" if registry else "client, headers"
        else:
            lines += [f'{up}_URL = "/api/v1/{e.plural}"']
            setup = [f"    url = {up}_URL"]
            fixtures_default = "client, headers"
        # payload_for_<x>: a valid payload whose references point at rows created through their own API;
        # create_<x>: posts it and returns the created row (used by the modules that reference this entity)
        if fk_fields:
            body = [f"    payload = dict(PAYLOAD_{up})"]
            for f in fk_fields:
                _pm, pe = index[f.references]  # type: ignore[index]
                body.append(f'    payload["{f.name}"] = create_{pe.snake}({caller})["id"]')
            body.append("    return payload")
        else:
            body = [f"    return dict(PAYLOAD_{up})"]
        lines += ["", "", f"def payload_for_{x}({caller}) -> dict:", *body]
        url_expr = f"{x}_url(tenant)" if tenant else f"{up}_URL"
        lines += ["", "", f"def create_{x}({caller}) -> dict:", f"    payload = payload_for_{x}({caller})", f"    response = client.post({url_expr}, json=payload, headers=headers)", "    assert response.status_code == 201, response.text", "    return response.json()"]
        prepare = [f"    payload = payload_for_{x}({caller})"]

        def test(name: str, body: List[str], fixtures: str = fixtures_default, with_payload: bool = True, with_url: bool = True) -> None:
            lines.extend(["", "", f"def {name}({fixtures}):", *(setup if with_url else []), *(prepare if with_payload else []), *body])

        if ops["create"]:
            echo = [f'    assert body["{f.name}"] == PAYLOAD_{up}["{f.name}"]' for f in required if f.type in ECHOED_TYPES and not f.references][:2]
            test(f"test_create_{x}", ["    response = client.post(url, json=payload, headers=headers)", "    assert response.status_code == 201, response.text", "    body = response.json()", '    assert uuid.UUID(body["id"])', *echo])
            test(f"test_create_{x}_rejects_invalid_payload", [f"    response = client.post(url, json={invalid}, headers=headers)", "    assert response.status_code == 422"], with_payload=False)
            test(f"test_create_{x}_rejects_unknown_properties", ['    response = client.post(url, json={**payload, "unexpected": 1}, headers=headers)', "    assert response.status_code == 422"])
            for f in fk_fields:
                body = [f'    payload["{f.name}"] = str(uuid.uuid4())', "    response = client.post(url, json=payload, headers=headers)", "    assert response.status_code == 409, response.text", '    assert response.headers["content-type"].startswith("application/problem+json")']
                if ops["list"]:
                    body.append("    assert client.get(url, headers=headers).json() == []  # nothing was persisted")
                test(f"test_create_{x}_with_unknown_{f.name}_is_a_conflict_and_leaves_no_row", body)
                if registry:
                    _pm, pe = index[f.references]  # type: ignore[index]
                    test(f"test_create_{x}_cannot_reference_a_{f.name}_of_another_tenant", [f'    foreign = create_{pe.snake}(client, {{"X-API-Key": "key-b"}}, OTHER_TENANT)', f'    payload["{f.name}"] = foreign["id"]', "    response = client.post(url, json=payload, headers=headers)", "    assert response.status_code == 409, response.text  # composite foreign key (tenant_id, id)"])
        if ops["get_route"] and ops["create"]:
            test(f"test_get_{x}_returns_the_created_one_and_404_for_unknown", ["    created = client.post(url, json=payload, headers=headers).json()", f"    found = client.get({item}, headers=headers)", "    assert found.status_code == 200", '    assert found.json()["id"] == created["id"]', '    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)', "    assert missing.status_code == 404", '    assert missing.headers["content-type"].startswith("application/problem+json")', '    assert missing.json()["status"] == 404'])
        if ops["list"] and ops["create"]:
            test(f"test_list_{e.plural}_paginates", ["    for _ in range(3):", "        assert client.post(url, json=payload, headers=headers).status_code == 201", '    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)', "    assert page.status_code == 200", "    assert len(page.json()) == 2", '    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422'])
        if ops["update"] and ops["create"]:
            f0 = next((f for f in e.fields if not f.references), e.fields[0])
            changed = _changed_sample(f0)
            check = [f'    assert updated.json()["{f0.name}"] == {changed}'] if f0.type in ECHOED_TYPES and not f0.references else []
            test(f"test_update_{x}", ["    created = client.post(url, json=payload, headers=headers).json()", f'    updated = client.patch({item}, json={{"{f0.name}": {changed}}}, headers=headers)', "    assert updated.status_code == 200, updated.text", *check, f'    assert client.patch({item}, json={{"unexpected": 1}}, headers=headers).status_code == 422'])
        if ops["delete"] and ops["create"]:
            after = f"    assert client.get({item}, headers=headers).status_code == 404" if ops["get_route"] else f"    assert client.delete({item}, headers=headers).status_code == 404"
            test(f"test_delete_{x}", ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.delete({item}, headers=headers).status_code == 204", after])
        if api_key and ops["create"]:
            test(f"test_{x}_routes_require_the_api_key", ["    assert client.post(url, json=payload).status_code == 401", '    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401'], fixtures=("client, headers, tenant" if registry else "client, headers"))
        if registry and ops["create"]:
            other = f"{x}_url(OTHER_TENANT)"
            body = [f"    assert client.post({other}, json=payload, headers=headers).status_code == 403"]
            if ops["list"]:
                body.append(f"    assert client.get({other}, headers=headers).status_code == 403")
            test(f"test_{x}_routes_refuse_another_tenant_for_the_same_key", body, with_url=False)
            if ops["list"]:
                test(f"test_{e.plural}_of_one_tenant_are_invisible_to_another", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f'    other = client.get({other}, headers={{"X-API-Key": "key-b"}})', "    assert other.status_code == 200", "    assert other.json() == []"])
        if ports and ops["create"]:
            p = ports[0]
            test(f"test_create_{x}_notifies_the_{p.name}_port", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f'    sent = app.state.ports["{p.name}"].sent', f'    assert sent and sent[-1]["event"] == "{x}.created"'], fixtures=("app, client, headers, tenant" if registry else "app, client, headers"))
    lines += ["", "", "def test_health_is_public(client):", '    assert client.get("/health").json() == {"status": "ok"}']
    return "\n".join(lines) + "\n"


def _group(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[str, List[Tuple[str, str]]]]:
    out: Dict[str, List[Tuple[str, str]]] = {}
    for module, entity in pairs:
        out.setdefault(module, []).append((module, entity))
    return sorted(out.items())


def _migration_tests(spec: Spec) -> str:
    tenant = _tenant(spec)
    index = _entity_index(spec)
    revision = initial_revision(spec)
    tables = sorted(e.plural for _m, e in _ordered_entities(spec))
    fks = []
    for _m, e in _ordered_entities(spec):
        for f in _fk_fields(e):
            _tm, te = index[f.references]  # type: ignore[index]
            fks.append((e.plural, _fk_name(e.plural, [tenant, f.name] if tenant else [f.name], te.plural)))
    lines = _header(
        "Migrations: the migrated schema is exactly what the models declare, constraints exist, and up/down is repeatable.",
        third_party=["from alembic.autogenerate import compare_metadata", "from alembic.config import Config", "from alembic.migration import MigrationContext", "from alembic.script import ScriptDirectory", "from sqlalchemy import inspect", "from sqlalchemy.ext.asyncio import create_async_engine"],
        local=[f"from {spec.package}.core.db import Base", "from tests.conftest import PROJECT_ROOT, alembic, run, table_names"],
    )
    lines += [
        f'REVISION = "{revision}"',
        *_call("TABLES = [", [json.dumps(t) for t in tables], "]"),
        *_call("FOREIGN_KEYS = [", [f"({json.dumps(t)}, {json.dumps(n)})" for t, n in fks], "]"),
        "",
        "",
        "async def _inspect(url: str, what):",
        "    engine = create_async_engine(url)",
        "    try:",
        "        async with engine.connect() as connection:",
        "            return await connection.run_sync(what)",
        "    finally:",
        "        await engine.dispose()",
        "",
        "",
        "def test_the_script_directory_has_one_head():",
        '    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))',
        "    assert script.get_heads() == [REVISION]",
        "",
        "",
        "def test_migrated_schema_matches_the_models(migrated: str):",
        '    """No drift: autogenerate would produce nothing (unexpected schema changes are detected here)."""',
        "",
        "    def diff(sync):",
        '        context = MigrationContext.configure(sync, opts={"compare_type": True})',
        "        return compare_metadata(context, Base.metadata)",
        "",
        "    assert run(_inspect(migrated, diff)) == []",
        "",
        "",
        "def test_tables_and_constraints_exist(migrated: str):",
        '    assert sorted(set(run(table_names(migrated))) - {"alembic_version"}) == TABLES',
        "",
        "    def constraints(sync):",
        "        inspector = inspect(sync)",
        "        found = []",
        "        for table in TABLES:",
        '            found += [(table, fk["name"]) for fk in inspector.get_foreign_keys(table)]',
    ]
    if tenant:
        lines += [
            '            unique = [sorted(uq["column_names"]) for uq in inspector.get_unique_constraints(table)]',
            f'            assert ["id", "{tenant}"] in unique, (table, unique)',
        ]
    lines += [
        "        return found",
        "",
        "    found = run(_inspect(migrated, constraints))",
        "    for expected in FOREIGN_KEYS:",
        "        assert expected in found, (expected, found)",
        "",
        "",
        "def test_upgrade_and_downgrade_are_repeatable(migrated: str):",
        '    assert alembic(migrated, "downgrade", "base").returncode == 0',
        '    assert run(table_names(migrated)) == ["alembic_version"]',
        '    assert alembic(migrated, "upgrade", "head").returncode == 0',
        '    assert sorted(set(run(table_names(migrated))) - {"alembic_version"}) == TABLES',
        '    current = alembic(migrated, "current")',
        "    assert current.returncode == 0 and REVISION in current.stdout + current.stderr",
    ]
    return "\n".join(lines) + "\n"


def _readme(spec: Spec, rec: Recommendation, layout_lines: List[str]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    prefix = spec.package.upper()
    rejected = "\n".join(f"- `{r['style']}`: {r['reason']}" for r in rec.rejected)
    pending = "\n".join(f"- {p}" for p in rec.pending) or "- none recorded"
    persistence = ""
    if pg:
        persistence = f'''
## Database and migrations

Tables are created by Alembic only (`create_all` is never called). Point `{prefix}_DATABASE_URL` at a database you own,
then:

```
uv run alembic upgrade head      # apply the migrations (the first one is generated from the contract)
uv run alembic current           # show the applied revision
uv run alembic downgrade base    # remove the schema again
```

The test suite needs `DATABASE_URL` set to an **empty, disposable** database: it refuses a database that already has
tables, applies the migrations, truncates the tables after every test, checks that the migrated schema matches the
models (`compare_metadata` is empty), exercises up/down, and downgrades at the end.
'''
    security = ""
    if _registry(spec):
        security = f'''
## Tenants and API keys

`{prefix}_API_KEYS` is a JSON object mapping each API key to the tenant it belongs to. A request to
`/api/v1/tenants/{{{_tenant(spec)}}}/...` is accepted only when the key presented in `X-API-Key` is bound to that tenant
(403 otherwise); every repository call is scoped by that tenant id. This is authentication plus tenant membership.
It is **not** resource-level authorization and it is **not** row-level security: the generated isolation is
application scoping{" plus composite foreign keys between tenant tables" if pg else ""}.
'''
    return f'''# {spec.name}

Generated by FastAPI Architect (design engine v2.0) from the `{spec.name}` contract (digest `{spec.digest[:12]}`); style **{rec.style}**.

## Why this structure

{chr(10).join("- " + r for r in rec.rationale)}

Rejected alternatives:

{rejected}

## Layout

```
{chr(10).join(layout_lines)}
```

## Run

```
uv sync                      # or: pip install -e . --group dev
uv run uvicorn {spec.package}.main:app --reload
uv run pytest -q
uv run ruff check .
```

Settings come from environment variables prefixed `{prefix}_` (see `.env.example`; no secret has a default). `.audit-fastapi.json` declares the design decisions the auditor should respect (`python3 scripts/audit_fastapi.py .` from the plugin repository).
{persistence}{security}
## Pending decisions

{pending}
'''


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def build(spec: Spec, rec: Recommendation) -> Blueprint:
    layout = Layout(spec, rec)
    pkg = spec.package
    pg = spec.persistence["kind"] == "postgresql"
    files: Dict[str, str] = {}
    notes: List[str] = []
    ports_by_module: Dict[str, List[IntegrationSpec]] = {m.name: _ports_for(spec, rec, m) for m in spec.modules}
    all_ports = [(m, i) for m in spec.modules for i in ports_by_module[m.name]]

    files["pyproject.toml"] = _pyproject(spec, rec)
    files[".gitignore"] = _gitignore()
    files[".env.example"] = _env_example(spec)
    files[".audit-fastapi.json"] = _audit_config(spec, rec)
    files[f"src/{pkg}/__init__.py"] = f'"""{spec.name}: generated by FastAPI Architect from a design contract."""\n\n__version__ = "0.1.0"\n'
    files[f"src/{pkg}/main.py"] = _main(spec, rec, layout)
    files[f"src/{pkg}/core/__init__.py"] = ""
    files[f"src/{pkg}/core/config.py"] = _config(spec)
    files[f"src/{pkg}/core/errors.py"] = _errors()
    if spec.security["authentication"] == "api_key":
        files[f"src/{pkg}/core/security.py"] = _security(spec)
    if pg:
        files[f"src/{pkg}/core/db.py"] = _db()
        files["alembic.ini"] = _alembic_ini(spec)
        files["migrations/env.py"] = _alembic_env(spec, layout)
        files["migrations/script.py.mako"] = _alembic_mako()
        files[f"migrations/versions/{initial_revision(spec)}_initial_schema.py"] = _migration(spec)
        files["tests/test_migrations.py"] = _migration_tests(spec)
    files[f"src/{pkg}/api/__init__.py"] = ""
    files[f"src/{pkg}/api/router.py"] = _api_router(spec, layout)
    if not layout.by_layer:
        files[f"src/{pkg}/modules/__init__.py"] = ""
    for m in spec.modules:
        ports = ports_by_module[m.name]
        if not layout.by_layer:
            files[f"src/{pkg}/modules/{m.name}/__init__.py"] = f'"""{m.description}"""\n'
        files[layout.path("schemas", m.name)] = _schemas(spec, m)
        files[layout.path("models", m.name)] = _models(spec, m, layout)
        files[layout.path("repository", m.name)] = _repository(spec, m, layout)
        files[layout.path("service", m.name)] = _service(spec, m, layout, ports)
        files[layout.path("dependencies", m.name)] = _dependencies(spec, m, layout, ports)
        files[layout.path("router", m.name)] = _router(spec, m, layout)
        if ports:
            files[layout.path("ports", m.name)] = _ports(m, ports)
            if not layout.by_layer:
                files[layout.path("adapters", m.name)] = ""
            for p in ports:
                files[layout.adapter_path(m.name, p.name)] = _adapter(p)
        files[f"tests/test_{m.name}.py"] = _tests(spec, m, ports)
    if layout.by_layer:
        for folder in ("api", "schemas", "services", "repositories", "models", "dependencies") + (("ports", "adapters") if all_ports else ()):
            files.setdefault(f"src/{pkg}/{folder}/__init__.py", "")
    files["tests/__init__.py"] = ""
    files["tests/conftest.py"] = _conftest(spec, all_ports, layout)
    layout_lines = _tree(sorted(files) + ["README.md"])
    files["README.md"] = _readme(spec, rec, layout_lines)
    if pg:
        notes.append(f"PostgreSQL persistence: Alembic environment and initial migration {initial_revision(spec)} generated from the contract; tables are created only by `alembic upgrade head`; the generated API tests skip unless DATABASE_URL points at an empty disposable database")
    if rec.style == STYLE_HEXAGONAL:
        notes.append("ports and adapters generated for: " + ", ".join(f"{m}:{','.join(i.name for i in ports_by_module[m])}" for m in rec.ports_modules))
    if _registry(spec):
        notes.append("multi-tenant binding api_key_registry: tenant routes resolve the tenant from the API key registry and refuse other tenants with 403; isolation is application scoping, not row-level security")
    return Blueprint(style=rec.style, package=pkg, files=files, layout=layout_lines, notes=notes)


def _tree(paths: List[str]) -> List[str]:
    lines: List[str] = []
    seen: set = set()
    for path in sorted(set(paths)):
        parts = path.split("/")
        for depth in range(len(parts)):
            key = "/".join(parts[: depth + 1])
            if key in seen:
                continue
            seen.add(key)
            lines.append("  " * depth + parts[depth] + ("/" if depth < len(parts) - 1 else ""))
    return lines

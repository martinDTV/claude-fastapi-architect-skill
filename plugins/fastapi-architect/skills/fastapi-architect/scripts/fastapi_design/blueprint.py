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
are generated (schemas, repository methods, use cases, routes and tests). Output is deterministic: sorted
iteration, no timestamps, no random values.
"""

from __future__ import annotations

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


def _header(doc: str, stdlib: Sequence[str] = (), third_party: Sequence[str] = (), local: Sequence[str] = ()) -> List[str]:
    """Module docstring plus import groups (stdlib, third party, project), each sorted, separated by blank lines."""
    lines = [f'"""{doc}"""', ""]
    for group in (stdlib, third_party, local):
        if group:
            lines.extend(sorted(set(group)))
            lines.append("")
    return lines


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


# --------------------------------------------------------------------------- #
# project files
# --------------------------------------------------------------------------- #


def _pyproject(spec: Spec, rec: Recommendation) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    deps = ['"fastapi>=0.115,<1"', '"uvicorn[standard]>=0.30"', '"pydantic>=2.7,<3"', '"pydantic-settings>=2.4,<3"']
    if pg:
        deps += ['"sqlalchemy[asyncio]>=2.0.36,<2.1"', '"asyncpg>=0.30,<0.31"']
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
        'pythonpath = ["src"]',
        "",
        "[tool.ruff]",
        "line-length = 110",
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
    # route patterns are matched against the paths the auditor sees on each handler (router-local, without the
    # prefixes added by include_router); without authentication every route is public by design
    config["public_routes"] = ["/*"] if spec.security["authentication"] == "none" else ["/health"]
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
    if spec.security["authentication"] == "api_key":
        lines.append(f"{prefix}_API_KEY=change-me")
    if spec.persistence["kind"] == "postgresql":
        lines.append(f"{prefix}_DATABASE_URL=postgresql+asyncpg://{spec.package}:{spec.package}@localhost:5432/{spec.package}")
    for i in spec.integrations:
        if i.kind == "http":
            lines.append(f"{prefix}_{i.name.upper()}_BASE_URL=http://localhost:8080")
    return "\n".join(lines) + "\n"


def _config(spec: Spec) -> str:
    prefix = spec.package.upper()
    api_key = spec.security["authentication"] == "api_key"
    fields = [f'    app_name: str = "{spec.name}"', '    environment: str = "local"']
    if api_key:
        fields.append(f'    api_key: SecretStr = SecretStr("")  # no default secret: set {prefix}_API_KEY (the dependency refuses an empty key)')
    if spec.persistence["kind"] == "postgresql":
        fields.append(f'    database_url: str = "postgresql+asyncpg://{spec.package}:{spec.package}@localhost:5432/{spec.package}"')
    for i in spec.integrations:
        if i.kind == "http":
            fields.append(f'    {i.name}_base_url: str = "http://localhost:8080"')
            fields.append(f"    {i.name}_timeout_s: float = 5.0")
    lines = _header(
        f"Settings: environment variables prefixed {prefix}_ (and an optional .env file); no secret has a default.",
        stdlib=["from functools import lru_cache", "from typing import Annotated"],
        third_party=["from fastapi import Depends, Request"] + (["from pydantic import SecretStr"] if api_key else []) + ["from pydantic_settings import BaseSettings, SettingsConfigDict"],
    )
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
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail, "instance": instance},
        media_type="application/problem+json",
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        return problem(exc.status, exc.title, exc.detail, str(request.url.path))
'''


def _security(spec: Spec) -> str:
    return f'''"""API-key authentication: a constant-time comparison against the configured key (header X-API-Key)."""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from {spec.package}.core.config import SettingsDep


def require_api_key(settings: SettingsDep, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> None:
    expected = settings.api_key.get_secret_value()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API key is not configured")
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
'''


def _db() -> str:
    return '''"""Async engine and request-scoped sessions (SQLAlchemy 2, asyncpg)."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


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
            body.append(f'app.state.ports.setdefault("{i.name}", {cls}(settings.{i.name}_base_url, settings.{i.name}_timeout_s))')
        else:
            body.append(f'app.state.ports.setdefault("{i.name}", {cls}())')
    lines = _header(
        "Application factory: settings, lifespan-owned resources, error handlers, routers.",
        stdlib=["from collections.abc import AsyncIterator", "from contextlib import asynccontextmanager"],
        third_party=["from fastapi import FastAPI"] + (["from sqlalchemy.ext.asyncio import async_sessionmaker"] if pg else []),
        local=local,
    )
    lines += [
        "",
        f"def create_app(settings: Settings | None = None{', ports: dict[str, object] | None = None' if ports else ''}) -> FastAPI:",
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
                if f.required:
                    create_lines.append(f"    {f.name}: {_annotation(f)} = Field({constraints})" if constraints else f"    {f.name}: {_annotation(f)}")
                else:
                    create_lines.append(f"    {f.name}: {_annotation(f)} = Field(default=None{', ' + constraints if constraints else ''})")
            lines += ["", f"class {e.name}Create(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *create_lines, ""]
        if ops["update"]:
            update_lines = [f"    {f.name}: {_annotation(f, optional=True)} = Field(default=None{', ' + _constraints(f) if _constraints(f) else ''})" for f in e.fields]
            lines += ["", f"class {e.name}Update(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *update_lines, ""]
        read_lines = ["    id: UUID"] + ([f"    {tenant}: UUID"] if tenant else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields] + ["    created_at: datetime"]
        lines += ["", f"class {e.name}Read(BaseModel):", "    model_config = ConfigDict(from_attributes=True)", *read_lines, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _models(spec: Spec, module: ModuleSpec) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    all_fields = [f for e in module.entities for f in e.fields]
    if not pg:
        stdlib = ["from dataclasses import dataclass, field"]
        for line in _type_imports(all_fields, uuid=("UUID", "uuid4")):
            if line.startswith("from datetime import"):
                names = sorted(set(line[len("from datetime import ") :].split(", ")) | {"timezone"})
                line = "from datetime import " + ", ".join(names)
            stdlib.append(line)
        lines = _header("Entities of the module (plain dataclasses; the in-memory repository stores them as they are).", stdlib=stdlib)
        for e in module.entities:
            body = ([f"    {tenant}: UUID"] if tenant else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields if f.required]
            body += [f"    {f.name}: {_annotation(f)} = None" for f in e.fields if not f.required]
            body += ["    id: UUID = field(default_factory=uuid4)", "    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))"]
            lines += ["", "@dataclass", f"class {e.name}:", *body, ""]
        return "\n".join(lines).rstrip("\n") + "\n"
    sa = sorted({SA_TYPES[f.type] for f in all_fields} | {"DateTime", "Uuid"})
    lines = _header(
        "SQLAlchemy 2 mapped classes of the module.",
        stdlib=_type_imports(all_fields, uuid=("UUID", "uuid4")),
        third_party=[f"from sqlalchemy import {', '.join(sa)}, func", "from sqlalchemy.orm import Mapped, mapped_column"],
        local=[f"from {spec.package}.core.db import Base"],
    )
    for e in module.entities:
        body = [f'    __tablename__ = "{e.plural}"', "    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)"]
        if tenant:
            body.append(f"    {tenant}: Mapped[UUID] = mapped_column(Uuid, index=True)")
        for f in e.fields:
            col = SA_TYPES[f.type]
            arg = f"String({f.max_length or 255})" if f.type == "str" else ("Numeric(18, 2)" if f.type == "decimal" else ("DateTime(timezone=True)" if f.type == "datetime" else col))
            body.append(f"    {f.name}: Mapped[{_annotation(f)}] = mapped_column({arg}{'' if f.required else ', nullable=True'})")
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
                source = f"(item for item in self._items.values() if item.{tenant} == {tenant})" if tenant else "self._items.values()"
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int) -> list[{E}]:", f"        items = sorted({source}, key=lambda item: (item.created_at, str(item.id)))", "        return items[offset : offset + limit]"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", f"        del self._items[{x}_id]", "        return True"]
        else:
            where = f".where({E}.{tenant} == {tenant})" if tenant else ""
            impl += [f"class Sql{E}Repository:", '    """Repository on the request-scoped AsyncSession; the service owns the commit."""', "", "    def __init__(self, session: AsyncSession) -> None:", "        self._session = session"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._session.add({x})", "        await self._session.flush()", f"        return {x}"]
            if ops["get"]:
                impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E} | None:", f"        return await self._session.scalar(select({E}).where({E}.id == {x}_id){where})"]
            if ops["list"]:
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int) -> list[{E}]:", f"        rows = await self._session.scalars(select({E}){where}.order_by({E}.created_at, {E}.id).limit(limit).offset(offset))", "        return list(rows)"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", "        await self._session.flush()", f"        return {x}"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", "        await self._session.delete(item)", "        await self._session.flush()", "        return True"]
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
        f"from {spec.package}.core.errors import NotFoundError",
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
        third_party=["from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=local,
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        ctor = [f"repository: {E}Repository"] + (["session: AsyncSession"] if pg else []) + [f"{p.name}: {_camel(p.name)}Port" for p in ports]
        assigns = ["        self._repository = repository"] + (["        self._session = session"] if pg else []) + [f"        self._{p.name} = {p.name}" for p in ports]
        commit = ["        await self._session.commit()"] if pg else []
        body = [f"class {E}Service:", f"    def __init__(self, {', '.join(ctor)}) -> None:", *assigns]
        if ops["create"]:
            kwargs = ", ".join(f"{f.name}=data.{f.name}" for f in e.fields)
            notify = [f'        await self._{p.name}.send({{"event": "{x}.created", "id": str({x}.id)}})' for p in ports]
            body += ["", f"    async def create(self, {t_sig}data: {E}Create) -> {E}:", f"        {x} = {E}({(tenant + '=' + tenant + ', ') if tenant else ''}{kwargs})", f"        await self._repository.add({x})", *commit, *notify, f"        return {x}"]
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
    lines = _header(
        "Composition of the module: repositories, ports and services as FastAPI dependencies.",
        stdlib=["from typing import Annotated"],
        third_party=["from fastapi import Depends, Request"],
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
        lines += ["", *repo, "", "", f"def get_{x}_service({', '.join(args)}) -> {E}Service:", f"    return {call}", "", "", f"{E}ServiceDep = Annotated[{E}Service, Depends(get_{x}_service)]", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _router(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    tenant = _tenant(spec)
    t_sig = f"{tenant}: UUID, " if tenant else ""
    t_arg = f"{tenant}, " if tenant else ""
    schema_names: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + [f"{e.name}Read"] + ([f"{e.name}Update"] if ops["update"] else [])
    lines = _header(
        f"HTTP boundary of the {module.name} module: validation by schema, status codes, problem details for domain errors.",
        stdlib=["from typing import Annotated", "from uuid import UUID"],
        third_party=["from fastapi import APIRouter, Query, status"],
        local=[f"from {layout.imp('dependencies', module.name)} import {', '.join(e.name + 'ServiceDep' for e in module.entities)}", f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}"],
    )
    lines.append(f'router = APIRouter(tags=["{module.name}"])')
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        prefix = (f"/tenants/{{{tenant}}}" if tenant else "") + f"/{e.plural}"
        if ops["create"]:
            lines += ["", "", f'@router.post("{prefix}", status_code=status.HTTP_201_CREATED)', f"async def create_{x}({t_sig}payload: {E}Create, service: {E}ServiceDep) -> {E}Read:", f"    return {E}Read.model_validate(await service.create({t_arg}payload))"]
        if ops["list"]:
            lines += ["", "", f'@router.get("{prefix}")', f"async def list_{e.plural}(", *([f"    {tenant}: UUID,"] if tenant else []), f"    service: {E}ServiceDep,", "    limit: Annotated[int, Query(ge=1, le=200)] = 50,", "    offset: Annotated[int, Query(ge=0)] = 0,", f") -> list[{E}Read]:", f"    return [{E}Read.model_validate(item) for item in await service.list({t_arg}limit, offset)]"]
        if ops["get_route"]:
            lines += ["", "", f'@router.get("{prefix}/{{{x}_id}}")', f"async def get_{x}({t_sig}{x}_id: UUID, service: {E}ServiceDep) -> {E}Read:", f"    return {E}Read.model_validate(await service.get({t_arg}{x}_id))"]
        if ops["update"]:
            lines += ["", "", f'@router.patch("{prefix}/{{{x}_id}}")', f"async def update_{x}({t_sig}{x}_id: UUID, payload: {E}Update, service: {E}ServiceDep) -> {E}Read:", f"    return {E}Read.model_validate(await service.update({t_arg}{x}_id, payload))"]
        if ops["delete"]:
            lines += ["", "", f'@router.delete("{prefix}/{{{x}_id}}", status_code=status.HTTP_204_NO_CONTENT)', f"async def delete_{x}({t_sig}{x}_id: UUID, service: {E}ServiceDep) -> None:", f"    await service.delete({t_arg}{x}_id)"]
    return "\n".join(lines) + "\n"


def _ports(module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    lines = _header(f"Ports of the {module.name} module: what the use cases need from the outside, as Protocols.", stdlib=["from typing import Protocol"])
    for p in ports:
        lines += ["", f"class {_camel(p.name)}Port(Protocol):", f'    """{p.kind} integration ({p.direction}, criticality {p.criticality})."""', "", "    async def send(self, payload: dict[str, object]) -> None: ...", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _adapter(p: IntegrationSpec) -> str:
    cls = _camel(p.name)
    if p.kind == "http":
        return f'''"""HTTP adapter of the {p.name} port: one client per call with an explicit timeout, errors raised to the use case."""

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


def _conftest(spec: Spec, ports: List[Tuple[ModuleSpec, IntegrationSpec]], layout: Layout) -> str:
    api_key = spec.security["authentication"] == "api_key"
    pg = spec.persistence["kind"] == "postgresql"
    settings_kwargs = ['app_name="test"'] + (['api_key=SecretStr("test-key")'] if api_key else []) + (['database_url=os.environ["DATABASE_URL"]'] if pg else [])
    local = [f"from {spec.package}.core.config import Settings", f"from {spec.package}.main import create_app"]
    local += sorted({f"from {layout.adapter_imp(m.name, i.name)} import Recording{_camel(i.name)}Adapter" for m, i in ports})
    ports_dict = "{" + ", ".join(f'"{i.name}": Recording{_camel(i.name)}Adapter()' for _m, i in ports) + "}" if ports else ""
    lines = _header(
        "Test fixtures: one application per test with its own state" + (" and recording adapters." if ports else "."),
        stdlib=["import os"] if pg else [],
        third_party=["import pytest", "from fastapi.testclient import TestClient"] + (["from pydantic import SecretStr"] if api_key else []),
        local=local,
    )
    lines += ["", "@pytest.fixture", "def app():"]
    if pg:
        lines += ['    if "DATABASE_URL" not in os.environ:', '        pytest.skip("set DATABASE_URL to run the API tests against PostgreSQL")']
    headers = '{"X-API-Key": "test-key"}' if api_key else "{}"
    lines += [
        f"    return create_app(Settings({', '.join(settings_kwargs)}){', ports=' + ports_dict if ports else ''})",
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
        f"    return {headers}",
    ]
    return "\n".join(lines) + "\n"


def _tests(spec: Spec, module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    api_key = spec.security["authentication"] == "api_key"
    tenant = _tenant(spec)
    lines = _header(f"API tests of the {module.name} module through the HTTP boundary.", stdlib=["import uuid"])
    item = 'f"{url}/{created[\'id\']}"'
    for e in module.entities:
        ops = _ops(e)
        x, up = e.snake, e.snake.upper()
        payload = "{" + ", ".join(f'"{f.name}": {_sample(f)}' for f in e.fields) + "}"
        required = [f for f in e.fields if f.required]
        invalid = "{}" if required else "{" + f'"{e.fields[0].name}": {WRONG[e.fields[0].type]}' + "}"
        lines += ["", f"PAYLOAD_{up} = {payload}"]
        if tenant:
            lines += ["", "", f"def {x}_url(tenant: uuid.UUID) -> str:", f'    return f"/api/v1/tenants/{{tenant}}/{e.plural}"']
            setup = ["    tenant = uuid.uuid4()", f"    url = {x}_url(tenant)"]
        else:
            lines += [f'{up}_URL = "/api/v1/{e.plural}"']
            setup = [f"    url = {up}_URL"]

        def test(name: str, body: List[str], fixtures: str = "client, headers") -> None:
            lines.extend(["", "", f"def {name}({fixtures}):", *setup, *body])

        if ops["create"]:
            echo = [f'    assert body["{f.name}"] == PAYLOAD_{up}["{f.name}"]' for f in required if f.type in ECHOED_TYPES][:2]
            test(f"test_create_{x}", [f"    response = client.post(url, json=PAYLOAD_{up}, headers=headers)", "    assert response.status_code == 201, response.text", "    body = response.json()", '    assert uuid.UUID(body["id"])', *echo])
            test(f"test_create_{x}_rejects_invalid_payload", [f"    response = client.post(url, json={invalid}, headers=headers)", "    assert response.status_code == 422"])
            test(f"test_create_{x}_rejects_unknown_properties", [f'    response = client.post(url, json={{**PAYLOAD_{up}, "unexpected": 1}}, headers=headers)', "    assert response.status_code == 422"])
        if ops["get_route"] and ops["create"]:
            test(f"test_get_{x}_returns_the_created_one_and_404_for_unknown", [f"    created = client.post(url, json=PAYLOAD_{up}, headers=headers).json()", f"    found = client.get({item}, headers=headers)", "    assert found.status_code == 200", '    assert found.json()["id"] == created["id"]', '    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)', "    assert missing.status_code == 404", '    assert missing.headers["content-type"].startswith("application/problem+json")', '    assert missing.json()["status"] == 404'])
        if ops["list"] and ops["create"]:
            test(f"test_list_{e.plural}_paginates", ["    for _ in range(3):", f"        assert client.post(url, json=PAYLOAD_{up}, headers=headers).status_code == 201", '    page = client.get(url, params={"limit": 2, "offset": 0}, headers=headers)', "    assert page.status_code == 200", "    assert len(page.json()) == 2", '    assert client.get(url, params={"limit": 0}, headers=headers).status_code == 422'])
        if ops["update"] and ops["create"]:
            f0 = e.fields[0]
            changed = _changed_sample(f0)
            check = [f'    assert updated.json()["{f0.name}"] == {changed}'] if f0.type in ECHOED_TYPES else []
            test(f"test_update_{x}", [f"    created = client.post(url, json=PAYLOAD_{up}, headers=headers).json()", f'    updated = client.patch({item}, json={{"{f0.name}": {changed}}}, headers=headers)', "    assert updated.status_code == 200, updated.text", *check, f'    assert client.patch({item}, json={{"unexpected": 1}}, headers=headers).status_code == 422'])
        if ops["delete"] and ops["create"]:
            after = f"    assert client.get({item}, headers=headers).status_code == 404" if ops["get_route"] else f"    assert client.delete({item}, headers=headers).status_code == 404"
            test(f"test_delete_{x}", [f"    created = client.post(url, json=PAYLOAD_{up}, headers=headers).json()", f"    assert client.delete({item}, headers=headers).status_code == 204", after])
        if api_key and ops["create"]:
            test(f"test_{x}_routes_require_the_api_key", [f"    assert client.post(url, json=PAYLOAD_{up}).status_code == 401", f'    assert client.post(url, json=PAYLOAD_{up}, headers={{"X-API-Key": "wrong"}}).status_code == 401'], fixtures="client")
        if ports and ops["create"]:
            p = ports[0]
            test(f"test_create_{x}_notifies_the_{p.name}_port", [f"    assert client.post(url, json=PAYLOAD_{up}, headers=headers).status_code == 201", f'    sent = app.state.ports["{p.name}"].sent', f'    assert sent and sent[-1]["event"] == "{x}.created"'], fixtures="app, client, headers")
    lines += ["", "", "def test_health_is_public(client):", '    assert client.get("/health").json() == {"status": "ok"}']
    return "\n".join(lines) + "\n"


def _readme(spec: Spec, rec: Recommendation, layout_lines: List[str]) -> str:
    rejected = "\n".join(f"- `{r['style']}`: {r['reason']}" for r in rec.rejected)
    pending = "\n".join(f"- {p}" for p in rec.pending) or "- none recorded"
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
```

Settings come from environment variables prefixed `{spec.package.upper()}_` (see `.env.example`). `.audit-fastapi.json` declares the design decisions the auditor should respect (`python3 scripts/audit_fastapi.py .` from the plugin repository).

## Pending decisions

{pending}
'''


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def build(spec: Spec, rec: Recommendation) -> Blueprint:
    layout = Layout(spec, rec)
    pkg = spec.package
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
    if spec.persistence["kind"] == "postgresql":
        files[f"src/{pkg}/core/db.py"] = _db()
    files[f"src/{pkg}/api/__init__.py"] = ""
    files[f"src/{pkg}/api/router.py"] = _api_router(spec, layout)
    if not layout.by_layer:
        files[f"src/{pkg}/modules/__init__.py"] = ""
    for m in spec.modules:
        ports = ports_by_module[m.name]
        if not layout.by_layer:
            files[f"src/{pkg}/modules/{m.name}/__init__.py"] = f'"""{m.description}"""\n'
        files[layout.path("schemas", m.name)] = _schemas(spec, m)
        files[layout.path("models", m.name)] = _models(spec, m)
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
    if spec.persistence["kind"] == "postgresql":
        notes.append("PostgreSQL persistence: tables are created by migrations you still have to add (Alembic); the generated API tests skip unless DATABASE_URL is set")
    if rec.style == STYLE_HEXAGONAL:
        notes.append("ports and adapters generated for: " + ", ".join(f"{m}:{','.join(i.name for i in ports_by_module[m])}" for m in rec.ports_modules))
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

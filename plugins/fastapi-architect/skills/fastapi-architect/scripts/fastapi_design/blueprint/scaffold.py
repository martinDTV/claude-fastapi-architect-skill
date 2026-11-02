"""Project scaffold: pyproject, audit configuration, environment example, settings, errors, application factory, router composition, README."""

from __future__ import annotations

import json
from typing import Dict, List

from ..advisor import STYLE_HEXAGONAL, STYLE_LAYERED, Recommendation
from ..spec import Spec
from ._base import LINE, TENANT_A, Layout
from ._helpers import _authenticator, _authz, _call, _camel, _claim_binding, _header, _oidc, _registry, _rls, _roles_all, _tenant
from .modules import _ports_for


def _pyproject(spec: Spec, rec: Recommendation) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    deps = ['"fastapi>=0.115,<1"', '"uvicorn[standard]>=0.30"', '"pydantic>=2.7,<3"', '"pydantic-settings>=2.4,<3"']
    if pg:
        deps += ['"sqlalchemy[asyncio]>=2.0.36,<2.1"', '"asyncpg>=0.30,<0.31"', '"alembic>=1.13,<2"']
    if _oidc(spec):
        deps.append('"pyjwt[crypto]>=2.10.1,<3"')  # 2.10.1: ECAlgorithm.to_jwk keeps leading zero bytes; the generated fixture no longer depends on it, the pin keeps every other JWK helper sound
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
    if _authz(spec):
        contracts["mutation_authorization_required"] = ["/api/*"]
    if contracts:
        config["contracts"] = contracts
    config["x-generated-by"] = "fastapi-architect design (v2.0): declarations of the design, reviewed by the auditor"
    return json.dumps(config, indent=2, sort_keys=True) + "\n"


def _gitignore() -> str:
    return ".venv/\n__pycache__/\n*.py[cod]\n.env\n.pytest_cache/\n.ruff_cache/\ndist/\n.fastapi-architect/ops/\n"


def _env_example(spec: Spec) -> str:
    prefix = spec.package.upper()
    lines = [f"{prefix}_APP_NAME={spec.name}", f"{prefix}_ENVIRONMENT=local"]
    if _registry(spec):
        grant = '{"tenant_id": "' + TENANT_A + '", "subject": "ops", "roles": ' + json.dumps(_roles_all(spec)) + "}"
        lines.append(f'{prefix}_API_KEYS={{"replace-with-a-generated-key": {grant}}}')
    elif spec.security["authentication"] == "api_key":
        lines.append(f"{prefix}_API_KEY=replace-with-a-generated-key")
    if _oidc(spec):
        o = spec.oidc or {}
        lines += [f"{prefix}_OIDC_ISSUER={o['issuer']}", f"{prefix}_OIDC_AUDIENCE={o['audience']}", f"{prefix}_OIDC_JWKS_URL={o['jwks_url']}", f"# {prefix}_OIDC_CA_BUNDLE=/etc/ssl/private-ca.pem  (only when the provider certificate is signed by a private CA)", f"# {prefix}_OIDC_JWKS_JSON is for tests only (refused unless {prefix}_ENVIRONMENT=test)"]
    if spec.persistence["kind"] == "postgresql":
        if _rls(spec):
            role = spec.security["multi_tenant"]["rls"]["app_role"]
            lines += [
                "# the application connects as the restricted role (no BYPASSRLS, no DDL); migrations run as the owner",
                f"{prefix}_DATABASE_URL=postgresql+asyncpg://{role}:PASSWORD@localhost:5432/{spec.package}",
                f"{prefix}_MIGRATIONS_DATABASE_URL=postgresql+asyncpg://OWNER:PASSWORD@localhost:5432/{spec.package}",
                f"{prefix}_APP_ROLE={role}",
            ]
        else:
            lines.append(f"{prefix}_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/{spec.package}")
    for i in spec.integrations:
        if i.kind == "http":
            lines.append(f"{prefix}_{i.name.upper()}_BASE_URL=http://localhost:8080")
    return "\n".join(lines) + "\n"


def _config(spec: Spec) -> str:
    prefix = spec.package.upper()
    api_key = spec.security["authentication"] == "api_key"
    registry = _registry(spec)
    oidc = _oidc(spec)
    fields = [f'    app_name: str = "{spec.name}"', '    environment: str = "local"']
    if registry:
        fields += [f"    # JSON object in {prefix}_API_KEYS: key -> grant (tenant_id, subject, roles); a bare tenant id is accepted", "    api_keys: dict[str, ApiKeyGrant] = {}"]
    elif api_key:
        fields += [f"    # no default secret: set {prefix}_API_KEY (the dependency refuses an empty key)", '    api_key: SecretStr = SecretStr("")']
    if oidc:
        o = spec.oidc or {}
        fields += [
            "    # OIDC provider of the contract; the accepted algorithms are fixed in core/security.py, not configurable",
            f'    oidc_issuer: str = "{o["issuer"]}"',
            f'    oidc_audience: str = "{o["audience"]}"',
            f'    oidc_jwks_url: str = "{o["jwks_url"]}"',
            '    oidc_jwks_json: str = ""  # static JWKS document for tests only (refused unless environment == "test")',
            f"    oidc_jwks_cache_s: int = {o['jwks_cache_s']}",
            "    oidc_jwks_stale_max_s: int = 3600  # keep serving the last keys this long when a refresh fails; never without any",
            "    oidc_fetch_timeout_s: float = 5.0",
            '    oidc_ca_bundle: str = ""  # PEM bundle of a private CA that signs the provider certificate; empty: system store',
            f"    oidc_leeway_s: int = {o['leeway_s']}",
        ]
    if spec.persistence["kind"] == "postgresql":
        if _rls(spec):
            fields += [f"    # {prefix}_DATABASE_URL is the application role's URL (row privileges only, no BYPASSRLS);", f"    # migrations run with {prefix}_MIGRATIONS_DATABASE_URL (the owner)", f'    database_url: str = "postgresql+asyncpg://localhost:5432/{spec.package}"']
        else:
            fields += [f"    # credentials come from {prefix}_DATABASE_URL, never from defaults", f'    database_url: str = "postgresql+asyncpg://localhost:5432/{spec.package}"']
    for i in spec.integrations:
        if i.kind == "http":
            fields.append(f'    {i.name}_base_url: str = "http://localhost:8080"')
            fields.append(f"    {i.name}_timeout_s: float = 5.0")
    stdlib = ["from functools import lru_cache", "from typing import Annotated"] + (["from uuid import UUID"] if registry else [])
    third = ["from fastapi import Depends, Request"]
    if api_key and not registry:
        third.append("from pydantic import SecretStr")
    if registry:
        third.append("from pydantic import BaseModel, ConfigDict, field_validator")
    third.append("from pydantic_settings import BaseSettings, SettingsConfigDict")
    lines = _header(f"Settings: environment variables prefixed {prefix}_ (and an optional .env file); no secret has a default.", stdlib=stdlib, third_party=third)
    if registry:
        lines += [
            "",
            "class ApiKeyGrant(BaseModel):",
            '    """One API key: the tenant it belongs to, the subject it acts as (default: a digest of the key), its roles."""',
            "",
            '    model_config = ConfigDict(extra="forbid")',
            "",
            "    tenant_id: UUID",
            "    subject: str | None = None",
            "    roles: list[str] = []",
            "",
        ]
    lines += [
        "",
        "class Settings(BaseSettings):",
        f'    model_config = SettingsConfigDict(env_prefix="{prefix}_", env_file=".env", extra="ignore")',
        "",
        *fields,
    ]
    if registry:
        lines += [
            "",
            '    @field_validator("api_keys", mode="before")',
            "    @classmethod",
            "    def _grants(cls, value: object) -> object:",
            '        """A bare tenant id per key (the registry format before grants) is read as a grant without subject or roles."""',
            "        if isinstance(value, dict):",
            '            return {key: ({"tenant_id": grant} if isinstance(grant, str) else grant) for key, grant in value.items()}',
            "        return value",
        ]
    lines += [
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


def _errors(spec: Spec) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    lines = _header(
        "Domain errors and their RFC 9457 problem-details responses: 403 denied, 404 unknown or invisible, 409 conflict, 503 infrastructure"
        + (" (a database that cannot be reached is an I/O failure, never a 401 or a 404)." if pg else "."),
        third_party=["from fastapi import FastAPI, Request", "from fastapi.responses import JSONResponse"] + (["from sqlalchemy.exc import InterfaceError, OperationalError"] if pg else []),
    )
    lines += [
        "",
        "class DomainError(Exception):",
        "    status = 400",
        '    title = "Domain error"',
        "",
        "    def __init__(self, detail: str) -> None:",
        "        super().__init__(detail)",
        "        self.detail = detail",
        "",
        "",
        "class NotFoundError(DomainError):",
        "    status = 404",
        '    title = "Not found"',
        "",
        "",
        "class ForbiddenError(DomainError):",
        '    """The caller is authenticated but the policy denies this operation or this resource."""',
        "",
        "    status = 403",
        '    title = "Forbidden"',
        "",
        "",
        "class ConflictError(DomainError):",
        "    status = 409",
        '    title = "Conflict"',
        "",
        "",
        "class UnavailableError(DomainError):",
        '    """An infrastructure dependency failed: distinct from a denial (403) and from a missing resource (404)."""',
        "",
        "    status = 503",
        '    title = "Service unavailable"',
        "",
        "",
        "def problem(status: int, title: str, detail: str, instance: str) -> JSONResponse:",
        '    content = {"type": "about:blank", "title": title, "status": status, "detail": detail, "instance": instance}',
        '    return JSONResponse(status_code=status, content=content, media_type="application/problem+json")',
        "",
        "",
        "def install_error_handlers(app: FastAPI) -> None:",
        "    @app.exception_handler(DomainError)",
        "    async def _domain(request: Request, exc: DomainError) -> JSONResponse:",
        "        return problem(exc.status, exc.title, exc.detail, str(request.url.path))",
    ]
    if pg:
        lines += [
            "",
            "    @app.exception_handler(OperationalError)",
            "    @app.exception_handler(InterfaceError)",
            "    @app.exception_handler(OSError)",
            "    async def _unavailable(request: Request, exc: Exception) -> JSONResponse:",
            '        return problem(503, "Service unavailable", "the database is unavailable", str(request.url.path))',
        ]
    return "\n".join(lines) + "\n"


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
    if _oidc(spec):
        local.append(f"from {spec.package}.core.security import JwksSource")
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
        *(["    app.state.jwks = JwksSource(settings)  # fails at startup when the JWKS source is misconfigured"] if _oidc(spec) else []),
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
    authenticated = spec.security["authentication"] != "none"
    authenticator = _authenticator(spec)
    local = [f"from {layout.imp('router', m.name)} import router as {m.name}_router" for m in spec.modules]
    if authenticated:
        local.append(f"from {spec.package}.core.security import {authenticator}")
    what = "the API key" if spec.security["authentication"] == "api_key" else "the bearer token"
    lines = _header(
        "Versioned API router: every business router is mounted under /api/v1" + (f" behind {what}." if authenticated else "."),
        third_party=["from fastapi import APIRouter, Depends" if authenticated else "from fastapi import APIRouter"],
        local=local,
    )
    lines.append(f'api_router = APIRouter(prefix="/api/v1"{f", dependencies=[Depends({authenticator})]" if authenticated else ""})')
    lines += [f"api_router.include_router({m.name}_router)" for m in spec.modules]
    return "\n".join(lines) + "\n"


def _readme(spec: Spec, rec: Recommendation, layout_lines: List[str]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    prefix = spec.package.upper()
    rejected = "\n".join(f"- `{r['style']}`: {r['reason']}" for r in rec.rejected)
    pending = "\n".join(f"- {p}" for p in rec.pending) or "- none recorded"
    persistence = ""
    if pg and _rls(spec):
        role = spec.security["multi_tenant"]["rls"]["app_role"]
        persistence = f'''
## Database, roles and migrations

Two database roles, two URLs. The **owner** runs the migrations (`{prefix}_MIGRATIONS_DATABASE_URL`); the **application
role** `{role}` (`{prefix}_DATABASE_URL`, name in `{prefix}_APP_ROLE`) only reads and writes rows. Create the application role
before the first migration (roles are provisioning, not migrations; the migration verifies it exists and cannot bypass
row-level security):

```
CREATE ROLE {role} LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
```

Then, as the owner:

```
uv run alembic upgrade head      # tables, indexes, GRANTs, ENABLE + FORCE ROW LEVEL SECURITY and one policy per table
uv run alembic current
uv run alembic downgrade base
```

The test suite needs `DATABASE_URL` (the application role) and `{prefix}_MIGRATIONS_DATABASE_URL` (the owner) pointing at the same
**empty, disposable** database: it migrates and truncates as the owner, runs the API as the application role, and
`tests/test_rls.py` proves the isolation with raw SQL through that role (no context: no rows; context bound to the
transaction; another tenant's rows unreachable; the role cannot lift the protection; concurrent tenants never mix).
'''
    elif pg:
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

`{prefix}_API_KEYS` is a JSON object mapping each API key to its grant: `{{"tenant_id": ..., "subject": ..., "roles": [...]}}`
(a bare tenant id is still accepted). A request to `/api/v1/tenants/{{{_tenant(spec)}}}/...` is accepted only when the key
presented in `X-API-Key` is bound to that tenant (403 otherwise); every repository call is scoped by that tenant id.
{"Row-level security in PostgreSQL enforces the same boundary inside the database (see above)." if _rls(spec) else "This is authentication plus tenant membership; the storage isolation is application scoping" + (" plus composite foreign keys between tenant tables" if pg else "") + "."}
'''
    if _oidc(spec):
        o = spec.oidc or {}
        security = f'''
## Identity (OIDC)

Requests carry `Authorization: Bearer <token>` issued by `{o["issuer"]}` for audience `{o["audience"]}`. The token is
verified in `core/security.py`: algorithm in {", ".join(o["algorithms"])} (nothing symmetric, nothing unsigned), signature
against the provider's JWKS (`{prefix}_OIDC_JWKS_URL`, https only, cached {o["jwks_cache_s"]} s, refreshed once per unknown key
id), issuer, audience, `exp`/`nbf`/`iat` with {o["leeway_s"]} s leeway. The principal is built from verified claims only:
`{o["claims"]["subject"]}`, `{o["claims"]["roles"]}`{f", `{o['claims']['tenant']}` (the tenant; the path parameter must match it, 403 otherwise)" if _claim_binding(spec) else ""}.
A provider that cannot be reached is 503, never 401. Tests sign their own tokens and pass the public key as a static
JWKS (`oidc_jwks_json`), which the application accepts only when `{prefix}_ENVIRONMENT=test`.
'''
    if _authz(spec):
        authz = spec.security["authorization"]
        ops = "\n".join(f"- `{op}`: {', '.join(roles)}" for op, roles in authz["operations"].items()) or "- every declared operation is open to any authenticated member"
        ownership = ""
        if spec.with_ownership:
            owned = ", ".join(f"`{m.name}.{e.name}`" for m, e in spec.owned_entities())
            vis = authz["ownership"]["visibility"]
            admins = ", ".join(authz["ownership"]["admin_roles"]) or "none"
            ownership = f'''
Owned entities ({owned}) record the creating principal in `owner_id` (never taken from the request). Visibility is
**{vis}**: {"every member of the tenant reads them; only the owner (or an admin) updates or deletes them, 403 otherwise" if vis == "tenant" else "only the owner (or an admin) reads, updates or deletes them; to anyone else they do not exist (404), because the repository query is owner-scoped"}.
Admin roles: {admins}. The checks live in the service and the repository (`core/authz.py`), not in the router.
'''
        security += f'''
## Authorization (model `{spec.authz_model}`)

Roles come with the identity ({"the key grant" if _registry(spec) else "the token"}). Each route carries the principal admitted
for its operation (`CanCreate`, `CanGet`, ...); an operation with a role list refuses, with 403, a principal without one of
them:

{ops}
{ownership}'''
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

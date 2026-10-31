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

Multi-tenant contracts are generated only with a principal-tenant binding (``api_key_registry``: every API key is a
grant bound to one tenant with a subject and roles; ``principal_claim``: the tenant is a verified token claim): a
dependency resolves the tenant path parameter and refuses any other tenant with 403, and every repository call is
scoped. Increment 5 adds OIDC bearer-token validation (``authentication: oauth2_jwt`` + ``security.oidc``), the
authorization policy of schema 1.2 (per-operation roles in a route dependency, ownership in the service and the
repository query, ``core/authz.py``), and PostgreSQL row-level security for ``strategy: shared_schema_rls``
(policies with ENABLE + FORCE in the migration, an application role without BYPASSRLS, the tenant context set with
``set_config(..., true)`` inside the request transaction, and a generated ``tests/test_rls.py`` that proves it through
the application role). Output is deterministic: sorted iteration, no timestamps, no random values.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

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
class Lineage:
    """Migration history of a project that is being evolved: the chain already on disk is kept verbatim and a new,
    additive revision is appended for the tables that do not exist yet."""

    head: str
    files: List[str]  # relative paths of the existing migration files (never regenerated)
    tables: List[str]  # tables created by the existing chain
    revisions: List[Dict[str, object]] = field(default_factory=list)  # manifest entries of the existing chain
    baseline: Optional[Spec] = None  # the contract the chain was generated from (column deltas are computed against it)


@dataclass
class Blueprint:
    style: str
    package: str
    files: Dict[str, str]
    layout: List[str] = field(default_factory=list)  # tree lines for the report
    notes: List[str] = field(default_factory=list)
    migrations: List[Dict[str, object]] = field(default_factory=list)  # [{revision, down_revision, file, tables}] in chain order

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


def _name_key(name: str) -> Tuple[int, str]:
    """isort's order-by-type: CONSTANTS, then Classes, then functions and variables."""
    rank = 0 if name.isupper() and len(name) > 1 else (1 if name[:1].isupper() else 2)
    return rank, name


def _sorted_imports(lines: Sequence[str]) -> List[str]:
    """isort order inside a group: ``import x`` statements first, then ``from x import y`` by module name; the names of one
    module are merged into a single statement, ordered by type, and wrapped in parentheses when the line would be too long."""
    plain = sorted({line for line in lines if line.startswith("import ")}, key=lambda line: line.split()[1])
    merged: Dict[str, set] = {}
    for line in lines:
        if line.startswith("from "):
            module, names = line[len("from ") :].split(" import ", 1)
            merged.setdefault(module, set()).update(n.strip() for n in names.split(","))
    out = list(plain)
    for module in sorted(merged):
        names = sorted(merged[module], key=_name_key)
        one = f"from {module} import {', '.join(names)}"
        if len(one) <= LINE:
            out.append(one)
        else:
            out.append(f"from {module} import (\n" + "".join(f"    {n},\n" for n in names) + ")")
    return out


def _header(doc: str, stdlib: Sequence[str] = (), third_party: Sequence[str] = (), local: Sequence[str] = (), *, before_code: bool = False) -> List[str]:
    """Module docstring plus import groups (stdlib, third party, project), separated by blank lines. The block ends
    with one blank line; callers that continue with a ``def``/``class`` add the second one."""
    if len(doc) + 6 <= LINE:
        lines = [f'"""{doc}"""', ""]
    else:  # wrapped docstring: the generated code keeps its own 120-column rule
        wrapped = textwrap.wrap(doc, width=LINE - 3)
        lines = ['"""' + wrapped[0], *wrapped[1:], '"""', ""]
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


def _py_default(f: FieldSpec) -> str:
    """Python literal for a field default (pydantic/dataclass)."""
    if f.type == "decimal":
        return f'Decimal("{f.default}")'
    if f.type == "date":
        return f'date.fromisoformat("{f.default}")'
    if f.type == "datetime":
        return f'datetime.fromisoformat("{str(f.default).replace("Z", "+00:00")}")'
    return repr(f.default)


def _sql_default(f: FieldSpec) -> str:
    """SQL literal for a server default, as text()."""
    if f.type == "bool":
        return "true" if f.default else "false"
    if f.type in ("int", "float", "decimal"):
        return str(f.default)
    return "'" + str(f.default).replace("'", "''") + "'"


def _tenant(spec: Spec) -> str:
    return spec.security["multi_tenant"]["tenant_param"] if spec.multi_tenant else ""


def _registry(spec: Spec) -> bool:
    return spec.multi_tenant and spec.security["multi_tenant"]["binding"] == "api_key_registry"


def _oidc(spec: Spec) -> bool:
    return spec.security["authentication"] == "oauth2_jwt" and spec.oidc is not None


def _claim_binding(spec: Spec) -> bool:
    return spec.multi_tenant and spec.security["multi_tenant"]["binding"] == "principal_claim"


def _identity(spec: Spec) -> bool:
    """A per-caller principal exists (a key grant or a verified token); a shared API key has none."""
    return _registry(spec) or _oidc(spec)


def _authz(spec: Spec) -> bool:
    return spec.authz_model != "none"


def _rls(spec: Spec) -> bool:
    return spec.rls


def _principal_dep(spec: Spec) -> str:
    """The dependency alias that yields the principal bound to the addressed tenant (or just authenticated)."""
    return "TenantPrincipalDep" if spec.multi_tenant else "AuthenticatedPrincipal"


def _authenticator(spec: Spec) -> str:
    return "require_bearer_token" if _oidc(spec) else "require_api_key"


def _roles_all(spec: Spec) -> List[str]:
    return list(spec.security["authorization"]["roles"])


def _roles_admin(spec: Spec) -> List[str]:
    return list(spec.security["authorization"]["ownership"]["admin_roles"])


def _roles_member(spec: Spec) -> List[str]:
    """Roles of 'bob', the other member of the tenant: every declared role that is not an admin role."""
    admin = set(_roles_admin(spec))
    return [r for r in _roles_all(spec) if r not in admin]


def _allowed(spec: Spec, roles: Sequence[str], operation: str) -> bool:
    required = spec.security["authorization"]["operations"].get(operation)
    return not required or bool(set(roles) & set(required))


def _visibility(spec: Spec) -> str:
    return str(spec.security["authorization"]["ownership"]["visibility"])


def _identities(spec: Spec) -> List[Tuple[str, str, List[str]]]:
    """``(name, tenant constant, roles)`` of the callers the generated tests use: alice (tenant A, every role), bob (tenant A,
    member roles), admin (tenant A, admin roles; only when declared), limited (tenant A, no role; only with a role
    model), carol (tenant B, every role)."""
    out = [("alice", "TENANT_A", _roles_all(spec)), ("bob", "TENANT_A", _roles_member(spec))]
    if _roles_admin(spec):
        out.append(("admin", "TENANT_A", _roles_admin(spec)))
    if spec.with_roles:
        out.append(("limited", "TENANT_A", []))
    out.append(("carol", "TENANT_B", _roles_all(spec)))
    return out


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
    if _oidc(spec):
        deps.append('"pyjwt[crypto]>=2.9,<3"')
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
                f"# the application connects as the restricted role (no BYPASSRLS, no DDL); migrations run as the owner",
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
            f'    oidc_jwks_json: str = ""  # static JWKS document for tests only (refused unless environment == "test")',
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


def _security(spec: Spec) -> str:
    if _oidc(spec):
        return _security_oidc(spec)
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

Every configured key is a grant: the tenant it belongs to, the subject it acts as and the roles it holds.
``require_api_key`` authenticates the caller and yields the principal; ``require_tenant_member`` additionally resolves
the tenant path parameter and refuses, with 403, any request that addresses another tenant. This is authentication
plus tenant membership; per-operation and per-resource authorization live in core/authz.py (when the contract
declares a policy) and row-level security in the database (when the contract declares it).
"""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from {spec.package}.core.config import ApiKeyGrant, SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the subject it acts as, the tenant it belongs to and the roles it holds."""

    subject: str
    tenant_id: UUID
    roles: frozenset[str]


TenantPrincipal = Principal  # name used by projects generated before the identity model


def _grant_for(presented: str, keys: dict[str, ApiKeyGrant]) -> tuple[str, ApiKeyGrant] | None:
    matched: tuple[str, ApiKeyGrant] | None = None
    for key, grant in keys.items():  # every key is compared, so timing does not reveal which keys exist
        if secrets.compare_digest(key, presented):
            matched = (key, grant)
    return matched


def _subject(key: str, grant: ApiKeyGrant) -> str:
    return grant.subject or "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> Principal:
    if not settings.api_keys:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API keys are not configured")
    found = _grant_for(x_api_key, settings.api_keys) if x_api_key else None
    if found is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
    key, grant = found
    return Principal(subject=_subject(key, grant), tenant_id=grant.tenant_id, roles=frozenset(grant.roles))


AuthenticatedPrincipal = Annotated[Principal, Depends(require_api_key)]


def require_tenant_member({tenant}: UUID, principal: AuthenticatedPrincipal) -> Principal:
    if principal.tenant_id != {tenant}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the API key is not a member of this tenant")
    return principal


TenantPrincipalDep = Annotated[Principal, Depends(require_tenant_member)]
'''


def _security_oidc(spec: Spec) -> str:
    o = spec.oidc or {}
    tenant = _tenant(spec)
    claim_binding = _claim_binding(spec)
    algorithms = ", ".join(f'"{a}"' for a in o["algorithms"]) + ("," if len(o["algorithms"]) == 1 else "")
    required = ["exp", "iat", "iss", "aud", o["claims"]["subject"]] + ([o["claims"]["tenant"]] if claim_binding else [])
    tenant_field = "    tenant_id: UUID" if claim_binding else "    tenant_id: UUID | None"
    tenant_lines = []
    if claim_binding:
        tenant_lines = [
            "    raw_tenant = claims.get(TENANT_CLAIM)",
            "    try:",
            "        tenant_id = UUID(str(raw_tenant))",
            "    except (TypeError, ValueError) as exc:",
            '        raise _unauthorized("token has no usable tenant claim") from exc',
        ]
    else:
        tenant_lines = ["    tenant_id = None"]
    lines = [
        '"""OIDC bearer tokens issued by an external provider (authentication oauth2_jwt).',
        "",
        "A token is accepted only when: its header algorithm is one of ALGORITHMS (asymmetric; HS*/none are refused before",
        "any key is looked up), its signature verifies against the provider key named by ``kid``, ``iss`` equals the configured",
        "issuer, ``aud`` contains the configured audience, ``exp``/``nbf``/``iat`` hold within the leeway and the required claims",
        "are present.",
        "The principal is built from the verified claims only; nothing but the header is read before the signature check. The",
        "JWKS is fetched over https with TLS verification always on (a private CA may be added through oidc_ca_bundle, never",
        "removed), the response is capped in size, the keys are cached for ``oidc_jwks_cache_s`` seconds, a refresh is",
        "attempted at most once per MIN_REFRESH_INTERVAL_S (an unknown or forged kid cannot force a fetch per request), and",
        "when a refresh fails the last keys are served for at most ``oidc_jwks_stale_max_s``. A static JWKS is accepted only",
        "in the test environment. A provider that cannot be reached is 503 (JwksUnavailable), never 401: an outage is not a",
        "statement about the token. There are no refresh tokens, sessions or token issuance here.",
        '"""',
        "",
        "import json",
        "import logging",
        "import ssl",
        "import threading",
        "import time",
        "import urllib.error",
        "import urllib.request",
        "from collections.abc import Callable",
        "from dataclasses import dataclass",
        "from typing import Annotated",
        "from uuid import UUID",
        "",
        "import jwt",
        "from fastapi import Depends, Header, HTTPException, Request, status",
        "from jwt import PyJWK, PyJWKSet",
        "from jwt.exceptions import PyJWKError, PyJWKSetError",
        "",
        f"from {spec.package}.core.config import Settings, SettingsDep",
        "",
        f"ALGORITHMS = ({algorithms})",
        f'SUBJECT_CLAIM = "{o["claims"]["subject"]}"',
        f'TENANT_CLAIM = "{o["claims"]["tenant"]}"',
        f'ROLES_CLAIM = "{o["claims"]["roles"]}"',
        f"REQUIRED_CLAIMS = ({', '.join(json.dumps(c) for c in required)})",
        "MIN_REFRESH_INTERVAL_S = 30  # one JWKS fetch attempt per interval, whatever the reason (expiry, unknown kid, failure)",
        "MAX_JWKS_BYTES = 262_144  # a key set is a few kilobytes; anything larger is not one",
        f'LOG = logging.getLogger("{spec.package}.security")',
        'AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]',
        "",
        "",
        "@dataclass(frozen=True)",
        "class Principal:",
        '    """The authenticated caller, from verified claims only: subject, tenant and roles."""',
        "",
        "    subject: str",
        tenant_field,
        "    roles: frozenset[str]",
        "",
        "",
        "class JwksUnavailable(RuntimeError):",
        '    """The provider keys could not be obtained or read: an infrastructure failure, never a verdict on a token."""',
        "",
        "",
        "class JwksSource:",
        '    """Keys of the provider: the https JWKS endpoint behind a bounded, rate-limited cache, or a static document (tests',
        "    only). ``clock`` is injectable so the cache and refresh policy can be tested without waiting.",
        '    """',
        "",
        "    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:",
        "        self._url = settings.oidc_jwks_url",
        "        self._lifespan = settings.oidc_jwks_cache_s",
        "        self._stale_max = settings.oidc_jwks_stale_max_s",
        "        self._timeout = settings.oidc_fetch_timeout_s",
        "        self._clock = clock",
        "        self._lock = threading.Lock()",
        "        self._keys: PyJWKSet | None = None",
        "        self._fetched_at = 0.0",
        "        self._last_attempt: float | None = None",
        "        self._static = False",
        "        if settings.oidc_jwks_json:",
        '            if settings.environment != "test":',
        '                raise RuntimeError("a static JWKS (OIDC_JWKS_JSON) is accepted only when ENVIRONMENT is \'test\'")',
        "            self._keys = PyJWKSet.from_json(settings.oidc_jwks_json)",
        "            self._static = True",
        '        elif not self._url.startswith("https://"):',
        '            raise RuntimeError("OIDC_JWKS_URL must be an https URL")',
        "        # certificate and hostname verification are always on; a private CA can only be added, never substituted",
        "        self._tls = ssl.create_default_context(cafile=settings.oidc_ca_bundle or None)",
        "",
        "    def _fetch(self) -> PyJWKSet:",
        "        try:",
        "            with urllib.request.urlopen(self._url, timeout=self._timeout, context=self._tls) as response:",
        "                body = response.read(MAX_JWKS_BYTES + 1)",
        "        except urllib.error.HTTPError as exc:",
        '            raise JwksUnavailable(f"http {exc.code}") from exc',
        "        except (OSError, ssl.SSLError, TimeoutError) as exc:  # refused, unreachable, timed out, TLS failure",
        "            raise JwksUnavailable(f\"{type(exc).__name__}: {getattr(exc, 'reason', exc)}\") from exc",
        "        if len(body) > MAX_JWKS_BYTES:",
        '            raise JwksUnavailable("document too large")',
        "        try:",
        "            document = json.loads(body)",
        "            if not isinstance(document, dict):",
        '                raise TypeError("not an object")',
        "            keys = PyJWKSet.from_dict(document)",
        "        except (ValueError, TypeError, PyJWKError, PyJWKSetError) as exc:  # malformed JSON, not a key set, no key",
        '            raise JwksUnavailable(f"invalid JWKS document ({type(exc).__name__})") from exc',
        '        if not any(key.key_type in ("RSA", "EC", "OKP") for key in keys.keys):  # symmetric-only sets cannot sign for us',
        '            raise JwksUnavailable("the JWKS holds no asymmetric key")',
        "        return keys",
        "",
        "    def _refresh(self, now: float) -> None:",
        '        """One attempt; on failure the last keys stay in service for at most stale_max seconds, then nothing does."""',
        "        self._last_attempt = now",
        "        try:",
        "            self._keys, self._fetched_at = self._fetch(), now",
        "        except JwksUnavailable as exc:",
        "            age = now - self._fetched_at",
        "            if self._keys is not None and age <= self._stale_max:",
        '                LOG.warning("JWKS refresh failed (%s); serving the keys fetched %.0f s ago", exc, age)',
        "                return",
        '            LOG.warning("JWKS unavailable (%s) and no key set to fall back on", exc)',
        "            raise",
        "",
        "    def _match(self, kid: str) -> PyJWK | None:",
        "        for key in self._keys.keys if self._keys else []:",
        "            if key.key_id == kid:",
        "                return key",
        "        return None",
        "",
        "    def _may_attempt(self, now: float) -> bool:",
        "        return self._last_attempt is None or now - self._last_attempt >= MIN_REFRESH_INTERVAL_S",
        "",
        "    def key_for(self, kid: str) -> PyJWK | None:",
        '        """The signing key with this id: refreshed when the cache expired or the id is unknown, at most once per',
        '        MIN_REFRESH_INTERVAL_S; JwksUnavailable when no usable key set exists."""',
        "        if self._static:",
        "            return self._match(kid)",
        "        with self._lock:",
        "            now = self._clock()",
        "            expired = self._keys is None or now - self._fetched_at > self._lifespan",
        "            if expired and self._may_attempt(now):",
        "                self._refresh(now)",
        "            elif self._keys is None:",
        '                raise JwksUnavailable("provider unavailable; next attempt after the refresh interval")',
        "            found = self._match(kid)",
        "            if found is None and self._may_attempt(now):",
        "                self._refresh(now)  # key rotation: the id may have been published since the last fetch",
        "                found = self._match(kid)",
        "            return found",
        "",
        "",
        "def get_jwks(request: Request) -> JwksSource:",
        "    return request.app.state.jwks",
        "",
        "",
        "def _unauthorized(detail: str) -> HTTPException:",
        '    challenge = {"WWW-Authenticate": "Bearer"}',
        "    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=challenge)",
        "",
        "",
        "def verify_token(token: str, source: JwksSource, settings: Settings) -> dict[str, object]:",
        '    """The verified claims of ``token``: 401 for anything wrong with it, 503 when the provider keys cannot be read."""',
        "    try:",
        "        header = jwt.get_unverified_header(token)",
        "    except jwt.InvalidTokenError as exc:",
        '        raise _unauthorized("malformed token") from exc',
        '    if header.get("alg") not in ALGORITHMS:',
        '        raise _unauthorized("token algorithm is not accepted")',
        '    kid = header.get("kid")',
        "    if not isinstance(kid, str) or not kid:",
        '        raise _unauthorized("token has no key id")',
        "    try:",
        "        key = source.key_for(kid)",
        "    except JwksUnavailable as exc:",
        '        LOG.warning("identity provider keys unavailable: %s", exc)',
        '        detail = "identity provider keys are unavailable"',
        "        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc",
        "    if key is None:",
        '        raise _unauthorized("unknown signing key")',
        "    try:",
        "        return jwt.decode(",
        "            token,",
        "            key.key,",
        "            algorithms=list(ALGORITHMS),",
        "            audience=settings.oidc_audience,",
        "            issuer=settings.oidc_issuer,",
        "            leeway=settings.oidc_leeway_s,",
        '            options={"require": list(REQUIRED_CLAIMS)},',
        "        )",
        "    except jwt.InvalidTokenError as exc:",
        '        raise _unauthorized(f"invalid token ({type(exc).__name__})") from exc',
        "",
        "",
        "def _roles(claims: dict[str, object]) -> frozenset[str]:",
        "    raw = claims.get(ROLES_CLAIM, [])",
        "    if isinstance(raw, str):",
        "        raw = raw.split()",
        "    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):",
        '        raise _unauthorized("roles claim is malformed")',
        "    return frozenset(raw)",
        "",
        "",
        "def require_bearer_token(",
        "    settings: SettingsDep, source: Annotated[JwksSource, Depends(get_jwks)], authorization: AuthorizationHeader = None",
        ") -> Principal:",
        '    if not authorization or not authorization.startswith("Bearer "):',
        '        raise _unauthorized("missing bearer token")',
        '    claims = verify_token(authorization[len("Bearer ") :].strip(), source, settings)',
        "    subject = claims.get(SUBJECT_CLAIM)",
        "    if not isinstance(subject, str) or not subject:",
        '        raise _unauthorized("token has no usable subject claim")',
        *tenant_lines,
        "    return Principal(subject=subject, tenant_id=tenant_id, roles=_roles(claims))",
        "",
        "",
        "AuthenticatedPrincipal = Annotated[Principal, Depends(require_bearer_token)]",
    ]
    if claim_binding:
        lines += [
            "",
            "",
            f"def require_tenant_member({tenant}: UUID, principal: AuthenticatedPrincipal) -> Principal:",
            f"    if principal.tenant_id != {tenant}:",
            '        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the token is not a member of this tenant")',
            "    return principal",
            "",
            "",
            "TenantPrincipalDep = Annotated[Principal, Depends(require_tenant_member)]",
        ]
    return "\n".join(lines) + "\n"


def _authz_module(spec: Spec) -> str:
    authz = spec.security["authorization"]
    audit = spec.security["audit"]["authorization_decisions"]
    dep = _principal_dep(spec)
    ops_lines = []
    for op, roles in authz["operations"].items():
        ops_lines.append(f'    "{op}": frozenset({{{", ".join(json.dumps(r) for r in roles)}}}),')
    admin = ", ".join(json.dumps(r) for r in _roles_admin(spec))
    # the policy module names no entity: adding an owned module later must not regenerate it (it is not an integration point)
    lines = _header(
        f"Authorization policy declared by the contract (model {spec.authz_model}): per-operation roles checked by a route dependency "
        "before the use case runs, ownership of the entities declared owned checked by the service against the row it loaded and "
        "by the repository query that loaded it. The router never decides; it only carries the principal. A denial is 403; a "
        "resource outside the caller's visibility is 404 because the query does not return it, so existence is not revealed "
        "across owners or tenants.",
        stdlib=["from collections.abc import Callable", "from typing import Annotated"] + (["import logging"] if audit else []),
        third_party=["from fastapi import Depends"],
        local=[f"from {spec.package}.core.errors import ForbiddenError", f"from {spec.package}.core.security import Principal, {dep}"],
    )
    lines += [
        "# operations absent from this map are open to every authenticated member of the tenant",
        "OPERATION_ROLES: dict[str, frozenset[str]] = {" + ("" if ops_lines else "}"),
        *ops_lines,
        *(["}"] if ops_lines else []),
        f"ADMIN_ROLES: frozenset[str] = frozenset({{{admin}}})" if admin else "ADMIN_ROLES: frozenset[str] = frozenset()",
        f'OWNER_VISIBILITY = "{_visibility(spec)}"  # tenant: members read, owner/admin modify; owner: only owner/admin read or modify',
    ]
    if audit:
        lines.append(f'LOG = logging.getLogger("{spec.package}.authz")')
    lines += [
        "",
        "",
        "def allows_operation(principal: Principal, operation: str) -> bool:",
        "    required = OPERATION_ROLES.get(operation)",
        "    return not required or bool(principal.roles & required)",
        "",
        "",
        "def is_admin(principal: Principal) -> bool:",
        "    return bool(principal.roles & ADMIN_ROLES)",
        "",
        "",
        "def read_scope(principal: Principal) -> str | None:",
        '    """The owner a repository read must filter by; None when the principal sees the whole tenant."""',
        '    if OWNER_VISIBILITY == "tenant" or is_admin(principal):',
        "        return None",
        "    return principal.subject",
        "",
        "",
        "def deny(principal: Principal, operation: str, resource: str, reason: str) -> None:",
    ]
    if audit:
        lines += [
            "    LOG.info(",
            '        "authorization denied",',
            "        extra={",
            '            "subject": principal.subject,',
            '            "tenant_id": str(principal.tenant_id),',
            '            "operation": operation,',
            '            "resource": resource,',
            '            "reason": reason,',
            "        },",
            "    )",
        ]
    lines += [
        '    raise ForbiddenError(f"{operation} {resource}: {reason}")',
        "",
        "",
        "def ensure_owner(principal: Principal, owner_id: str, operation: str, resource: str) -> None:",
        '    """The caller owns the loaded row or holds an admin role; otherwise 403 (the row was visible, 404 would lie)."""',
        "    if owner_id == principal.subject or is_admin(principal):",
        "        return",
        '    deny(principal, operation, resource, "not the owner")',
        "",
        "",
        "def require_operation(operation: str) -> Callable[..., Principal]:",
        f"    def dependency(principal: {dep}) -> Principal:",
        "        if not allows_operation(principal, operation):",
        '            deny(principal, operation, "operation", f"needs one of {sorted(OPERATION_ROLES[operation])}")',
        "        return principal",
        "",
        "    return dependency",
        "",
        "",
        'CanCreate = Annotated[Principal, Depends(require_operation("create"))]',
        'CanGet = Annotated[Principal, Depends(require_operation("get"))]',
        'CanList = Annotated[Principal, Depends(require_operation("list"))]',
        'CanUpdate = Annotated[Principal, Depends(require_operation("update"))]',
        'CanDelete = Annotated[Principal, Depends(require_operation("delete"))]',
    ]
    return "\n".join(lines) + "\n"


def _db(spec: Spec) -> str:
    rls = _rls(spec)
    lines = [
        '"""Async engine, request-scoped sessions and the declarative base (SQLAlchemy 2, asyncpg).',
        "",
        "Tables are created by Alembic migrations only (``alembic upgrade head``); nothing here calls ``create_all``.",
        "The naming convention gives every constraint and index a deterministic name, so the migration scripts and the",
        "models agree and the test suite can compare them.",
    ]
    if rls:
        lines += [
            "",
            "Row-level security: the application connects as a role without BYPASSRLS, and ``get_tenant_session`` sets the",
            "tenant context for the current transaction only (``set_config(..., true)``), from the authenticated principal",
            "and never from the request. The policies see that tenant until commit or rollback; a connection returned to",
            "the pool carries no context (reset on return), so the next request starts closed.",
        ]
    lines += [
        '"""',
        "",
        "from collections.abc import AsyncIterator",
        "from typing import Annotated",
        "",
        "from fastapi import Depends, Request",
        "from sqlalchemy import MetaData" + (", text" if rls else ""),
        "from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine",
        "from sqlalchemy.orm import DeclarativeBase",
    ]
    if rls:
        lines += ["", f"from {spec.package}.core.security import TenantPrincipalDep"]
    lines += [
        "",
        "NAMING_CONVENTION = {",
        '    "ix": "ix_%(column_0_label)s",',
        '    "uq": "uq_%(table_name)s_%(column_0_N_name)s",',
        '    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",',
        '    "pk": "pk_%(table_name)s",',
        "}",
    ]
    if rls:
        lines.append(f'TENANT_CONTEXT_KEY = "{spec.security["multi_tenant"]["rls"]["context_key"]}"  # the GUC the policies read; set per transaction')
    lines += [
        "",
        "",
        "class Base(DeclarativeBase):",
        "    metadata = MetaData(naming_convention=NAMING_CONVENTION)",
        "",
        "",
        "def make_engine(url: str) -> AsyncEngine:",
        "    return create_async_engine(url, pool_pre_ping=True)",
        "",
        "",
        "async def get_session(request: Request) -> AsyncIterator[AsyncSession]:",
        '    """One session per request (unit of work): the service commits; any error rolls the unit back."""',
        "    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory",
        "    async with factory() as session:",
        "        try:",
        "            yield session",
        "        except BaseException:",
        "            await session.rollback()",
        "            raise",
        "",
        "",
        "SessionDep = Annotated[AsyncSession, Depends(get_session)]",
    ]
    if rls:
        lines += [
            "",
            "",
            "async def get_tenant_session(session: SessionDep, principal: TenantPrincipalDep) -> AsyncSession:",
            '    """The request session with the tenant context of the authenticated principal, for this transaction only."""',
            '    params = {"key": TENANT_CONTEXT_KEY, "tenant": str(principal.tenant_id)}',
            '    await session.execute(text("SELECT set_config(:key, :tenant, true)"), params)',
            "    return session",
            "",
            "",
            "TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]",
        ]
    return "\n".join(lines) + "\n"


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
    if _rls(spec):
        url_doc = f"URL from {prefix}_MIGRATIONS_DATABASE_URL (the owner)"
        url_body = f'    owner = os.environ.get("{prefix}_MIGRATIONS_DATABASE_URL")\n    return owner or os.environ.get("{prefix}_DATABASE_URL") or config.get_main_option("sqlalchemy.url")'
    else:
        url_doc = f"URL from {prefix}_DATABASE_URL"
        url_body = f'    return os.environ.get("{prefix}_DATABASE_URL") or config.get_main_option("sqlalchemy.url")'
    return f'''"""Alembic environment: async engine, {url_doc},
metadata of every generated model.
"""

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
{url_body}


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


def delta_revision(lineage: Lineage, spec: Spec) -> str:
    """Deterministic revision id of an additive migration: derived from the current head and the evolved contract."""
    return hashlib.sha1((f"delta|{lineage.head}|" + spec.digest).encode("utf-8")).hexdigest()[:12]


def _fk_constraint(table: str, f: FieldSpec, referred: str, tenant: str, sa: str, indent: str) -> List[str]:
    """``ForeignKeyConstraint`` for a reference, composite over the tenant id between tenant tables."""
    cols = [tenant, f.name] if tenant else [f.name]
    refs = [f"{referred}.{c}" if c == tenant else f"{referred}.id" for c in cols]
    args = [json.dumps(cols), json.dumps(refs), f'name="{_fk_name(table, cols, referred)}"', f'ondelete="{f.on_delete.upper().replace("_", " ")}"']
    lines = _call(f"{sa}ForeignKeyConstraint(", args, ")", indent)
    lines[-1] += ","
    return lines


def _column_sql(f: FieldSpec) -> str:
    default = f', server_default=sa.text("{_sql_default(f)}")' if f.has_default else ""
    return f'sa.Column("{f.name}", {_sa_type(f)}{default}, nullable={not f.required})'


def _baseline_columns(lineage: Optional[Lineage]) -> Dict[str, List[str]]:
    """``table -> column names`` already present according to the lineage's baseline contract."""
    if lineage is None or lineage.baseline is None:
        return {}
    out: Dict[str, List[str]] = {}
    for m in lineage.baseline.modules:
        for e in m.entities:
            out[e.plural] = [f.name for f in e.fields]
    return out


def added_columns(spec: Spec, lineage: Optional[Lineage]) -> List[Tuple[str, FieldSpec, Optional[str]]]:
    """``(table, field, referred table)`` for fields of existing tables that the baseline did not have."""
    existing = set(lineage.tables) if lineage else set()
    baseline_cols = _baseline_columns(lineage)
    index = _entity_index(spec)
    out: List[Tuple[str, FieldSpec, Optional[str]]] = []
    for _m, e in _ordered_entities(spec):
        if e.plural not in existing:
            continue
        for f in e.fields:
            if f.name not in baseline_cols.get(e.plural, []):
                referred = index[f.references][1].plural if f.references and f.references in index else None
                out.append((e.plural, f, referred))
    return out


def _migration(spec: Spec, lineage: Optional[Lineage] = None) -> str:
    """The initial migration (every table), or, with a lineage, the additive migration: the tables the existing
    chain does not create yet plus the columns added to existing tables (``down_revision`` = current head;
    existing files are never touched). Columns added to existing tables are nullable or carry a server default,
    which is the backfill strategy; anything else is refused by the planner before this runs. Under
    ``shared_schema_rls`` every new tenant table is protected in the same revision that creates it (ENABLE + FORCE
    row level security, one USING/WITH CHECK policy on the transaction-local tenant context, row privileges for the
    application role, which must exist and must not bypass RLS)."""
    tenant = _tenant(spec)
    rls = _rls(spec)
    index = _entity_index(spec)
    revision = delta_revision(lineage, spec) if lineage else initial_revision(spec)
    existing = set(lineage.tables) if lineage else set()
    owned = {e.plural for _m, e in spec.owned_entities()}
    up: List[str] = []
    down: List[str] = []
    new_tables = [e.plural for _m, e in _ordered_entities(spec) if e.plural not in existing]
    if rls and new_tables:
        up.append("    op.execute(f\"GRANT USAGE ON SCHEMA public TO {_app_role()}\")")
    for _m, e in _ordered_entities(spec):
        table = e.plural
        if table in existing:
            continue
        cols = ['        sa.Column("id", sa.Uuid(), nullable=False),']
        if tenant:
            cols.append(f'        sa.Column("{tenant}", sa.Uuid(), nullable=False),')
        if table in owned:
            cols.append('        sa.Column("owner_id", sa.String(length=255), nullable=False),')
        for f in e.fields:
            cols.append(f"        {_column_sql(f)},")
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
        if table in owned:
            up.append(f'    op.create_index("ix_{table}_owner_id", "{table}", ["owner_id"], unique=False)')
        for f in _fk_fields(e):
            up.append(f'    op.create_index("ix_{table}_{f.name}", "{table}", ["{f.name}"], unique=False)')
        if rls:
            up.append(f'    _protect("{table}")')
        drop = [f'    op.drop_table("{table}")']
        for f in reversed(_fk_fields(e)):
            drop.insert(0, f'    op.drop_index("ix_{table}_{f.name}", table_name="{table}")')
        if table in owned:
            drop.insert(0, f'    op.drop_index("ix_{table}_owner_id", table_name="{table}")')
        if tenant:
            drop.insert(0, f'    op.drop_index("ix_{table}_{tenant}", table_name="{table}")')
        down = drop + down
    columns = added_columns(spec, lineage)
    for table, f, referred in columns:
        up.append(f"    op.add_column({json.dumps(table)}, {_column_sql(f)})")
        if referred:
            cols = [tenant, f.name] if tenant else [f.name]
            refs = [f"{tenant}" if c == tenant else "id" for c in cols]
            up.append(f'    op.create_foreign_key({json.dumps(_fk_name(table, cols, referred))}, {json.dumps(table)}, {json.dumps(referred)}, {json.dumps(cols)}, {json.dumps(refs)}, ondelete="{f.on_delete.upper().replace("_", " ")}")')
            up.append(f'    op.create_index("ix_{table}_{f.name}", "{table}", ["{f.name}"], unique=False)')
    for table, f, referred in reversed(columns):
        if referred:
            down.insert(0, f'    op.drop_column("{table}", "{f.name}")')
            down.insert(0, f'    op.drop_constraint({json.dumps(_fk_name(table, [tenant, f.name] if tenant else [f.name], referred))}, "{table}", type_="foreignkey")')
            down.insert(0, f'    op.drop_index("ix_{table}_{f.name}", table_name="{table}")')
        else:
            down.insert(0, f'    op.drop_column("{table}", "{f.name}")')
    changes = new_tables + [f"{t}.{f.name}" for t, f, _r in columns]
    title = f"Initial schema of {spec.name}" if not lineage else f"Add {', '.join(changes)} to {spec.name}"
    imports = ["import sqlalchemy as sa", "from alembic import op"]
    helpers: List[str] = []
    if rls:
        prefix = spec.package.upper()
        rls_cfg = spec.security["multi_tenant"]["rls"]
        imports = ["import os", "import re", "", "import sqlalchemy as sa", "from alembic import context, op"]
        helpers = [
            "",
            f'APP_ROLE = os.environ.get("{prefix}_APP_ROLE", "{rls_cfg["app_role"]}")',
            f'CONTEXT_KEY = "{rls_cfg["context_key"]}"',
            "",
            "",
            "def _app_role() -> str:",
            '    """The application role, verified on the live connection: it must exist and must not be able to bypass row-level',
            '    security (superuser or BYPASSRLS). Creating roles is a provisioning step, not a migration: the owner that runs the',
            '    migrations may not have CREATEROLE, and a role created here with a default password would be a credential."""',
            '    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", APP_ROLE):',
            f'        raise RuntimeError(f"{prefix}_APP_ROLE {{APP_ROLE!r}} is not a plain role name")',
            "    if context.is_offline_mode():",
            '        raise RuntimeError("row-level security migrations need a live connection to verify the application role")',
            '    query = sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :name")',
            '    row = op.get_bind().execute(query, {"name": APP_ROLE}).first()',
            "    if row is None:",
            "        raise RuntimeError(",
            '            f"role {APP_ROLE} does not exist; create it first: "',
            '            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD \'...\' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"',
            "        )",
            "    if row.rolsuper or row.rolbypassrls:",
            '        raise RuntimeError(f"role {APP_ROLE} would bypass row-level security (superuser or BYPASSRLS); refusing")',
            "    return APP_ROLE",
            "",
            "",
            "def _protect(table: str) -> None:",
            '    """Row-level security for a tenant table: enabled and forced (the owner is subject to it too), one policy that both',
            "    filters reads (USING) and checks writes (WITH CHECK) against the transaction-local tenant context; a NULL or empty",
            '    context matches nothing. The application role gets row privileges only: no DDL, no TRUNCATE, no ownership."""',
            "    role = _app_role()",
            f'    predicate = f"{tenant} = NULLIF(current_setting(\'{{CONTEXT_KEY}}\', true), \'\')::uuid"',
            '    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {role}")',
            '    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")',
            '    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")',
            '    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})")',
        ]
    lines = [
        f'"""{title}: generated from the architecture contract (digest {spec.digest[:12]}).',
        "",
        f"Revision ID: {revision}",
        f"Revises: {lineage.head if lineage else 'none (initial)'}",
        "Create Date: derived from the contract, not from the clock (reproducible)",
        '"""',
        "",
        *imports,
        "",
        f'revision = "{revision}"',
        f"down_revision = {json.dumps(lineage.head) if lineage else 'None'}",
        "branch_labels = None",
        "depends_on = None",
        *helpers,
        "",
        "",
        "def upgrade() -> None:",
        *(up or ["    pass"]),
        "",
        "",
        "def downgrade() -> None:",
        *(down or ["    pass"]),
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
                if f.has_default:
                    args = [f"default={_py_default(f)}"] + ([constraints] if constraints else [])
                    create_lines += _call(f"{f.name}: {_annotation(f)} = Field(", args, ")", "    ")
                elif f.required and not constraints:
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
        read_lines = ["    id: UUID"] + ([f"    {tenant}: UUID"] if tenant else []) + (["    owner_id: str"] if e.owned else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields] + ["    created_at: datetime"]
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
            body = ([f"    {tenant}: UUID"] if tenant else []) + (["    owner_id: str  # subject of the principal that created the row"] if e.owned else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields if f.required and not f.has_default]
            body += [f"    {f.name}: {_annotation(f)} = {_py_default(f) if f.has_default else 'None'}" for f in e.fields if not f.required or f.has_default]
            body += ["    id: UUID = field(default_factory=uuid4)", "    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))"]
            lines += ["", "@dataclass", f"class {e.name}:", *body, ""]
        return "\n".join(lines).rstrip("\n") + "\n"
    index = _entity_index(spec)
    sa_names = {SA_TYPES[f.type] for f in all_fields} | {"DateTime", "Uuid", "func"}
    if any(f.has_default for f in all_fields):
        sa_names.add("text")
    if any(_fk_fields(e) for e in module.entities):
        sa_names.add("ForeignKeyConstraint")
    if any(e.owned for e in module.entities):
        sa_names.add("String")
    if tenant:
        sa_names.add("UniqueConstraint")
    lines = _header(
        "SQLAlchemy 2 mapped classes of the module (tables are created by Alembic, never by create_all).",
        stdlib=_type_imports(all_fields, uuid=("UUID", "uuid4")),
        third_party=[f"from sqlalchemy import {', '.join(sorted(sa_names))}", "from sqlalchemy.orm import Mapped, mapped_column"],
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
        if e.owned:
            body.append("    owner_id: Mapped[str] = mapped_column(String(255), index=True)  # subject of the creating principal")
        for f in e.fields:
            col = SA_TYPES[f.type]
            arg = f"String({f.max_length or 255})" if f.type == "str" else ("Numeric(18, 2)" if f.type == "decimal" else ("DateTime(timezone=True)" if f.type == "datetime" else col))
            extras = ("" if f.required else ", nullable=True") + (", index=True" if f.references else "")
            if f.has_default:
                extras += f", default={_py_default(f)}, server_default=text(\"{_sql_default(f)}\")"
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
        "Repositories: a Protocol per entity and the implementation chosen by the design."
        + (" Owned entities take an optional owner: when given, reads are restricted to that owner's rows in the query itself." if any(e.owned for e in module.entities) else ""),
        stdlib=["from typing import Protocol", "from uuid import UUID"],
        third_party=["from sqlalchemy import select", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=[f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}"],
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        o_sig = ", owner: str | None = None" if e.owned else ""
        proto: List[str] = []
        if ops["create"]:
            proto.append(f"    async def add(self, {x}: {E}) -> {E}: ...")
        if ops["get"]:
            proto.append(f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None: ...")
        if ops["list"]:
            proto.append(f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]: ...")
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
                conditions = ([f"item.{tenant} != {tenant}"] if tenant else []) + (["(owner is not None and item.owner_id != owner)"] if e.owned else [])
                if conditions:
                    impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None:", f"        item = self._items.get({x}_id)", f"        if item is None or {' or '.join(conditions)}:", "            return None", "        return item"]
                else:
                    impl += ["", f"    async def get(self, {x}_id: UUID) -> {E} | None:", f"        return self._items.get({x}_id)"]
            if ops["list"]:
                conditions = ([f"item.{tenant} == {tenant}"] if tenant else []) + (["(owner is None or item.owner_id == owner)"] if e.owned else [])
                if not conditions:
                    source = ["        scoped = list(self._items.values())"]
                elif len(f"        scoped = [item for item in self._items.values() if {' and '.join(conditions)}]") <= LINE:
                    source = [f"        scoped = [item for item in self._items.values() if {' and '.join(conditions)}]"]
                else:
                    source = ["        scoped = [", "            item", "            for item in self._items.values()", f"            if {' and '.join(conditions)}", "        ]"]
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]:", *source, "        items = sorted(scoped, key=lambda item: (item.created_at, str(item.id)))", "        return items[offset : offset + limit]"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", f"        del self._items[{x}_id]", "        return True"]
        else:
            where = f".where({E}.{tenant} == {tenant})" if tenant else ""
            owner_filter = ["        if owner is not None:", f"            statement = statement.where({E}.owner_id == owner)"] if e.owned else []
            impl += [f"class Sql{E}Repository:", '    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""', "", "    def __init__(self, session: AsyncSession) -> None:", "        self._session = session"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._session.add({x})", f"        return {x}"]
            if ops["get"]:
                impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None:", f"        statement = select({E}).where({E}.id == {x}_id){where}", *owner_filter, "        return await self._session.scalar(statement)"]
            if ops["list"]:
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]:", f"        statement = select({E}){where}.order_by({E}.created_at, {E}.id)", *owner_filter, "        rows = await self._session.scalars(statement.limit(limit).offset(offset))", "        return list(rows)"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        return {x}  # tracked by the session; written at commit"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", "        await self._session.delete(item)", "        return True"]
        lines += ["", *impl, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _service(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    authz = _authz(spec)
    owned_here = any(e.owned for e in module.entities)
    # without a policy the use cases take the tenant id; with one they take the principal and derive tenant, owner and scope
    t_sig = "principal: Principal, " if authz else (f"{tenant}: UUID, " if tenant else "")
    t_arg = ("principal.tenant_id, " if tenant else "") if authz else (f"{tenant}, " if tenant else "")
    schema_names: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + ([f"{e.name}Update"] if ops["update"] else [])
    local = [
        f"from {spec.package}.core.errors import {'ConflictError, ' if pg else ''}NotFoundError",
        f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}",
        f"from {layout.imp('repository', module.name)} import {', '.join(e.name + 'Repository' for e in module.entities)}",
    ]
    if authz:
        local.append(f"from {spec.package}.core.security import Principal")
    if owned_here:
        local.append(f"from {spec.package}.core.authz import ensure_owner, read_scope")
    if schema_names:
        local.append(f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    doc = "Use cases of the module: validation beyond the schema, domain errors, one commit per use case."
    if owned_here:
        doc += " Ownership is enforced here and in the repository query (read_scope / ensure_owner), never in the router alone."
    lines = _header(
        doc,
        stdlib=["from uuid import UUID"],
        third_party=["from sqlalchemy.exc import IntegrityError", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=local,
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        o_arg = ", owner=read_scope(principal)" if e.owned else ""
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
            kwargs = ([f"{tenant}=principal.tenant_id" if authz else f"{tenant}={tenant}"] if tenant else []) + (["owner_id=principal.subject"] if e.owned else []) + [f"{f.name}=data.{f.name}" for f in e.fields]
            notify = [f'        await self._{p.name}.send({{"event": "{x}.created", "id": str({x}.id)}})' for p in ports]
            body += ["", f"    async def create(self, {t_sig}data: {E}Create) -> {E}:", *_call(f"{x} = {E}(", kwargs, ")", "        "), f"        await self._repository.add({x})", *commit, *notify, f"        return {x}"]
        if ops["get"]:
            body += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E}:", f"        {x} = await self._repository.get({t_arg}{x}_id{o_arg})", f"        if {x} is None:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', f"        return {x}"]
        if ops["list"]:
            body += ["", f"    async def list(self, {t_sig}limit: int = 50, offset: int = 0) -> list[{E}]:", f"        return await self._repository.list({t_arg}limit, offset{o_arg})"]
        if ops["update"]:
            guard = [f'        ensure_owner(principal, {x}.owner_id, "update", "{E}")'] if e.owned else []
            body += ["", f"    async def update(self, {t_sig}{x}_id: UUID, data: {E}Update) -> {E}:", f"        {x} = await self.get({'principal, ' if authz else t_arg}{x}_id)", *guard, "        for name, value in data.model_dump(exclude_unset=True).items():", f"            setattr({x}, name, value)", f"        await self._repository.save({x})", *commit, f"        return {x}"]
        if ops["delete"]:
            if e.owned:
                body += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> None:", f"        {x} = await self.get(principal, {x}_id)", f'        ensure_owner(principal, {x}.owner_id, "delete", "{E}")', f"        await self._repository.delete({t_arg}{x}.id)", *commit]
            else:
                body += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> None:", f"        removed = await self._repository.delete({t_arg}{x}_id)", "        if not removed:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', *commit]
        lines += ["", *body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _dependencies(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    session_dep = "TenantSessionDep" if _rls(spec) else "SessionDep"
    local = [
        f"from {layout.imp('repository', module.name)} import {', '.join(f'{e.name}Repository' + (f', Sql{e.name}Repository' if pg else '') for e in module.entities)}",
        f"from {layout.imp('service', module.name)} import {', '.join(e.name + 'Service' for e in module.entities)}",
    ]
    if pg:
        local.append(f"from {spec.package}.core.db import {session_dep}")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    needs_request = (not pg) or bool(ports)
    doc = "Composition of the module: repositories, ports and services as FastAPI dependencies."
    if _rls(spec):
        doc += " The session carries the tenant context of the authenticated principal (TenantSessionDep), so every query runs under the row-level security policies."
    lines = _header(
        doc,
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
            repo = [f"def get_{x}_repository(session: {session_dep}) -> {E}Repository:", f"    return Sql{E}Repository(session)"]
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]", f"session: {session_dep}"] + port_args
            call = f"{E}Service(repository, session{port_pass})"
        else:
            repo = [f"def get_{x}_repository(request: Request) -> {E}Repository:", f'    return request.app.state.repositories["{x}"]']
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]"] + port_args
            call = f"{E}Service(repository{port_pass})"
        lines += ["", *repo, "", "", *_call(f"def get_{x}_service(", args, f") -> {E}Service:"), f"    return {call}", "", "", f"{E}ServiceDep = Annotated[{E}Service, Depends(get_{x}_service)]", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _router(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    tenant = _tenant(spec)
    bound = _registry(spec) or _claim_binding(spec)
    authz = _authz(spec)
    # with a tenant binding the tenant comes from the authenticated principal, never from a bare path parameter;
    # with a policy every route carries the principal admitted for its operation (core/authz.py) and passes it on
    can = {"create": "CanCreate", "list": "CanList", "get_route": "CanGet", "update": "CanUpdate", "delete": "CanDelete"}
    if authz:
        t_arg = "principal, "
    elif bound:
        t_arg = "principal.tenant_id, "
    else:
        t_arg = f"{tenant}, " if tenant else ""
    base_params = [] if authz else (["principal: TenantPrincipalDep"] if bound else ([f"{tenant}: UUID"] if tenant else []))

    def params(op: str) -> List[str]:
        return [f"principal: {can[op]}"] if authz else base_params

    schema_names: List[str] = []
    used_can: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + [f"{e.name}Read"] + ([f"{e.name}Update"] if ops["update"] else [])
        for op in ("create", "list", "get_route", "update", "delete"):
            if ops[op] and can[op] not in used_can:
                used_can.append(can[op])
    local = [f"from {layout.imp('dependencies', module.name)} import {', '.join(e.name + 'ServiceDep' for e in module.entities)}", f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}"]
    if authz:
        local.append(f"from {spec.package}.core.authz import {', '.join(sorted(used_can))}")
    elif bound:
        local.append(f"from {spec.package}.core.security import TenantPrincipalDep")
    lines = _header(
        f"HTTP boundary of the {module.name} module: schema validation, status codes, problem details for domain errors."
        + (" Each route carries the principal admitted for its operation; the service decides on the resource." if authz else ""),
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
            lines += ["", "", f'@router.post("{prefix}", status_code=status.HTTP_201_CREATED)', *_call(f"async def create_{x}(", params("create") + [f"payload: {E}Create", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.create({t_arg}payload))"]
        if ops["list"]:
            lines += ["", "", f'@router.get("{prefix}")', f"async def list_{e.plural}(", *[f"    {p}," for p in params("list")], f"    service: {E}ServiceDep,", "    limit: Annotated[int, Query(ge=1, le=200)] = 50,", "    offset: Annotated[int, Query(ge=0)] = 0,", f") -> list[{E}Read]:", f"    return [{E}Read.model_validate(item) for item in await service.list({t_arg}limit, offset)]"]
        if ops["get_route"]:
            lines += ["", "", f'@router.get("{prefix}/{{{x}_id}}")', *_call(f"async def get_{x}(", params("get_route") + [f"{x}_id: UUID", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.get({t_arg}{x}_id))"]
        if ops["update"]:
            lines += ["", "", f'@router.patch("{prefix}/{{{x}_id}}")', *_call(f"async def update_{x}(", params("update") + [f"{x}_id: UUID", f"payload: {E}Update", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.update({t_arg}{x}_id, payload))"]
        if ops["delete"]:
            lines += ["", "", f'@router.delete("{prefix}/{{{x}_id}}", status_code=status.HTTP_204_NO_CONTENT)', *_call(f"async def delete_{x}(", params("delete") + [f"{x}_id: UUID", f"service: {E}ServiceDep"], ") -> None:"), f"    await service.delete({t_arg}{x}_id)"]
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
    oidc = _oidc(spec)
    identity = _identity(spec)
    pg = spec.persistence["kind"] == "postgresql"
    rls = _rls(spec)
    tenant = _tenant(spec)
    prefix = spec.package.upper()
    o = spec.oidc or {}
    settings_kwargs = ['app_name="test"']
    if registry:
        settings_kwargs.append("api_keys=API_KEYS")
    elif api_key:
        settings_kwargs.append('api_key=SecretStr("test-key")')
    if oidc:
        settings_kwargs += ['environment="test"', "oidc_jwks_json=jwks_json()"]
    if pg:
        settings_kwargs.append("database_url=database_url" if rls else "database_url=migrated")
    local = [f"from {spec.package}.core.config import Settings", f"from {spec.package}.main import create_app"]
    local += sorted({f"from {layout.adapter_imp(m.name, i.name)} import Recording{_camel(i.name)}Adapter" for m, i in ports})
    if pg:
        local.append(f"from {spec.package}.core.db import Base")
    ports_items = [f'"{i.name}": Recording{_camel(i.name)}Adapter()' for _m, i in ports]
    stdlib = (["import asyncio", "import os", "import subprocess", "import sys", "from pathlib import Path"] if pg else []) + (["from uuid import UUID"] if identity or tenant else [])
    if oidc:
        stdlib += ["import json", "import time"]
    third = ["import pytest", "from fastapi.testclient import TestClient"] + (["from pydantic import SecretStr"] if api_key and not registry else [])
    algorithm = o.get("algorithms", ["RS256"])[0] if oidc else ""
    if oidc:
        third.append("import jwt")
        if algorithm.startswith(("RS", "PS")):
            third += ["from cryptography.hazmat.primitives.asymmetric import rsa", "from jwt.algorithms import RSAAlgorithm"]
        elif algorithm.startswith("ES"):
            third += ["from cryptography.hazmat.primitives.asymmetric import ec", "from jwt.algorithms import ECAlgorithm"]
        else:
            third += ["from cryptography.hazmat.primitives.asymmetric import ed25519", "from jwt.algorithms import OKPAlgorithm"]
    if pg:
        third += ["from sqlalchemy import inspect, text", "from sqlalchemy.ext.asyncio import create_async_engine"]
    doc = "Test fixtures: one application per test" + (" on a PostgreSQL database migrated by Alembic and truncated between tests" if pg else " with its own in-memory state")
    if rls:
        doc += f"; the application connects as the restricted role (DATABASE_URL), migrations and truncation as the owner ({prefix}_MIGRATIONS_DATABASE_URL)"
    if identity:
        doc += ". Identities: alice (tenant A, every role), bob (tenant A, member roles), admin and limited (tenant A) when the policy declares them, carol (tenant B)"
    lines = _header(doc + ".", stdlib=stdlib, third_party=third, local=local)
    if identity or tenant:
        lines += [f'TENANT_A = UUID("{TENANT_A}")', f'TENANT_B = UUID("{TENANT_B}")']
    if identity:
        lines += ["# name -> (tenant, roles); the credentials of each identity come from credentials(name)", "IDENTITIES: dict[str, tuple[UUID, list[str]]] = {"]
        for name, tenant_const, roles in _identities(spec):
            lines.append(f'    "{name}": ({tenant_const}, {json.dumps(roles)}),')
        lines.append("}")
    if registry:
        lines += [
            "API_KEYS = {",
            '    f"key-{name}": {"tenant_id": tenant, "subject": name, "roles": roles}',
            "    for name, (tenant, roles) in IDENTITIES.items()",
            "}",
            "",
            "",
            "def credentials(identity: str) -> dict[str, str]:",
            '    return {"X-API-Key": f"key-{identity}"}',
        ]
    if oidc:
        if algorithm.startswith(("RS", "PS")):
            keygen = "rsa.generate_private_key(public_exponent=65537, key_size=2048)"
            to_jwk = "RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key())"
        elif algorithm.startswith("ES"):
            curve = {"ES256": "SECP256R1", "ES384": "SECP384R1", "ES512": "SECP521R1"}[algorithm]
            keygen = f"ec.generate_private_key(ec.{curve}())"
            to_jwk = "ECAlgorithm.to_jwk(PRIVATE_KEY.public_key())"
        else:
            keygen = "ed25519.Ed25519PrivateKey.generate()"
            to_jwk = "OKPAlgorithm.to_jwk(PRIVATE_KEY.public_key())"
        lines += [
            f'ISSUER = "{o["issuer"]}"',
            f'AUDIENCE = "{o["audience"]}"',
            f'ALGORITHM = "{algorithm}"',
            f'TENANT_CLAIM = "{o["claims"]["tenant"]}"',
            f'ROLES_CLAIM = "{o["claims"]["roles"]}"',
            f'SUBJECT_CLAIM = "{o["claims"]["subject"]}"',
            'KID = "test-key-1"',
            "",
            "",
            "def new_private_key():",
            '    """A fresh signing key of the contract algorithm family (the fake provider of tests/test_jwks.py rotates them)."""',
            f"    return {keygen}",
            "",
            "",
            "def public_jwk(private_key, kid: str) -> dict:",
            f"    jwk = json.loads({to_jwk.replace('PRIVATE_KEY', 'private_key')})",
            '    jwk.update({"kid": kid, "alg": ALGORITHM, "use": "sig"})',
            "    return jwk",
            "",
            "",
            "PRIVATE_KEY = new_private_key()  # one key pair per session",
            "",
            "",
            "def jwks_json() -> str:",
            '    return json.dumps({"keys": [public_jwk(PRIVATE_KEY, KID)]})',
            "",
            "",
            "def mint(",
            "    identity: str = \"alice\",",
            "    *,",
            "    issuer: str = ISSUER,",
            "    audience: str = AUDIENCE,",
            "    expires_in: int = 300,",
            "    not_before: int = -5,",
            "    kid: str | None = KID,",
            "    algorithm: str = ALGORITHM,",
            "    key: object = PRIVATE_KEY,",
            "    overrides: dict[str, object] | None = None,",
            "    drop: tuple[str, ...] = (),",
            ") -> str:",
            '    """A token for an identity, with the knobs the negative tests need (issuer, expiry, key, missing claims)."""',
            "    tenant, roles = IDENTITIES[identity]",
            "    now = int(time.time())",
            "    claims: dict[str, object] = {",
            '        "iss": issuer,',
            '        "aud": audience,',
            "        SUBJECT_CLAIM: identity,",
            '        "iat": now - 5,',
            '        "nbf": now + not_before,',
            '        "exp": now + expires_in,',
            "        TENANT_CLAIM: str(tenant),",
            "        ROLES_CLAIM: roles,",
            "    }",
            "    claims.update(overrides or {})",
            "    for name in drop:",
            "        claims.pop(name, None)",
            "    headers = {\"kid\": kid} if kid is not None else {}",
            "    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)",
            "",
            "",
            "def credentials(identity: str) -> dict[str, str]:",
            '    return {"Authorization": f"Bearer {mint(identity)}"}',
        ]
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
            f"    env = dict(os.environ, {prefix}_DATABASE_URL=url{f', {prefix}_MIGRATIONS_DATABASE_URL=url' if rls else ''})",
            '    command = [sys.executable, "-m", "alembic", *args]',
            "    return subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)",
            "",
            "",
            '@pytest.fixture(scope="session")',
            "def database_url() -> str:",
            '    url = os.environ.get("DATABASE_URL")',
            "    if not url:",
            f'        pytest.skip("set DATABASE_URL to an empty, disposable PostgreSQL database{" (application role)" if rls else ""}")',
            "    return url",
        ]
        if rls:
            lines += [
                "",
                "",
                '@pytest.fixture(scope="session")',
                "def owner_url() -> str:",
                f'    url = os.environ.get("{prefix}_MIGRATIONS_DATABASE_URL")',
                "    if not url:",
                f'        pytest.skip("set {prefix}_MIGRATIONS_DATABASE_URL to the owner of the same database")',
                "    return url",
                "",
                "",
                '@pytest.fixture(scope="session")',
                "def migrated(owner_url: str):",
                '    """Only an empty database is accepted: the suite migrates, truncates and downgrades it as the owner; the',
                '    application never connects as the owner."""',
                "    existing = [t for t in run(table_names(owner_url)) if t != \"alembic_version\"]  # a downgrade leaves it empty",
                "    if existing:",
                f'        pytest.fail(f"{prefix}_MIGRATIONS_DATABASE_URL must be an empty database; found tables: {{existing}}")',
                '    result = alembic(owner_url, "upgrade", "head")',
                "    assert result.returncode == 0, result.stderr",
                "    yield owner_url",
                '    alembic(owner_url, "downgrade", "base")',
            ]
        else:
            lines += [
                "",
                "",
                '@pytest.fixture(scope="session")',
                "def migrated(database_url: str):",
                '    """Only an empty database is accepted: the suite migrates, truncates and downgrades it."""',
                "    existing = [t for t in run(table_names(database_url)) if t != \"alembic_version\"]  # a downgrade leaves it empty",
                "    if existing:",
                '        pytest.fail(f"DATABASE_URL must point at an empty database; found tables: {existing}")',
                '    result = alembic(database_url, "upgrade", "head")',
                "    assert result.returncode == 0, result.stderr",
                "    yield database_url",
                '    alembic(database_url, "downgrade", "base")',
            ]
        lines += [
            "",
            "",
            "@pytest.fixture(autouse=True)",
            "def clean_tables(migrated: str):",
            "    yield",
            "    run(truncate_all(migrated))",
        ]
    if lines[-1] != "":
        lines.append("")
    app_params = ("migrated: str, database_url: str" if rls else "migrated: str") if pg else ""
    lines += ["", "@pytest.fixture", f"def app({app_params}):", *_call("settings = Settings(", settings_kwargs, ")", "    ")]
    if ports:
        lines += [*_call("ports = {", ports_items, "}", "    "), "    return create_app(settings, ports=ports)"]
    else:
        lines.append("    return create_app(settings)")
    if identity:
        headers_value = 'credentials("alice")'
    elif api_key:
        headers_value = '{"X-API-Key": "test-key"}'
    else:
        headers_value = "{}"
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
        f"    return {headers_value}",
    ]
    if identity:
        lines += ["", "", "@pytest.fixture", "def auth():", '    """credentials(identity): the headers of alice, bob, admin, limited or carol."""', "    return credentials"]
    if tenant:
        lines += ["", "", "@pytest.fixture", "def tenant() -> UUID:", '    """The tenant of alice and bob (the default headers); carol belongs to TENANT_B."""', "    return TENANT_A"]
    return "\n".join(lines) + "\n"


def _tests(spec: Spec, module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    api_key = spec.security["authentication"] == "api_key"
    oidc = _oidc(spec)
    identity = _identity(spec)
    authz = _authz(spec)
    bound = _registry(spec) or _claim_binding(spec)
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    index = _entity_index(spec)
    item = 'f"{url}/{created[\'id\']}"'
    caller = "client, headers, tenant" if tenant else "client, headers"
    bob_roles, admin_roles = _roles_member(spec), _roles_admin(spec)
    visibility = _visibility(spec)
    # helpers of other modules: create_<parent>(client, headers[, tenant]) builds a valid row with its own parents
    parent_helpers = sorted({(index[f.references][0].name, index[f.references][1].snake) for e in module.entities for f in _fk_fields(e) if pg and index[f.references][0].name != module.name})  # type: ignore[index]
    local = [f"from tests.test_{m} import {', '.join('create_' + x for _m, x in group)}" for m, group in _group(parent_helpers)]
    doc = f"API tests of the {module.name} module through the HTTP boundary"
    if authz:
        doc += ": every declared operation, the role each one needs, ownership across members and admins, and tenant isolation"
    lines: List[str] = []  # the header is prepended at the end, once the conftest names the body uses are known
    first = True
    for e in module.entities:
        ops = _ops(e)
        x, up = e.snake, e.snake.upper()
        required = [f for f in e.fields if f.required]
        invalid = "{}" if required else "{" + f'"{e.fields[0].name}": {WRONG[e.fields[0].type]}' + "}"
        fk_fields = _fk_fields(e) if pg else []
        lines += ([] if first else [""]) + _payload_literal(e.fields, f"PAYLOAD_{up}")
        first = False
        if tenant:
            lines += ["", "", f"def {x}_url(tenant: uuid.UUID) -> str:", f'    return f"/api/v1/tenants/{{tenant}}/{e.plural}"']
            setup = [f"    url = {x}_url(tenant)"]
            fixtures_default = "client, headers, tenant"
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
        other_tenant_url = f"{x}_url(TENANT_B)"

        def test(name: str, body: List[str], fixtures: str = fixtures_default, with_payload: bool = True, with_url: bool = True) -> None:
            lines.extend(["", "", f"def {name}({fixtures}):", *(setup if with_url else []), *(prepare if with_payload else []), *body])

        def as_other(identity_name: str) -> str:
            return f'credentials("{identity_name}")'

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
                if bound:
                    _pm, pe = index[f.references]  # type: ignore[index]
                    test(f"test_create_{x}_cannot_reference_a_{f.name}_of_another_tenant", [f"    foreign = create_{pe.snake}(client, {as_other('carol')}, TENANT_B)", f'    payload["{f.name}"] = foreign["id"]', "    response = client.post(url, json=payload, headers=headers)", "    assert response.status_code == 409, response.text  # composite foreign key (tenant_id, id)"])
        if ops["get_route"] and ops["create"]:
            test(f"test_get_{x}_returns_the_created_one_and_404_for_unknown", ["    created = client.post(url, json=payload, headers=headers).json()", f"    found = client.get({item}, headers=headers)", "    assert found.status_code == 200", '    assert found.json()["id"] == created["id"]', '    missing = client.get(f"{url}/{uuid.uuid4()}", headers=headers)', "    assert missing.status_code == 404", '    assert missing.headers["content-type"].startswith("application/problem+json")', '    assert missing.json()["status"] == 404'])
            test(f"test_get_{x}_with_a_malformed_id_is_422", ['    assert client.get(f"{url}/not-an-id", headers=headers).status_code == 422'], with_payload=False)
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
            test(f"test_{x}_routes_require_the_api_key", ["    assert client.post(url, json=payload).status_code == 401", '    assert client.post(url, json=payload, headers={"X-API-Key": "wrong"}).status_code == 401'])
        if oidc and ops["create"]:
            test(f"test_{x}_routes_require_a_valid_bearer_token", ["    assert client.post(url, json=payload).status_code == 401", '    assert client.post(url, json=payload, headers={"Authorization": "Bearer not-a-token"}).status_code == 401'])
        if bound and ops["create"]:
            body = [f"    assert client.post({other_tenant_url}, json=payload, headers=headers).status_code == 403"]
            if ops["list"]:
                body.append(f"    assert client.get({other_tenant_url}, headers=headers).status_code == 403")
            test(f"test_{x}_routes_refuse_another_tenant_for_the_same_principal", body, with_url=False)
            if ops["list"]:
                test(f"test_{e.plural}_of_one_tenant_are_invisible_to_another", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f"    other = client.get({other_tenant_url}, headers={as_other('carol')})", "    assert other.status_code == 200", "    assert other.json() == []"])
            if ops["get_route"] or ops["update"] or ops["delete"]:
                body = [f"    foreign = create_{x}(client, {as_other('carol')}, TENANT_B)", "    foreign_url = f\"{url}/{foreign['id']}\"  # a valid id of another tenant, presented under the caller's tenant"]
                if ops["get_route"]:
                    body.append("    assert client.get(foreign_url, headers=headers).status_code == 404")
                if ops["update"]:
                    f0 = next((f for f in e.fields if not f.references), e.fields[0])
                    body.append(f'    assert client.patch(foreign_url, json={{"{f0.name}": {_changed_sample(f0)}}}, headers=headers).status_code == 404')
                if ops["delete"]:
                    body.append("    assert client.delete(foreign_url, headers=headers).status_code == 404")
                if ops["get_route"]:
                    body.append(f"    still_there = client.get(f\"{{{other_tenant_url}}}/{{foreign['id']}}\", headers={as_other('carol')})")
                    body.append("    assert still_there.status_code == 200  # untouched")
                test(f"test_{x}_of_another_tenant_is_not_found_through_the_callers_tenant", body, with_payload=False)
        if authz and ops["create"]:
            # per-operation roles: 'limited' is authenticated without any role, so every restricted operation refuses it
            restricted = [op for op in ("create", "get", "list", "update", "delete") if spec.security["authorization"]["operations"].get(op)]
            ops_map = {"create": ops["create"], "get": ops["get_route"], "list": ops["list"], "update": ops["update"], "delete": ops["delete"]}
            for op in restricted:
                if not ops_map[op]:
                    continue
                limited = as_other("limited")
                if op == "create":
                    body = [f"    denied = client.post(url, json=payload, headers={limited})", "    assert denied.status_code == 403, denied.text", '    assert denied.headers["content-type"].startswith("application/problem+json")']
                    if ops["list"]:
                        body.append("    assert client.get(url, headers=headers).json() == []  # nothing was created")
                elif op == "list":
                    body = [f"    assert client.get(url, headers={limited}).status_code == 403"]
                elif op == "get":
                    body = ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.get({item}, headers={limited}).status_code == 403"]
                elif op == "update":
                    f0 = next((f for f in e.fields if not f.references), e.fields[0])
                    body = ["    created = client.post(url, json=payload, headers=headers).json()", f'    denied = client.patch({item}, json={{"{f0.name}": {_changed_sample(f0)}}}, headers={limited})', "    assert denied.status_code == 403, denied.text"]
                else:
                    body = ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.delete({item}, headers={limited}).status_code == 403"]
                    if ops["get_route"]:
                        body.append(f"    assert client.get({item}, headers=headers).status_code == 200  # untouched")
                test(f"test_{op}_{x}_needs_a_permitted_role", body)
        if e.owned and ops["create"]:
            test(f"test_{x}_records_the_creating_principal_as_owner_and_never_takes_it_from_the_client", ['    assert client.post(url, json={**payload, "owner_id": "mallory"}, headers=headers).status_code == 422', "    created = client.post(url, json=payload, headers=headers).json()", '    assert created["owner_id"] == "alice"'])
            bob = as_other("bob")
            if ops["get_route"] and _allowed(spec, bob_roles, "get"):
                expected = "200" if visibility == "tenant" else "404"
                test(f"test_another_member_{'reads' if visibility == 'tenant' else 'cannot_see'}_{x}_of_a_colleague", ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.get({item}, headers={bob}).status_code == {expected}  # visibility: {visibility}"])
            if ops["list"] and _allowed(spec, bob_roles, "list"):
                if visibility == "owner":
                    test(f"test_list_{e.plural}_is_owner_scoped", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f"    assert client.get(url, headers={bob}).json() == []", "    assert len(client.get(url, headers=headers).json()) == 1"])
                else:
                    test(f"test_list_{e.plural}_is_shared_within_the_tenant", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f"    assert len(client.get(url, headers={bob}).json()) == 1"])
            modify_status = "403" if visibility == "tenant" else "404"
            if ops["update"] and _allowed(spec, bob_roles, "update"):
                f0 = next((f for f in e.fields if not f.references), e.fields[0])
                body = ["    created = client.post(url, json=payload, headers=headers).json()", f'    denied = client.patch({item}, json={{"{f0.name}": {_changed_sample(f0)}}}, headers={bob})', f"    assert denied.status_code == {modify_status}, denied.text"]
                if ops["get_route"] and f0.type in ECHOED_TYPES:
                    body.append(f'    assert client.get({item}, headers=headers).json()["{f0.name}"] == created["{f0.name}"]  # unchanged')
                test(f"test_another_member_cannot_update_{x}_of_a_colleague", body)
            if ops["delete"] and _allowed(spec, bob_roles, "delete"):
                body = ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.delete({item}, headers={bob}).status_code == {modify_status}"]
                if ops["get_route"]:
                    body.append(f"    assert client.get({item}, headers=headers).status_code == 200  # still there")
                test(f"test_another_member_cannot_delete_{x}_of_a_colleague", body)
            if admin_roles:
                admin = as_other("admin")
                if ops["update"] and _allowed(spec, admin_roles, "update"):
                    f0 = next((f for f in e.fields if not f.references), e.fields[0])
                    test(f"test_an_admin_can_update_{x}_of_another_member", ["    created = client.post(url, json=payload, headers=headers).json()", f'    updated = client.patch({item}, json={{"{f0.name}": {_changed_sample(f0)}}}, headers={admin})', "    assert updated.status_code == 200, updated.text"])
                if ops["get_route"] and visibility == "owner" and _allowed(spec, admin_roles, "get"):
                    test(f"test_an_admin_sees_{x}_of_another_member", ["    created = client.post(url, json=payload, headers=headers).json()", f"    assert client.get({item}, headers={admin}).status_code == 200"])
        if ports and ops["create"]:
            p = ports[0]
            test(f"test_create_{x}_notifies_the_{p.name}_port", ["    assert client.post(url, json=payload, headers=headers).status_code == 201", f'    sent = app.state.ports["{p.name}"].sent', f'    assert sent and sent[-1]["event"] == "{x}.created"'], fixtures=("app, " + fixtures_default))
    lines += ["", "", "def test_health_is_public(client):", '    assert client.get("/health").json() == {"status": "ok"}']
    body = "\n".join(lines)
    conftest_names = [name for name in ("TENANT_B", "credentials") if name in body]
    if conftest_names:
        local.append(f"from tests.conftest import {', '.join(conftest_names)}")
    return "\n".join(_header(doc + ".", stdlib=["import uuid"], local=local) + lines) + "\n"


def _probe_target(spec: Spec) -> Tuple[ModuleSpec, EntitySpec, str]:
    """The route the authentication tests call: the first entity with a list operation (a plain GET), else the first one."""
    for m in spec.modules:
        for e in m.entities:
            if "list" in e.operations:
                return m, e, "list"
    m = spec.modules[0]
    return m, m.entities[0], "create"


def _jwks_tests(spec: Spec) -> str:
    """Generated tests of the JWKS client against a local HTTPS provider (a CA created per session, trusted only through
    oidc_ca_bundle): fetch, signature, rotation, unknown kid, same-kid replacement, malformed and invalid documents,
    timeout, HTTP error, TLS failure, outage and recovery, size cap, cache and refresh limits, concurrency."""
    o = spec.oidc or {}
    tenant = _tenant(spec)
    m, e, op = _probe_target(spec)
    x = e.snake
    url = f'f"/api/v1/tenants/{{TENANT_A}}/{e.plural}"' if tenant else f'"/api/v1/{e.plural}"'
    conftest_names = sorted({"ALGORITHM", "AUDIENCE", "ISSUER", "ROLES_CLAIM", "SUBJECT_CLAIM", "TENANT_CLAIM", "new_private_key", "public_jwk"} | ({"TENANT_A"} if tenant else set()))
    local = [
        f"from {spec.package}.core.config import Settings",
        f"from {spec.package}.core.security import MAX_JWKS_BYTES, MIN_REFRESH_INTERVAL_S, JwksSource, JwksUnavailable",
        f"from {spec.package}.main import create_app",
        f"from tests.conftest import {', '.join(conftest_names)}",
    ]
    if op == "create":
        local.append(f"from tests.test_{m.name} import payload_for_{x}")
    ok_status = "200" if op == "list" else "201"
    lines = _header(
        "The JWKS client against a local HTTPS identity provider: a certificate authority created for the session signs the "
        "provider certificate and is trusted only through oidc_ca_bundle (certificate and hostname verification stay on); "
        "the provider serves whatever document the test asks for, counts its hits, and rotates keys. Every failure of the "
        "provider is 503 without a WWW-Authenticate challenge; every failure of a token is 401 with one.",
        stdlib=["import base64", "import datetime", "import ipaddress", "import json", "import ssl", "import threading", "import time", "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer", "from pathlib import Path"],
        third_party=["import jwt", "import pytest", "from cryptography import x509", "from cryptography.hazmat.primitives import hashes, serialization", "from cryptography.hazmat.primitives.asymmetric import rsa", "from cryptography.x509.oid import NameOID", "from fastapi.testclient import TestClient"],
        local=local,
    )
    lines += [
        f"URL = {url}",
        'TENANT_VALUE = str(TENANT_A)' if tenant else 'TENANT_VALUE = None',
        "",
        "",
        "def _certificate(subject: str, key, issuer_name=None, issuer_key=None, *, ca: bool, san: bool = False):",
        '    """A certificate signed by issuer_key (self-signed when None): the session CA, or the provider leaf with SANs."""',
        "    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])",
        "    now = datetime.datetime.now(datetime.UTC)",
        "    builder = (",
        "        x509.CertificateBuilder()",
        "        .subject_name(name)",
        "        .issuer_name(issuer_name or name)",
        "        .public_key(key.public_key())",
        "        .serial_number(x509.random_serial_number())",
        "        .not_valid_before(now - datetime.timedelta(days=1))",
        "        .not_valid_after(now + datetime.timedelta(days=1))",
        "        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)",
        "        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)",
        "        .add_extension(",
        "            x509.KeyUsage(",
        "                digital_signature=True,",
        "                content_commitment=False,",
        "                key_encipherment=not ca,",
        "                data_encipherment=False,",
        "                key_agreement=False,",
        "                key_cert_sign=ca,",
        "                crl_sign=ca,",
        "                encipher_only=False,",
        "                decipher_only=False,",
        "            ),",
        "            critical=True,",
        "        )",
        "    )",
        "    if issuer_key is not None:  # strict verifiers want the authority key identifier and server-auth usage on a leaf",
        "        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key())",
        "        builder = builder.add_extension(aki, critical=False)",
        "        builder = builder.add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)",
        "    if san:",
        '        names = [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]',
        "        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)",
        "    return builder.sign(issuer_key or key, hashes.SHA256())",
        "",
        "",
        "def _pem(obj) -> bytes:",
        "    if hasattr(obj, 'public_bytes'):",
        "        return obj.public_bytes(serialization.Encoding.PEM)",
        "    fmt, enc = serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()",
        "    return obj.private_bytes(serialization.Encoding.PEM, fmt, enc)",
        "",
        "",
        "class Provider:",
        '    """The fake identity provider: keys by kid, a response mode, a hit counter, an HTTPS server on 127.0.0.1."""',
        "",
        "    def __init__(self, directory: Path) -> None:",
        '        self.keys = {"k1": new_private_key()}',
        '        self.mode = "ok"',
        "        self.delay = 0.0",
        "        self.hits = 0",
        "        self.lock = threading.Lock()",
        "        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)",
        '        ca_cert = _certificate("test provider CA", ca_key, ca=True)',
        "        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)",
        '        leaf_cert = _certificate("127.0.0.1", leaf_key, ca_cert.subject, ca_key, ca=False, san=True)',
        '        self.ca_path = directory / "provider-ca.pem"',
        "        self.ca_path.write_bytes(_pem(ca_cert))",
        '        cert_path, key_path = directory / "provider.pem", directory / "provider-key.pem"',
        "        cert_path.write_bytes(_pem(leaf_cert))",
        "        key_path.write_bytes(_pem(leaf_key))",
        "        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)",
        '        self.other_ca_path = directory / "other-ca.pem"',
        '        self.other_ca_path.write_bytes(_pem(_certificate("another CA", other_key, ca=True)))',
        "        provider = self",
        "",
        "        class Handler(BaseHTTPRequestHandler):",
        "            def log_message(self, *_args) -> None:",
        "                pass",
        "",
        "            def do_GET(self) -> None:",
        "                provider.respond(self)",
        "",
        '        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)',
        "        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)",
        "        context.load_cert_chain(str(cert_path), str(key_path))",
        "        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)",
        "        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)",
        "        self.thread.start()",
        '        self.url = f"https://127.0.0.1:{self.server.server_address[1]}/jwks.json"',
        "",
        "    def close(self) -> None:",
        "        self.server.shutdown()",
        "        self.server.server_close()",
        "",
        "    def document(self) -> bytes:",
        '        return json.dumps({"keys": [public_jwk(key, kid) for kid, key in self.keys.items()]}).encode()',
        "",
        "    def respond(self, handler: BaseHTTPRequestHandler) -> None:",
        "        with self.lock:",
        "            self.hits += 1",
        "            mode, delay = self.mode, self.delay",
        "        if delay:",
        "            time.sleep(delay)",
        "        bodies = {",
        '            "ok": (200, self.document()),',
        '            "malformed": (200, b"{not json"),',
        '            "invalid": (200, json.dumps({"keys": [{"kty": "oct", "k": "AAAA"}]}).encode()),',
        '            "list": (200, b"[1, 2, 3]"),',
        '            "error": (500, b"boom"),',
        '            "missing": (404, b"not here"),',
        '            "huge": (200, b"{\\"keys\\": [], \\"pad\\": \\"" + b"x" * (MAX_JWKS_BYTES + 1) + b"\\"}"),',
        "        }",
        "        status, body = bodies[mode]",
        "        handler.send_response(status)",
        '        handler.send_header("Content-Type", "application/json")',
        '        handler.send_header("Content-Length", str(len(body)))',
        "        handler.end_headers()",
        "        handler.wfile.write(body)",
        "",
        "",
        "class Clock:",
        "    def __init__(self) -> None:",
        "        self.now = 1000.0",
        "",
        "    def __call__(self) -> float:",
        "        return self.now",
        "",
        "    def advance(self, seconds: float) -> None:",
        "        self.now += seconds",
        "",
        "",
        '@pytest.fixture(scope="module")',
        "def provider(tmp_path_factory):",
        '    p = Provider(tmp_path_factory.mktemp("provider"))',
        "    yield p",
        "    p.close()",
        "",
        "",
        "@pytest.fixture",
        "def reset(provider):",
        '    provider.keys = {"k1": new_private_key()}',
        '    provider.mode = "ok"',
        "    provider.delay = 0.0",
        "    provider.hits = 0",
        "    return provider",
        "",
        "",
        "def make_client(provider, clock, **overrides):",
        '    """An application whose JWKS source reads the fake provider over HTTPS through the session CA and a fake clock."""',
        '    kwargs = {"app_name": "jwks", "oidc_jwks_url": provider.url, "oidc_ca_bundle": str(provider.ca_path)}',
        '    kwargs["oidc_fetch_timeout_s"] = 0.5',
        "    kwargs.update(overrides)",
        "    app = create_app(Settings(**kwargs))",
        "    app.state.jwks = JwksSource(app.state.settings, clock=clock)",
        "    client = TestClient(app)",
        "    client.__enter__()",
        "    return client",
        "",
        "",
        "def token(provider, kid: str, key=None, **overrides) -> str:",
        "    now = int(time.time())",
        "    claims = {",
        '        "iss": ISSUER,',
        '        "aud": AUDIENCE,',
        '        SUBJECT_CLAIM: "alice",',
        '        "iat": now - 5,',
        '        "exp": now + 300,',
        '        ROLES_CLAIM: ["admin", "editor", "viewer", "member", "moderator", "writer", "dispatcher"],',
        "    }",
        "    if TENANT_VALUE is not None:",
        "        claims[TENANT_CLAIM] = TENANT_VALUE",
        "    claims.update(overrides)",
        '    return jwt.encode(claims, key or provider.keys[kid], algorithm=ALGORITHM, headers={"kid": kid})',
        "",
        "",
        "def call(client, bearer: str):",
        '    headers = {"Authorization": f"Bearer {bearer}"}',
        *(["    return client.get(URL, headers=headers)"] if op == "list" else [f"    return client.post(URL, json=payload_for_{x}(client, headers{', TENANT_A' if tenant else ''}), headers=headers)"]),
        "",
        "",
        "def test_keys_are_fetched_over_https_and_a_valid_signature_is_accepted(reset):",
        "    client = make_client(reset, Clock())",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        "    assert reset.hits == 1",
        "",
        "",
        "def test_the_cache_serves_repeated_requests_without_refetching(reset):",
        "    client = make_client(reset, Clock())",
        "    for _ in range(5):",
        f'        assert call(client, token(reset, "k1")).status_code == {ok_status}',
        "    assert reset.hits == 1",
        "",
        "",
        "def test_a_rotated_key_is_fetched_once_the_refresh_interval_has_passed(reset):",
        "    clock = Clock()",
        "    client = make_client(reset, clock)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        '    reset.keys["k2"] = new_private_key()',
        '    assert call(client, token(reset, "k2")).status_code == 401  # just fetched: the unknown id is not fetched again yet',
        "    clock.advance(MIN_REFRESH_INTERVAL_S)",
        f'    assert call(client, token(reset, "k2")).status_code == {ok_status}',
        "    assert reset.hits == 2",
        "",
        "",
        "def test_unknown_key_ids_cannot_force_a_fetch_per_request(reset):",
        "    clock = Clock()",
        "    client = make_client(reset, clock)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        "    clock.advance(MIN_REFRESH_INTERVAL_S)",
        "    for i in range(10):",
        '        forged = token(reset, "k1").split(".")',
        '        assert call(client, _with_kid(forged, f"kid-{i}")).status_code == 401',
        "    assert reset.hits == 2  # one refresh for the first unknown id, none for the other nine",
        "",
        "",
        "def _with_kid(parts: list, kid: str) -> str:",
        '    """The same token with another kid in its header (the signature no longer matters: the key is unknown)."""',
        '    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))',
        '    header["kid"] = kid',
        '    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")',
        '    return ".".join([encoded, parts[1], parts[2]])',
        "",
        "",
        "def test_a_key_replaced_under_the_same_id_is_rejected_until_the_cache_expires(reset):",
        "    clock = Clock()",
        "    client = make_client(reset, clock, oidc_jwks_cache_s=120)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        '    reset.keys["k1"] = new_private_key()',
        '    assert call(client, token(reset, "k1")).status_code == 401  # the cached key no longer matches',
        "    clock.advance(121)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        "    assert reset.hits == 2",
        "",
        "",
        "def test_a_removed_key_is_rejected_after_the_cache_expires(reset):",
        "    clock = Clock()",
        "    client = make_client(reset, clock, oidc_jwks_cache_s=120)",
        '    old = token(reset, "k1")',
        f"    assert call(client, old).status_code == {ok_status}",
        '    reset.keys = {"k2": new_private_key()}',
        "    clock.advance(121)",
        "    assert call(client, old).status_code == 401",
        f'    assert call(client, token(reset, "k2")).status_code == {ok_status}',
        "",
        "",
        '@pytest.mark.parametrize("mode", ["malformed", "invalid", "list", "error", "missing", "huge"])',
        "def test_an_unusable_or_failing_provider_is_503_not_401(reset, mode):",
        "    reset.mode = mode",
        "    client = make_client(reset, Clock())",
        '    response = call(client, token(reset, "k1"))',
        "    assert response.status_code == 503, (mode, response.text)",
        '    assert "www-authenticate" not in response.headers',
        '    assert response.json()["detail"] == "identity provider keys are unavailable"',
        "",
        "",
        "def test_a_slow_provider_times_out_as_503(reset):",
        "    reset.delay = 2.0",
        "    client = make_client(reset, Clock())",
        "    started = time.monotonic()",
        '    assert call(client, token(reset, "k1")).status_code == 503',
        "    assert time.monotonic() - started < 1.9  # the 0.5 s fetch timeout, not the 2 s delay",
        "",
        "",
        "def test_tls_verification_is_never_disabled(reset):",
        '    assert call(make_client(reset, Clock(), oidc_ca_bundle=""), token(reset, "k1")).status_code == 503  # system store',
        "    other = make_client(reset, Clock(), oidc_ca_bundle=str(reset.other_ca_path))",
        '    assert call(other, token(reset, "k1")).status_code == 503  # a CA that did not sign the provider certificate',
        "    assert reset.hits == 0  # the handshake failed before any request reached the provider",
        "",
        "",
        "def test_outage_without_a_cache_is_503_and_the_provider_is_retried_after_the_interval(reset):",
        '    reset.mode = "error"',
        "    clock = Clock()",
        "    client = make_client(reset, clock)",
        '    assert call(client, token(reset, "k1")).status_code == 503',
        '    assert call(client, token(reset, "k1")).status_code == 503',
        "    assert reset.hits == 1  # the second failure is answered without hammering the provider",
        '    reset.mode = "ok"',
        "    clock.advance(MIN_REFRESH_INTERVAL_S)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        "",
        "",
        "def test_a_warm_cache_survives_an_outage_for_the_stale_window_only(reset):",
        "    clock = Clock()",
        "    client = make_client(reset, clock, oidc_jwks_cache_s=60, oidc_jwks_stale_max_s=600)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}',
        '    reset.mode = "error"',
        "    clock.advance(61)",
        f'    assert call(client, token(reset, "k1")).status_code == {ok_status}  # stale keys, provider failing',
        "    clock.advance(600)",
        '    assert call(client, token(reset, "k1")).status_code == 503  # beyond the stale window nothing is served',
        "",
        "",
        "def test_concurrent_cold_starts_fetch_once(reset):",
        "    source = JwksSource(make_client(reset, Clock()).app.state.settings, clock=Clock())",
        "    results: list = []",
        "",
        "    def worker() -> None:",
        "        try:",
        '            results.append(source.key_for("k1") is not None)',
        "        except JwksUnavailable as exc:",
        "            results.append(exc)",
        "",
        "    threads = [threading.Thread(target=worker) for _ in range(8)]",
        "    for t in threads:",
        "        t.start()",
        "    for t in threads:",
        "        t.join()",
        "    assert results == [True] * 8",
        "    assert reset.hits == 1",
    ]
    return "\n".join(lines) + "\n"


def _auth_tests(spec: Spec) -> str:
    o = spec.oidc or {}
    tenant = _tenant(spec)
    claim_binding = _claim_binding(spec)
    m, e, op = _probe_target(spec)
    x = e.snake
    url = f'f"/api/v1/tenants/{{TENANT_A}}/{e.plural}"' if tenant else f'"/api/v1/{e.plural}"'
    local = [
        f"from {spec.package}.core.config import Settings",
        f"from {spec.package}.main import create_app",
        "from tests.conftest import " + ", ".join(sorted({"KID", "PRIVATE_KEY", "ISSUER", "AUDIENCE", "SUBJECT_CLAIM", "ROLES_CLAIM", "jwks_json", "mint"} | ({"TENANT_A"} if tenant else set()) | ({"TENANT_CLAIM"} if claim_binding else set()))),
    ]
    if op == "create":
        local.append(f"from tests.test_{m.name} import payload_for_{x}")
    algorithm = o["algorithms"][0]
    if algorithm.startswith(("RS", "PS")):
        other_key = "rsa.generate_private_key(public_exponent=65537, key_size=2048)"
        key_import = "from cryptography.hazmat.primitives.asymmetric import rsa"
    elif algorithm.startswith("ES"):
        curve = {"ES256": "SECP256R1", "ES384": "SECP384R1", "ES512": "SECP521R1"}[algorithm]
        other_key = f"ec.generate_private_key(ec.{curve}())"
        key_import = "from cryptography.hazmat.primitives.asymmetric import ec"
    else:
        other_key = "ed25519.Ed25519PrivateKey.generate()"
        key_import = "from cryptography.hazmat.primitives.asymmetric import ed25519"
    lines = _header(
        f"Authentication tests: what the OIDC validation refuses (issuer {o['issuer']}, audience {o['audience']}, algorithms {'/'.join(o['algorithms'])}) "
        "and how failures are told apart: 401 for a bad token, 403 for another tenant, 503 when the provider cannot be reached.",
        stdlib=["import base64", "import hashlib", "import hmac", "import json", "import time"],
        third_party=["import jwt", "import pytest", key_import, "from cryptography.hazmat.primitives import serialization", "from fastapi.testclient import TestClient"],
        local=local,
    )
    lines += [
        f"URL = {url}",
        "",
        "",
        "def call(client, headers: dict[str, str]):",
        *(["    return client.get(URL, headers=headers)"] if op == "list" else [f"    return client.post(URL, json=payload_for_{x}(client, headers{', TENANT_A' if tenant else ''}), headers=headers)"]),
        "",
        "",
        "def bearer(token: str) -> dict[str, str]:",
        '    return {"Authorization": f"Bearer {token}"}',
        "",
        "",
        "def claims_of(token: str) -> dict:",
        '    payload = token.split(".")[1]',
        '    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))',
        "",
        "",
        "def b64(data: bytes) -> str:",
        '    return base64.urlsafe_b64encode(data).decode().rstrip("=")',
        "",
        "",
        "def test_a_valid_token_is_accepted(client, headers):",
        f"    assert call(client, headers).status_code == {'200' if op == 'list' else '201'}",
        "",
        "",
        "def test_missing_or_non_bearer_authorization_is_401_with_a_challenge(client):",
        "    response = call(client, {})",
        "    assert response.status_code == 401",
        '    assert response.headers["www-authenticate"] == "Bearer"',
        '    assert call(client, {"Authorization": "Basic YWxpY2U6c2VjcmV0"}).status_code == 401',
        "",
        "",
        "def test_a_token_with_altered_claims_keeps_its_signature_but_is_rejected(client):",
        '    header, payload, signature = mint("bob").split(".")',
        '    claims = claims_of(f"{header}.{payload}.{signature}")',
        '    claims[SUBJECT_CLAIM] = "alice"',
        '    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")',
        '    assert call(client, bearer(f"{header}.{forged}.{signature}")).status_code == 401',
        "",
        "",
        "def test_an_expired_token_is_rejected_beyond_the_leeway(client):",
        '    assert call(client, bearer(mint(expires_in=-120))).status_code == 401',
        "",
        "",
        "def test_a_token_not_yet_valid_is_rejected(client):",
        '    assert call(client, bearer(mint(not_before=600))).status_code == 401',
        "",
        "",
        "def test_wrong_audience_or_issuer_is_rejected(client):",
        '    assert call(client, bearer(mint(audience="another-api"))).status_code == 401',
        '    assert call(client, bearer(mint(issuer="https://evil.example.com/"))).status_code == 401',
        "",
        "",
        "def test_a_symmetric_signature_with_the_public_key_is_rejected(client):",
        '    """Algorithm confusion: HS256 with the provider public key as the secret must never verify."""',
        "    encoding, fmt = serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo",
        "    public_pem = PRIVATE_KEY.public_key().public_bytes(encoding, fmt)",
        "    now = int(time.time())",
        '    claims = {"iss": ISSUER, "aud": AUDIENCE, SUBJECT_CLAIM: "alice", "iat": now, "exp": now + 300}',
        "    # PyJWT itself refuses to sign HS256 with a PEM, so the forged token is assembled by hand",
        '    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())',
        '    signing_input = f"{header}.{b64(json.dumps(claims).encode())}"',
        "    signature = b64(hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest())",
        '    assert call(client, bearer(f"{signing_input}.{signature}")).status_code == 401',
        "",
        "",
        "def test_an_unsigned_token_is_rejected(client):",
        "    now = int(time.time())",
        '    claims = {"iss": ISSUER, "aud": AUDIENCE, SUBJECT_CLAIM: "alice", "iat": now, "exp": now + 300}',
        '    token = jwt.encode(claims, None, algorithm="none", headers={"kid": KID})',
        "    assert call(client, bearer(token)).status_code == 401",
        "",
        "",
        "def test_an_unknown_key_id_or_a_missing_key_id_is_rejected(client):",
        '    assert call(client, bearer(mint(kid="rotated-away"))).status_code == 401',
        "    assert call(client, bearer(mint(kid=None))).status_code == 401",
        "",
        "",
        "def test_a_token_signed_by_another_key_with_the_same_key_id_is_rejected(client):",
        f"    other = {other_key}",
        "    assert call(client, bearer(mint(key=other))).status_code == 401",
        "",
        "",
        "def test_malformed_tokens_are_rejected(client):",
        '    for token in ("not-a-jwt", "a.b", "a.b.c", ""):',
        "        assert call(client, bearer(token)).status_code == 401, token",
        "",
        "",
        "def test_missing_required_claims_are_rejected(client):",
        '    for claim in ("exp", "iat", "iss", "aud", SUBJECT_CLAIM):',
        "        assert call(client, bearer(mint(drop=(claim,)))).status_code == 401, claim",
        "",
        "",
        "def test_a_malformed_roles_claim_is_rejected(client):",
        "    assert call(client, bearer(mint(overrides={ROLES_CLAIM: 5}))).status_code == 401",
        "    assert call(client, bearer(mint(overrides={ROLES_CLAIM: [1, 2]}))).status_code == 401",
        "",
        "",
        "def test_roles_may_come_as_a_space_separated_string(client):",
        '    roles = " ".join(claims_of(mint())[ROLES_CLAIM])',
        f"    assert call(client, bearer(mint(overrides={{ROLES_CLAIM: roles}}))).status_code == {'200' if op == 'list' else '201'}",
    ]
    if claim_binding:
        lines += [
            "",
            "",
            "def test_a_token_without_a_usable_tenant_claim_is_401(client):",
            "    assert call(client, bearer(mint(drop=(TENANT_CLAIM,)))).status_code == 401",
            '    assert call(client, bearer(mint(overrides={TENANT_CLAIM: "not-a-uuid"}))).status_code == 401',
            "",
            "",
            "def test_a_member_of_another_tenant_is_403_not_401(client):",
            '    response = call(client, bearer(mint("carol")))',
            "    assert response.status_code == 403",
            '    assert response.headers["content-type"].startswith("application/json")',
        ]
    lines += [
        "",
        "",
        "def test_a_static_jwks_is_refused_outside_the_test_environment():",
        '    with pytest.raises(RuntimeError, match="static JWKS"):',
        '        create_app(Settings(app_name="x", environment="local", oidc_jwks_json=jwks_json()))',
        "",
        "",
        "def test_a_non_https_jwks_url_is_refused():",
        '    with pytest.raises(RuntimeError, match="https"):',
        '        create_app(Settings(app_name="x", oidc_jwks_url="http://issuer.example.com/jwks.json"))',
        "",
        "",
        "def test_an_unreachable_provider_is_503_not_401():",
        '    app = create_app(Settings(app_name="x", oidc_jwks_url="https://127.0.0.1:9/jwks.json"))',
        "    with TestClient(app) as client:",
        '        response = call(client, bearer(mint()))',
        "    assert response.status_code == 503, response.text",
    ]
    return "\n".join(lines) + "\n"


def _rls_tests(spec: Spec) -> str:
    tenant = _tenant(spec)
    rls = spec.security["multi_tenant"]["rls"]
    ordered = _ordered_entities(spec)
    m, e = ordered[0]  # referenced tables come first, so this entity has no parent to create
    x, up = e.snake, e.snake.upper()
    tables = sorted(ent.plural for _mm, ent in ordered)
    # parameters travel as text and are cast in SQL, so asyncpg never infers a non-text type for a sample value
    casts = {"uuid": "uuid", "datetime": "timestamptz", "date": "date", "decimal": "numeric"}
    columns = ["id", tenant] + (["owner_id"] if e.owned else []) + [f.name for f in e.fields]
    values = ["gen_random_uuid()", "CAST(:tenant AS text)::uuid"] + (["'alice'"] if e.owned else []) + [f"CAST(:{f.name} AS text)::{casts[f.type]}" if f.type in casts else f":{f.name}" for f in e.fields]
    insert_sql = f"INSERT INTO {e.plural} ({', '.join(columns)}) VALUES ({', '.join(values)})"
    if _registry(spec):
        unreachable_settings = 'Settings(app_name="x", api_keys={"key-alice": grant}, database_url=url)'
        unreachable_headers = '{"X-API-Key": "key-alice"}'
    else:
        unreachable_settings = 'Settings(app_name="x", environment="test", oidc_jwks_json=jwks_json(), database_url=url)'
        unreachable_headers = 'credentials("alice")'
    conftest_names = ["TENANT_A", "TENANT_B", "credentials", "run"] + ([] if _registry(spec) else ["jwks_json"])
    lines = _header(
        "Row-level security, demonstrated through the application role and raw SQL (no application filter involved): policies "
        "exist and are forced, no context means no rows, the context is bound to the transaction and never survives commit, "
        "rollback or the return of a pooled connection, another tenant's rows cannot be read, written, updated or deleted, "
        "the application role cannot lift the protection, and concurrent requests of two tenants never see each other.",
        stdlib=["import socket", "import uuid"],
        third_party=["import anyio", "import httpx", "import pytest", "from fastapi.testclient import TestClient", "from sqlalchemy import text", "from sqlalchemy.exc import DBAPIError", "from sqlalchemy.ext.asyncio import create_async_engine"],
        local=[f"from {spec.package}.core.config import Settings", f"from {spec.package}.main import create_app", f"from tests.conftest import {', '.join(sorted(conftest_names))}", f"from tests.test_{m.name} import PAYLOAD_{up}, create_{x}"],
    )
    lines += [
        f'TABLE = "{e.plural}"',
        *_call("TABLES = [", [json.dumps(t) for t in tables], "]"),
        f'CONTEXT_KEY = "{rls["context_key"]}"',
        f'TENANT_COLUMN = "{tenant}"',
        "INSERT_ROW = (",
        *[f"    {json.dumps(chunk + ' ')}" for chunk in textwrap.wrap(insert_sql, 100)],
        ")",
        "SET_CONTEXT = \"SELECT set_config(:key, :tenant, true)\"",
        "READ_CONTEXT = \"SELECT current_setting(:key, true)\"",
        "",
        "",
        "def url_for(tenant: uuid.UUID) -> str:",
        f'    return f"/api/v1/tenants/{{tenant}}/{e.plural}"',
        "",
        "",
        "def row_of(tenant: uuid.UUID) -> dict:",
        f'    return {{"tenant": str(tenant), **PAYLOAD_{up}}}',
        "",
        "",
        "async def sql(url: str, statement: str, params: dict | None = None, *, tenant: object = None, commit: bool = False):",
        '    """One statement on a fresh engine; with ``tenant`` the context is set first, in the same transaction."""',
        "    engine = create_async_engine(url)",
        "    try:",
        "        async with engine.connect() as connection:",
        "            if tenant is not None:",
        '                await connection.execute(text(SET_CONTEXT), {"key": CONTEXT_KEY, "tenant": str(tenant)})',
        "            result = await connection.execute(text(statement), params or {})",
        "            rows = result.all() if result.returns_rows else result.rowcount",
        "            if commit:",
        "                await connection.commit()",
        "            else:",
        "                await connection.rollback()",
        "            return rows",
        "    finally:",
        "        await engine.dispose()",
        "",
        "",
        "def seed(client) -> tuple[dict, dict]:",
        '    """One row of tenant A (alice) and one of tenant B (carol), created through the API."""',
        f'    of_a = create_{x}(client, credentials("alice"), TENANT_A)',
        f'    of_b = create_{x}(client, credentials("carol"), TENANT_B)',
        "    return of_a, of_b",
        "",
        "",
        "def test_every_tenant_table_has_rls_enabled_forced_and_a_policy(migrated):",
        '    query = "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables) ORDER BY 1"',
        '    rows = run(sql(migrated, query, {"tables": TABLES}))',
        "    assert [r[0] for r in rows] == TABLES",
        "    assert all(r[1] and r[2] for r in rows), rows",
        '    query = "SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE tablename = ANY(:tables)"',
        '    policies = run(sql(migrated, query, {"tables": TABLES}))',
        "    assert sorted(p[0] for p in policies) == TABLES",
        "    for _table, name, qual, with_check in policies:",
        '        assert name.endswith("_tenant_isolation")',
        "        assert CONTEXT_KEY in qual and CONTEXT_KEY in with_check",
        "",
        "",
        "def test_the_application_role_is_restricted(database_url):",
        '    query = "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"',
        "    row = run(sql(database_url, query))[0]",
        "    assert tuple(row) == (False, False, False, False), row",
        "",
        "",
        "def test_without_tenant_context_no_row_is_visible(client, migrated, database_url):",
        "    seed(client)",
        '    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == 2  # the owner sees both',
        '    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}"))[0][0] == 0',
        '    assert run(sql(database_url, f"SELECT * FROM {TABLE}")) == []',
        "",
        "",
        "def test_without_tenant_context_an_insert_is_rejected(database_url):",
        '    with pytest.raises(DBAPIError, match="row-level security"):',
        "        run(sql(database_url, INSERT_ROW, row_of(TENANT_A), commit=True))",
        "",
        "",
        "def test_with_tenant_context_only_that_tenants_rows_are_visible(client, database_url):",
        "    seed(client)",
        '    rows = run(sql(database_url, f"SELECT {TENANT_COLUMN} FROM {TABLE}", tenant=TENANT_A))',
        "    assert [str(r[0]) for r in rows] == [str(TENANT_A)]",
        '    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_B))[0][0] == 1',
        "",
        "",
        "def test_with_tenant_context_a_row_of_another_tenant_cannot_be_written(client, database_url):",
        "    _created, other = seed(client)",
        '    with pytest.raises(DBAPIError, match="row-level security"):',
        "        run(sql(database_url, INSERT_ROW, row_of(TENANT_B), tenant=TENANT_A, commit=True))",
        '    with pytest.raises(DBAPIError, match="row-level security"):',
        '        move = f"UPDATE {TABLE} SET {TENANT_COLUMN} = CAST(:b AS text)::uuid"',
        '        move += f" WHERE {TENANT_COLUMN} = CAST(:a AS text)::uuid"',
        '        tenants = {"a": str(TENANT_A), "b": str(TENANT_B)}',
        "        run(sql(database_url, move, tenants, tenant=TENANT_A, commit=True))",
        '    touch = f"UPDATE {TABLE} SET created_at = now() WHERE id = CAST(:id AS text)::uuid"',
        '    assert run(sql(database_url, touch, {"id": other["id"]}, tenant=TENANT_A, commit=True)) == 0',
        '    drop = f"DELETE FROM {TABLE} WHERE id = CAST(:id AS text)::uuid"',
        '    assert run(sql(database_url, drop, {"id": other["id"]}, tenant=TENANT_A, commit=True)) == 0',
        '    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_B))[0][0] == 1  # untouched',
        "",
        "",
        "def test_tenant_context_does_not_survive_commit_rollback_or_the_pool(database_url):",
        "    async def scenario() -> list:",
        "        engine = create_async_engine(database_url, pool_size=1, max_overflow=0)",
        "        seen = []",
        "        try:",
        '            for end in ("commit", "rollback"):',
        "                async with engine.connect() as connection:",
        '                    await connection.execute(text(SET_CONTEXT), {"key": CONTEXT_KEY, "tenant": str(TENANT_A)})',
        '                    inside = (await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar()',
        "                    assert inside == str(TENANT_A)",
        '                    await (connection.commit() if end == "commit" else connection.rollback())',
        '                    seen.append((await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar())',
        "                async with engine.connect() as connection:  # the same pooled connection, next checkout",
        '                    seen.append((await connection.execute(text(READ_CONTEXT), {"key": CONTEXT_KEY})).scalar())',
        "        finally:",
        "            await engine.dispose()",
        "        return seen",
        "",
        "    seen = run(scenario())",
        "    assert len(seen) == 4",
        '    assert all(value in (None, "") for value in seen), seen',
        "",
        "",
        "def test_rollback_leaves_no_row(client, migrated, database_url):",
        "    seed(client)",
        '    before = run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0]',
        "    assert run(sql(database_url, INSERT_ROW, row_of(TENANT_A), tenant=TENANT_A, commit=False)) == 1  # then rolled back",
        '    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == before',
        "    assert run(sql(database_url, INSERT_ROW, row_of(TENANT_A), tenant=TENANT_A, commit=True)) == 1",
        '    assert run(sql(migrated, f"SELECT count(*) FROM {TABLE}"))[0][0] == before + 1',
        "",
        "",
        "def test_an_invalid_or_empty_tenant_context_fails_closed(client, database_url):",
        "    seed(client)",
        '    with pytest.raises(DBAPIError, match="invalid input syntax for type uuid"):',
        '        run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant="not-a-tenant"))',
        '    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=""))[0][0] == 0',
        "",
        "",
        "def test_the_application_role_cannot_lift_the_protection(database_url):",
        "    for statement, reason in (",
        '        (f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY", "must be owner"),',
        '        (f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY", "must be owner"),',
        '        (f"DROP POLICY {TABLE}_tenant_isolation ON {TABLE}", "must be owner"),',
        '        (f"TRUNCATE TABLE {TABLE}", "permission denied"),',
        '        ("ALTER ROLE current_user BYPASSRLS", "permission denied|must be superuser"),',
        "    ):",
        "        with pytest.raises(DBAPIError, match=reason):",
        "            run(sql(database_url, statement, commit=True))",
        "",
        "",
        "def test_the_context_is_a_setting_that_any_statement_run_as_the_application_role_can_change(client, database_url):",
        '    """The boundary of the mechanism: the policies read a transaction setting, and the application role may set it. A',
        "    statement executed as that role (application code, or an injection that reaches statement level) can switch tenant;",
        '    a bound parameter cannot. Row-level security protects against missing filters, not against arbitrary SQL."""',
        "    seed(client)",
        '    switch = f"SELECT set_config(:key, :other, true), (SELECT count(*) FROM {TABLE})"',
        '    rows = run(sql(database_url, switch, {"key": CONTEXT_KEY, "other": str(TENANT_B)}, tenant=TENANT_A))',
        "    assert rows[0][1] == 1  # after the switch the same transaction sees tenant B's row: the limit, demonstrated",
        '    injected = f"\'; SELECT set_config(\'{CONTEXT_KEY}\', \'{TENANT_B}\', true); --"',
        '    probe = f"SELECT count(*) FROM {TABLE} WHERE CAST(id AS text) = :value"',
        "    # a bound parameter is data, not a statement: nothing is switched and the context stays on tenant A",
        '    assert run(sql(database_url, probe, {"value": injected}, tenant=TENANT_A))[0][0] == 0',
        '    assert run(sql(database_url, f"SELECT count(*) FROM {TABLE}", tenant=TENANT_A))[0][0] == 1',
        "",
        "",
        "def test_the_application_role_has_row_privileges_only(database_url):",
        "    for statement, reason in (",
        '        ("SELECT rolpassword FROM pg_authid LIMIT 1", "permission denied"),',
        '        ("SELECT * FROM alembic_version", "permission denied"),',
        '        ("CREATE TABLE smuggled (id int)", "permission denied"),',
        '        (f"CREATE POLICY open_door ON {TABLE} USING (true)", "must be owner"),',
        '        (f"ALTER TABLE {TABLE} OWNER TO current_user", "must be owner|permission denied"),',
        "    ):",
        "        with pytest.raises(DBAPIError, match=reason):",
        "            run(sql(database_url, statement, commit=True))",
        "",
        "",
        "def test_concurrent_requests_of_two_tenants_never_see_each_other(app):",
        "    async def scenario() -> list[tuple[str, int, list]]:",
        "        results: list[tuple[str, int, list]] = []",
        "        async with app.router.lifespan_context(app):",
        '            transport = httpx.ASGITransport(app=app)',
        '            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:',
        '                for identity, tenant, count in (("alice", TENANT_A, 3), ("carol", TENANT_B, 2)):',
        "                    for _ in range(count):",
        f"                        response = await http.post(url_for(tenant), json=PAYLOAD_{up}, headers=credentials(identity))",
        "                        assert response.status_code == 201, response.text",
        "",
        "                async def one(identity: str, tenant: uuid.UUID) -> None:",
        "                    response = await http.get(url_for(tenant), headers=credentials(identity))",
        "                    results.append((str(tenant), response.status_code, response.json()))",
        "",
        "                async with anyio.create_task_group() as group:",
        "                    for i in range(24):",
        '                        identity, tenant = ("alice", TENANT_A) if i % 2 == 0 else ("carol", TENANT_B)',
        "                        group.start_soon(one, identity, tenant)",
        "        return results",
        "",
        "    results = run(scenario())",
        "    assert len(results) == 24",
        "    for tenant, status, rows in results:",
        "        assert status == 200",
        "        assert len(rows) == (3 if tenant == str(TENANT_A) else 2)",
        "        assert {row[TENANT_COLUMN] for row in rows} == {tenant}",
        "",
        "",
        "def test_an_unreachable_database_is_503_not_401_or_404():",
        "    with socket.socket() as probe:",
        '        probe.bind(("127.0.0.1", 0))',
        "        port = probe.getsockname()[1]",
        '    url = f"postgresql+asyncpg://nobody@127.0.0.1:{port}/none"',
        *(['    grant = {"tenant_id": TENANT_A, "subject": "alice", "roles": []}'] if _registry(spec) else []),
        f"    settings = {unreachable_settings}",
        "    with TestClient(create_app(settings)) as client:",
        f"        response = client.get(url_for(TENANT_A), headers={unreachable_headers})",
        "    assert response.status_code == 503, response.text",
        '    assert response.headers["content-type"].startswith("application/problem+json")',
    ]
    return "\n".join(lines) + "\n"


def _group(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[str, List[Tuple[str, str]]]]:
    out: Dict[str, List[Tuple[str, str]]] = {}
    for module, entity in pairs:
        out.setdefault(module, []).append((module, entity))
    return sorted(out.items())


def migration_file(spec: Spec, lineage: Optional[Lineage] = None) -> str:
    if lineage is None:
        return f"migrations/versions/{initial_revision(spec)}_initial_schema.py"
    new_tables = [e.plural for _m, e in _ordered_entities(spec) if e.plural not in set(lineage.tables)]
    changes = new_tables + [f"{t}_{f.name}" for t, f, _r in added_columns(spec, lineage)]
    slug = "add_" + "_".join(changes) if changes else "noop"
    return f"migrations/versions/{delta_revision(lineage, spec)}_{slug[:60]}.py"


def _migration_tests(spec: Spec, lineage: Optional[Lineage] = None) -> str:
    tenant = _tenant(spec)
    index = _entity_index(spec)
    has_delta = bool(lineage) and (any(e.plural not in set(lineage.tables) for _m, e in _ordered_entities(spec)) or bool(added_columns(spec, lineage)))
    revision = delta_revision(lineage, spec) if has_delta else (lineage.head if lineage else initial_revision(spec))
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


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def build(spec: Spec, rec: Recommendation, lineage: Optional[Lineage] = None) -> Blueprint:
    """``lineage`` (evolution): keep the existing migration chain and append one additive revision."""
    layout = Layout(spec, rec)
    pkg = spec.package
    pg = spec.persistence["kind"] == "postgresql"
    files: Dict[str, str] = {}
    notes: List[str] = []
    migrations: List[Dict[str, object]] = []
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
    files[f"src/{pkg}/core/errors.py"] = _errors(spec)
    if spec.security["authentication"] == "api_key" or _oidc(spec):
        files[f"src/{pkg}/core/security.py"] = _security(spec)
    if _authz(spec):
        files[f"src/{pkg}/core/authz.py"] = _authz_module(spec)
    if pg:
        files[f"src/{pkg}/core/db.py"] = _db(spec)
        files["alembic.ini"] = _alembic_ini(spec)
        files["migrations/env.py"] = _alembic_env(spec, layout)
        files["migrations/script.py.mako"] = _alembic_mako()
        all_tables = [e.plural for _m, e in _ordered_entities(spec)]
        if lineage is None:
            files[migration_file(spec)] = _migration(spec)
            migrations.append({"revision": initial_revision(spec), "down_revision": None, "file": migration_file(spec), "tables": all_tables, "row_level_security": _rls(spec)})
        else:
            new_tables = [t for t in all_tables if t not in set(lineage.tables)]
            new_columns = added_columns(spec, lineage)
            migrations.extend(lineage.revisions)
            if new_tables or new_columns:
                files[migration_file(spec, lineage)] = _migration(spec, lineage)
                migrations.append({"revision": delta_revision(lineage, spec), "down_revision": lineage.head, "file": migration_file(spec, lineage), "tables": new_tables, "columns": [f"{t}.{f.name}" for t, f, _r in new_columns], "row_level_security": _rls(spec)})
        files["tests/test_migrations.py"] = _migration_tests(spec, lineage)
        if _rls(spec):
            files["tests/test_rls.py"] = _rls_tests(spec)
    if _oidc(spec):
        files["tests/test_auth.py"] = _auth_tests(spec)
        files["tests/test_jwks.py"] = _jwks_tests(spec)
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
        notes.append(f"PostgreSQL persistence: Alembic environment and migration chain {[m['revision'] for m in migrations]} generated from the contract; tables are created only by `alembic upgrade head`; the generated API tests skip unless DATABASE_URL points at an empty disposable database")
    if rec.style == STYLE_HEXAGONAL:
        notes.append("ports and adapters generated for: " + ", ".join(f"{m}:{','.join(i.name for i in ports_by_module[m])}" for m in rec.ports_modules))
    if _registry(spec):
        notes.append("multi-tenant binding api_key_registry: tenant routes resolve the tenant from the API key registry (grants with tenant, subject and roles) and refuse other tenants with 403" + ("" if _rls(spec) else "; isolation is application scoping, not row-level security"))
    if _claim_binding(spec):
        notes.append("multi-tenant binding principal_claim: the tenant is the verified token claim; tenant routes refuse any other tenant with 403")
    if _oidc(spec):
        notes.append("OIDC bearer tokens validated in core/security.py (asymmetric algorithms only, JWKS cache with bounded refresh, issuer/audience/exp/nbf/iat); tests/test_auth.py holds the negative controls")
    if _authz(spec):
        notes.append(f"authorization model {spec.authz_model}: core/authz.py (per-operation roles in a route dependency, ownership in services and repository queries); the module tests cover other members, admins and other tenants")
    if _rls(spec):
        notes.append(f"row-level security: every tenant table is protected in its creating migration (ENABLE + FORCE, policy on {spec.security['multi_tenant']['rls']['context_key']}, grants to {spec.security['multi_tenant']['rls']['app_role']}); the application sets the tenant context per transaction; tests/test_rls.py proves it through the application role and needs {spec.package.upper()}_MIGRATIONS_DATABASE_URL besides DATABASE_URL")
    return Blueprint(style=rec.style, package=pkg, files=files, layout=layout_lines, notes=notes, migrations=migrations)


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

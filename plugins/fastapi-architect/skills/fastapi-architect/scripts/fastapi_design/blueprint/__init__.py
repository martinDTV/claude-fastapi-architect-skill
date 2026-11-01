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

Package layout (v2.1, increment 0; the split moved code verbatim, generated output is byte-identical):

* ``_base``: type tables, test constants, ``Lineage``, ``Blueprint``, ``Layout``;
* ``_helpers``: pure rendering helpers and contract predicates;
* ``scaffold``: pyproject, audit configuration, settings, errors, application factory, router, README;
* ``security``: API-key grants, OIDC/JWKS client, authorization policy module;
* ``persistence``: database module, Alembic environment, initial and additive migrations, revision ids;
* ``modules``: per-module schemas, models, repository, service, dependencies, router, ports, adapters;
* ``testing`` and ``security_tests``: the generated test suite;
* this module: ``build`` (the only entry point) and the public names.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional

from ..advisor import STYLE_HEXAGONAL, Recommendation
from ..spec import IntegrationSpec, Spec
from ._base import Blueprint, Layout, Lineage, PhaseState
from ._helpers import _authz, _claim_binding, _oidc, _ordered_entities, _registry, _rls
from .modules import _adapter, _dependencies, _models, _ports, _ports_for, _repository, _router, _schemas, _service
from .persistence import _alembic_env, _alembic_ini, _alembic_mako, _db, _migration, added_columns, delta_revision, initial_revision, migration_file
from .phased import (
    backfill_runner,
    backfill_specs,
    backfill_tests,
    contract_migration,
    contract_migration_file,
    contract_revision,
    index_migration,
    index_migration_file,
    index_name,
    index_revision,
    pending_columns,
    progress_table,
    renames_of,
)
from .scaffold import _api_router, _audit_config, _config, _env_example, _errors, _gitignore, _main, _pyproject, _readme
from .security import _authz_module, _security
from .security_tests import _auth_tests, _jwks_tests, _rls_tests
from .testing import _conftest, _migration_tests, _tests


def build(spec: Spec, rec: Recommendation, lineage: Optional[Lineage] = None, phases: Optional[PhaseState] = None) -> Blueprint:
    """``lineage`` (evolution): keep the existing migration chain and append the revisions of this change: one additive or
    expand revision, then a concurrent-index revision, then a contract revision, each only when needed (v2.1).
    ``phases``: the project's phased changes (pending columns stay nullable; the backfill runner and its definitions are
    generated; the migration tests expect the final head). Without both, the output is a fresh project."""
    layout = Layout(spec, rec)
    pkg = spec.package
    pg = spec.persistence["kind"] == "postgresql"
    pending = pending_columns(phases.pending) if phases is not None else frozenset()
    renames = renames_of(phases.pending) if phases is not None else {}
    indexed = frozenset((str(i["table"]), str(c)) for i in (phases.all_indexes if phases is not None else []) for c in i["columns"])
    has_phased = phases is not None and bool(phases.all_phased or phases.new_phased or phases.contracting)
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
        files["migrations/env.py"] = _alembic_env(spec, layout, phases)
        files["migrations/script.py.mako"] = _alembic_mako()
        all_tables = [e.plural for _m, e in _ordered_entities(spec)]
        head: Optional[str] = phases.head if phases is not None else None
        if lineage is None:
            files[migration_file(spec)] = _migration(spec)
            migrations.append({"revision": initial_revision(spec), "down_revision": None, "file": migration_file(spec), "tables": all_tables, "row_level_security": _rls(spec), "phase": "initial", "transactional": True, "reversible": True})
            if head is None:
                head = initial_revision(spec)
        else:
            new_tables = [t for t in all_tables if t not in set(lineage.tables)]
            new_columns = added_columns(spec, lineage)
            migrations.extend(lineage.revisions)
            head = lineage.head
            if new_tables or new_columns:
                expand = phases is not None and bool(phases.new_phased)
                progress = [progress_table(spec)] if expand and progress_table(spec) not in set(lineage.tables) else []
                files[migration_file(spec, lineage, phases)] = _migration(spec, lineage, phases)
                migrations.append({"revision": delta_revision(lineage, spec), "down_revision": head, "file": migration_file(spec, lineage, phases), "tables": new_tables + progress, "columns": [f"{t}.{f.name}" for t, f, _r in new_columns], "row_level_security": _rls(spec), "phase": "expand" if expand else "additive", "transactional": True, "reversible": True})
                head = delta_revision(lineage, spec)
            if phases is not None and phases.new_indexes:
                files[index_migration_file(head, phases.new_indexes)] = index_migration(spec, head, phases.new_indexes)
                migrations.append({"revision": index_revision(head, phases.new_indexes), "down_revision": head, "file": index_migration_file(head, phases.new_indexes), "tables": [], "columns": [], "indexes": [index_name(str(i["table"]), list(i["columns"])) for i in phases.new_indexes], "row_level_security": _rls(spec), "phase": "index", "transactional": False, "reversible": True})
                head = index_revision(head, phases.new_indexes)
            if phases is not None and phases.contracting:
                files[contract_migration_file(head, phases.contracting)] = contract_migration(spec, head, phases.contracting)
                migrations.append({"revision": contract_revision(head, [str(c["op_id"]) for c in phases.contracting]), "down_revision": head, "file": contract_migration_file(head, phases.contracting), "tables": [], "columns": [f"{c['table']}.{c['column']}" for c in phases.contracting], "operations": [str(c["op_id"]) for c in phases.contracting], "row_level_security": _rls(spec), "phase": "contract", "transactional": False, "reversible": True})
                head = contract_revision(head, [str(c["op_id"]) for c in phases.contracting])
        test_phases = replace(phases, head=head) if phases is not None else None
        files["tests/test_migrations.py"] = _migration_tests(spec, lineage, test_phases)
        if has_phased:
            files[f"src/{pkg}/ops/__init__.py"] = '"""Operational tooling generated by FastAPI Architect: the backfill runner of phased migrations."""\n'
            files[f"src/{pkg}/ops/backfill.py"] = backfill_runner(spec)
            files[f"src/{pkg}/ops/backfills.py"] = backfill_specs(spec, phases.all_phased, phases.all_indexes, head or "")
            files["tests/test_backfill.py"] = backfill_tests(spec, phases.all_phased, phases.all_indexes)
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
        files[layout.path("schemas", m.name)] = _schemas(spec, m, pending, renames)
        files[layout.path("models", m.name)] = _models(spec, m, layout, pending, indexed, renames)
        files[layout.path("repository", m.name)] = _repository(spec, m, layout)
        files[layout.path("service", m.name)] = _service(spec, m, layout, ports, renames)
        files[layout.path("dependencies", m.name)] = _dependencies(spec, m, layout, ports)
        files[layout.path("router", m.name)] = _router(spec, m, layout)
        if ports:
            files[layout.path("ports", m.name)] = _ports(m, ports)
            if not layout.by_layer:
                files[layout.path("adapters", m.name)] = ""
            for p in ports:
                files[layout.adapter_path(m.name, p.name)] = _adapter(p)
        files[f"tests/test_{m.name}.py"] = _tests(spec, m, ports, renames)
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
    if has_phased:
        notes.append(f"phased migrations: {len(phases.all_phased)} change(s) with a backfill (python -m {pkg}.ops.backfill), {len(phases.all_indexes)} concurrent index(es); pending columns stay nullable until their contract phase")
    return Blueprint(style=rec.style, package=pkg, files=files, layout=layout_lines, notes=notes, migrations=migrations, phased=list(phases.all_phased) if phases is not None else [], indexes=list(phases.all_indexes) if phases is not None else [])


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


__all__ = ["Blueprint", "Layout", "Lineage", "added_columns", "build", "delta_revision", "initial_revision", "migration_file"]

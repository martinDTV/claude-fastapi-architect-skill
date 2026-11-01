"""Generated test suite: conftest, per-module tests and the migration tests."""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from ..spec import EntitySpec, IntegrationSpec, ModuleSpec, Spec
from ._base import ECHOED_TYPES, TENANT_A, TENANT_B, WRONG, Layout, Lineage, PhaseState
from ._helpers import (
    _allowed,
    _authz,
    _call,
    _camel,
    _changed_sample,
    _claim_binding,
    _entity_index,
    _fk_fields,
    _fk_name,
    _header,
    _identities,
    _identity,
    _oidc,
    _ordered_entities,
    _payload_literal,
    _registry,
    _rls,
    _roles_admin,
    _roles_member,
    _sample,
    _tenant,
    _visibility,
)
from .modules import _ops
from .persistence import added_columns, delta_revision, initial_revision
from .phased import progress_table


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


def _tests(spec: Spec, module: ModuleSpec, ports: List[IntegrationSpec], renames: Optional[Dict[Tuple[str, str], Dict[str, object]]] = None) -> str:
    renames = renames or {}
    api_key = spec.security["authentication"] == "api_key"
    oidc = _oidc(spec)
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
        for f in e.fields:
            entry = renames.get((e.plural, f.name))
            if entry is None or not ops["create"] or f.type not in ECHOED_TYPES:
                continue
            old = str(entry["from_column"])
            if True:
                test(f"test_create_{x}_accepts_the_former_name_{old}_and_returns_both_names", [f'    payload["{old}"] = payload.pop("{f.name}")', "    response = client.post(url, json=payload, headers=headers)", "    assert response.status_code == 201, response.text", "    body = response.json()", f'    assert body["{f.name}"] == body["{old}"] == PAYLOAD_{up}["{f.name}"]'])
            if ops["update"]:
                test(f"test_update_{x}_through_the_former_name_{old}_writes_both_columns", ["    created = client.post(url, json=payload, headers=headers).json()", f'    updated = client.patch({item}, json={{"{old}": {_changed_sample(f)}}}, headers=headers)', "    assert updated.status_code == 200, updated.text", f'    assert updated.json()["{f.name}"] == updated.json()["{old}"] == {_changed_sample(f)}', f'    again = client.patch({item}, json={{"{f.name}": {_sample(f)}}}, headers=headers)', f'    assert again.json()["{old}"] == {_sample(f)}'])
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


def _group(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[str, List[Tuple[str, str]]]]:
    out: Dict[str, List[Tuple[str, str]]] = {}
    for module, entity in pairs:
        out.setdefault(module, []).append((module, entity))
    return sorted(out.items())


def _migration_tests(spec: Spec, lineage: Optional[Lineage] = None, phases: Optional[PhaseState] = None) -> str:
    tenant = _tenant(spec)
    index = _entity_index(spec)
    has_delta = bool(lineage) and (any(e.plural not in set(lineage.tables) for _m, e in _ordered_entities(spec)) or bool(added_columns(spec, lineage)))
    revision = delta_revision(lineage, spec) if has_delta else (lineage.head if lineage else initial_revision(spec))
    if phases is not None and phases.head:
        revision = phases.head  # the chain may end with an index or contract revision (v2.1)
    tables = sorted(e.plural for _m, e in _ordered_entities(spec))
    progress = progress_table(spec) if phases is not None and (phases.all_phased or phases.new_phased) else None
    if progress:
        tables = sorted(tables + [progress])
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
        (f"        for table in [t for t in TABLES if t != {json.dumps(progress)}]:" if progress else "        for table in TABLES:"),
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

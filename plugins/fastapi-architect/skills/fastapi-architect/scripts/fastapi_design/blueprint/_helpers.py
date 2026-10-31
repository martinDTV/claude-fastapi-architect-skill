"""Small pure helpers shared by the generators: import ordering, headers, field rendering, contract predicates."""

from __future__ import annotations

import json
import textwrap
from typing import Dict, List, Sequence, Tuple

from ..spec import EntitySpec, FieldSpec, ModuleSpec, Spec
from ._base import LINE, PY_TYPES, SAMPLE


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

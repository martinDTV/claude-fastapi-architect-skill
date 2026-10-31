"""Architecture contract: a declarative, versioned specification of the backend to design (v2.0, schema 1.0).

The specification is plain JSON (never executed), validated strictly like ``.audit-fastapi.json``: unknown
properties are errors unless prefixed ``x-``, identifiers are checked, enumerations are closed, and every
problem is reported with its JSON path. Validation also records **provenance**: which values were written by
the author (``explicit``) and which were filled with a default (``assumed``), so the architecture report can
separate requirements from assumptions; ``decisions`` with ``status: pending`` and ``open_questions`` are the
pending decisions.

Top-level keys (``schema`` is required):

* ``name``, ``package`` (python identifier, defaults from the name), ``description``;
* ``scope``: ``size`` (small | medium | large), ``team_size``, ``expected_rps``, ``data_volume`` (small | medium | large);
* ``functional``: ``modules`` (at least one) with ``entities`` (at least one, with ``fields`` and ``operations``) and ``depends_on``;
  a ``uuid`` field may declare ``references: "module.Entity"`` (plus ``on_delete`` restrict | cascade | set_null), a foreign key in SQL
  persistence that must follow the module dependency direction (schema 1.1); a field may carry an explicit ``default``
  (schema, model and server default; the backfill strategy of a NOT NULL column added later);
* ``nonfunctional``: ``availability``, ``latency_p95_ms``, ``consistency`` (strong | eventual), ``auditability``, ``compliance``;
* ``persistence``: ``kind`` (memory | postgresql), ``migrations``;
* ``security``: ``authentication`` (none | api_key | oauth2_jwt | session); ``oidc`` (schema 1.2, required with oauth2_jwt:
  ``issuer``, ``audience``, ``algorithms`` asymmetric only, ``jwks_url``, ``leeway_s``, ``jwks_cache_s``, ``claims`` subject | tenant |
  roles); ``authorization``: the legacy string (none | role | resource, schemas 1.0/1.1: a declaration that stays pending) or the
  object of schema 1.2 (``model`` none | roles | ownership | roles_and_ownership, ``roles``, ``operations`` operation -> roles,
  ``ownership`` with ``visibility`` tenant | owner and ``admin_roles``); ``multi_tenant`` (``enabled``, ``strategy``
  shared_schema_application_scoping | shared_schema_rls | schema_per_tenant | database_per_tenant, ``tenant_param``, ``binding``
  none | api_key_registry | principal_claim: how the authenticated principal is bound to the tenant it addresses, ``rls``
  ``app_role``/``context_key``); ``audit`` (``authorization_decisions``). An entity may declare ``owned: true`` (schema 1.2): it
  carries the ``owner_id`` of the principal that created it and the ownership policy applies to it;
* ``integrations``: ``name``, ``kind`` (http | queue | smtp | storage | database), ``direction`` (outbound | inbound), ``criticality``;
* ``infrastructure``: ``deployment`` (container | vm | serverless), ``cache``, ``broker``, ``regions``;
* ``architecture``: ``style_preference`` (layered | modular_monolith | hexagonal | microservices), ``independent_deployment``;
* ``decisions``: ``id``, ``title``, ``status`` (accepted | pending | rejected), ``rationale``;
* ``components``: free list of named components with ``depends_on`` (documentation; validated for identifiers);
* ``testing``: ``levels`` (unit | integration | contract | end_to_end), ``coverage_target``;
* ``assumptions``, ``open_questions``: lists of strings.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SPEC_SCHEMA = "fastapi-architect/spec/1.2"  # emitted for new contracts
# accepted: 1.1 adds field.references/on_delete and security.multi_tenant.binding; 1.2 adds security.oidc, the authorization
# object (roles, operations, ownership), entity.owned, tenant strategy shared_schema_application_scoping (the new default),
# binding principal_claim, multi_tenant.rls and security.audit. In 1.0/1.1 documents 'shared_schema_rls' stays a declaration
# (application scoping is generated); in 1.2 documents it generates the policies or is rejected.
SPEC_SCHEMAS = ("fastapi-architect/spec/1.0", "fastapi-architect/spec/1.1", "fastapi-architect/spec/1.2")
SPEC_SCHEMA_12 = "fastapi-architect/spec/1.2"
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CLASS_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
FIELD_TYPES = ("str", "int", "float", "bool", "uuid", "datetime", "date", "decimal")
OPERATIONS = ("create", "get", "list", "update", "delete")
SIZES = ("small", "medium", "large")
CONSISTENCY = ("strong", "eventual")
PERSISTENCE = ("memory", "postgresql")
AUTHENTICATION = ("none", "api_key", "oauth2_jwt", "session")
AUTHORIZATION = ("none", "role", "resource")  # legacy string form (schemas 1.0/1.1): a declaration, never generated
AUTHORIZATION_MODELS = ("none", "roles", "ownership", "roles_and_ownership")  # schema 1.2 object form: generated and tested
OWNER_VISIBILITY = ("tenant", "owner")  # who may read an owned resource: every member of the tenant, or its owner and admins only
TENANT_STRATEGIES = ("shared_schema_application_scoping", "shared_schema_rls", "schema_per_tenant", "database_per_tenant")
TENANT_BINDINGS = ("none", "api_key_registry", "principal_claim")  # how a principal is bound to a tenant
JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")  # asymmetric only
INSECURE_JWT_ALGORITHMS = ("none", "HS256", "HS384", "HS512")
HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[^\s]*)?$")
CLAIM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:./-]{0,127}$")  # providers namespace custom claims as URLs
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,39}$")
GUC_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
ON_DELETE = ("restrict", "cascade", "set_null")
INTEGRATION_KINDS = ("http", "queue", "smtp", "storage", "database")
DIRECTIONS = ("outbound", "inbound")
CRITICALITY = ("low", "medium", "high")
DEPLOYMENTS = ("container", "vm", "serverless")
STYLES = ("layered", "modular_monolith", "hexagonal", "microservices")
DECISION_STATUS = ("accepted", "pending", "rejected")
TEST_LEVELS = ("unit", "integration", "contract", "end_to_end")
RESERVED_MODULE_NAMES = frozenset({"core", "api", "tests", "main", "modules", "services", "repositories", "schemas", "models", "ports", "adapters", "config", "db", "errors"})
RESERVED_FIELD_NAMES = frozenset({"id", "tenant_id", "owner_id", "created_at", "updated_at"})
TOP_LEVEL = ("schema", "name", "package", "description", "scope", "functional", "nonfunctional", "persistence", "security", "integrations", "infrastructure", "architecture", "decisions", "components", "testing", "assumptions", "open_questions")


class SpecError(ValueError):
    def __init__(self, source: Optional[str], errors: List[str]) -> None:
        self.source = source or "<spec>"
        self.errors = list(errors)
        super().__init__(f"{self.source}: {len(self.errors)} specification error(s): " + "; ".join(self.errors))


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


@dataclass
class FieldSpec:
    name: str
    type: str
    required: bool = True
    max_length: Optional[int] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    description: Optional[str] = None
    references: Optional[str] = None  # "module.Entity" (uuid fields only): a foreign key in SQL persistence
    on_delete: str = "restrict"
    default: Optional[object] = None  # explicit default (schema, model and server default); required fields may carry one
    has_default: bool = False


@dataclass
class EntitySpec:
    name: str  # ClassName
    fields: List[FieldSpec]
    operations: List[str]
    owned: bool = False  # schema 1.2: rows carry the owner_id of the creating principal; the ownership policy applies

    @property
    def snake(self) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", self.name).lower()

    @property
    def plural(self) -> str:
        """Collection name for routes and tables: entries, boxes, orders (regular English plurals only)."""
        s = self.snake
        if s.endswith("y") and len(s) > 1 and s[-2] not in "aeiou":
            return s[:-1] + "ies"
        if s.endswith(("s", "x", "z", "ch", "sh")):
            return s + "es"
        return s + "s"


@dataclass
class ModuleSpec:
    name: str
    description: str
    entities: List[EntitySpec]
    depends_on: List[str] = field(default_factory=list)


@dataclass
class IntegrationSpec:
    name: str
    kind: str
    direction: str
    criticality: str
    module: Optional[str] = None


@dataclass
class DecisionSpec:
    id: str
    title: str
    status: str
    rationale: str


@dataclass
class Spec:
    schema: str
    name: str
    package: str
    description: str
    scope: Dict[str, Any]
    modules: List[ModuleSpec]
    nonfunctional: Dict[str, Any]
    persistence: Dict[str, Any]
    security: Dict[str, Any]
    integrations: List[IntegrationSpec]
    infrastructure: Dict[str, Any]
    architecture: Dict[str, Any]
    decisions: List[DecisionSpec]
    components: List[Dict[str, Any]]
    testing: Dict[str, Any]
    assumptions: List[str]
    open_questions: List[str]
    provenance: Dict[str, str]  # json path -> explicit | assumed
    digest: str  # sha256 of the canonical source document
    source: Optional[str] = None
    document: Dict[str, Any] = field(default_factory=dict)  # the validated input, verbatim (stored in the manifest)

    @property
    def multi_tenant(self) -> bool:
        return bool(self.security["multi_tenant"]["enabled"])

    @property
    def authz_model(self) -> str:
        """none | roles | ownership | roles_and_ownership (the legacy string form of 1.0/1.1 is 'none' here, see ``authz_declared``)."""
        return str(self.security["authorization"]["model"])

    @property
    def authz_declared(self) -> Optional[str]:
        """The legacy ``authorization: role | resource`` declaration of schemas 1.0/1.1 (a pending decision), else None."""
        return self.security["authorization"].get("declared")

    @property
    def with_roles(self) -> bool:
        return self.authz_model in ("roles", "roles_and_ownership")

    @property
    def with_ownership(self) -> bool:
        return self.authz_model in ("ownership", "roles_and_ownership")

    @property
    def oidc(self) -> Optional[Dict[str, Any]]:
        return self.security.get("oidc")

    @property
    def rls(self) -> bool:
        """Row-level security is generated: schema 1.2, PostgreSQL, multi-tenant with strategy shared_schema_rls. In 1.0/1.1
        documents the same strategy is a declaration (application scoping is generated and the report says so)."""
        return self.schema == SPEC_SCHEMA_12 and self.multi_tenant and self.security["multi_tenant"]["strategy"] == "shared_schema_rls" and self.persistence["kind"] == "postgresql"

    def owned_entities(self) -> List[Tuple[ModuleSpec, EntitySpec]]:
        return [(m, e) for m in self.modules for e in m.entities if e.owned]

    def pending_decisions(self) -> List[str]:
        out = [f"decision {d.id}: {d.title}" for d in self.decisions if d.status == "pending"]
        out.extend(f"open question: {q}" for q in self.open_questions)
        return out

    def assumed_paths(self) -> List[str]:
        return sorted(p for p, how in self.provenance.items() if how == "assumed")

    def security_document(self) -> Dict[str, Any]:
        """The normalised security block in the shape of its schema: the legacy authorization string is emitted back as a
        string, the 1.2 object without its internal ``declared`` marker; ``oidc`` only when present."""
        out = json.loads(json.dumps(self.security))
        authz = out["authorization"]
        declared = authz.pop("declared", None)
        if declared is not None:
            out["authorization"] = declared
        if out.get("oidc") is None:
            out.pop("oidc", None)
        if out["multi_tenant"].get("strategy") != "shared_schema_rls":
            out["multi_tenant"].pop("rls", None)  # defaults that the validator only accepts next to the RLS strategy
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "package": self.package,
            "description": self.description,
            "scope": dict(self.scope),
            "functional": {"modules": [
                {"name": m.name, "description": m.description, "depends_on": list(m.depends_on), "entities": [
                    {k: v for k, v in {"name": e.name, "operations": list(e.operations), "owned": e.owned or None}.items() if v is not None} | {"fields": [
                        {k: v for k, v in {"name": f.name, "type": f.type, "required": f.required, "max_length": f.max_length, "min_value": f.min_value, "max_value": f.max_value, "description": f.description, "references": f.references, "on_delete": f.on_delete if f.references else None, "default": f.default if f.has_default else None}.items() if v is not None}
                        for f in e.fields
                    ]} for e in m.entities
                ]} for m in self.modules
            ]},
            "nonfunctional": dict(self.nonfunctional),
            "persistence": dict(self.persistence),
            "security": self.security_document(),
            "integrations": [{"name": i.name, "kind": i.kind, "direction": i.direction, "criticality": i.criticality, "module": i.module} for i in self.integrations],
            "infrastructure": dict(self.infrastructure),
            "architecture": dict(self.architecture),
            "decisions": [{"id": d.id, "title": d.title, "status": d.status, "rationale": d.rationale} for d in self.decisions],
            "components": list(self.components),
            "testing": dict(self.testing),
            "assumptions": list(self.assumptions),
            "open_questions": list(self.open_questions),
            "provenance": dict(sorted(self.provenance.items())),
            "digest": self.digest,
        }


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #


class _V:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.provenance: Dict[str, str] = {}

    def unknown(self, obj: Dict[str, Any], known: Tuple[str, ...], path: str) -> None:
        for key in obj:
            if key not in known and not str(key).startswith("x-"):
                self.errors.append(f"{path}.{key}: unknown property (known: {', '.join(known)}; use an 'x-' prefix for annotations)")

    def obj(self, parent: Dict[str, Any], key: str, path: str, required: bool = False) -> Dict[str, Any]:
        value = parent.get(key)
        if value is None:
            if required:
                self.errors.append(f"{path}: required object is missing")
            else:
                self.provenance[path] = "assumed"
            return {}
        if not isinstance(value, dict):
            self.errors.append(f"{path}: expected an object, got {type(value).__name__}")
            return {}
        self.provenance[path] = "explicit"
        return value

    def choice(self, obj: Dict[str, Any], key: str, path: str, options: Tuple[str, ...], default: Optional[str]) -> Optional[str]:
        if key not in obj:
            if default is not None:
                self.provenance[f"{path}.{key}"] = "assumed"
            return default
        value = obj[key]
        if not isinstance(value, str) or value not in options:
            self.errors.append(f"{path}.{key}: expected one of {', '.join(options)}, got {value!r}")
            return default
        self.provenance[f"{path}.{key}"] = "explicit"
        return value

    def boolean(self, obj: Dict[str, Any], key: str, path: str, default: bool) -> bool:
        if key not in obj:
            self.provenance[f"{path}.{key}"] = "assumed"
            return default
        if not isinstance(obj[key], bool):
            self.errors.append(f"{path}.{key}: expected true or false, got {obj[key]!r}")
            return default
        self.provenance[f"{path}.{key}"] = "explicit"
        return bool(obj[key])

    def integer(self, obj: Dict[str, Any], key: str, path: str, default: Optional[int], minimum: int = 0, maximum: Optional[int] = None) -> Optional[int]:
        if key not in obj:
            if default is not None:
                self.provenance[f"{path}.{key}"] = "assumed"
            return default
        value = obj[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
            self.errors.append(f"{path}.{key}: expected an integer between {minimum} and {maximum if maximum is not None else 'infinity'}, got {value!r}")
            return default
        self.provenance[f"{path}.{key}"] = "explicit"
        return value

    def text(self, obj: Dict[str, Any], key: str, path: str, default: Optional[str], pattern: Optional[re.Pattern] = None, required: bool = False) -> Optional[str]:
        if key not in obj:
            if required:
                self.errors.append(f"{path}.{key}: required")
            elif default is not None:
                self.provenance[f"{path}.{key}"] = "assumed"
            return default
        value = obj[key]
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"{path}.{key}: expected a non-empty string, got {value!r}")
            return default
        if pattern is not None and not pattern.match(value):
            self.errors.append(f"{path}.{key}: {value!r} does not match {pattern.pattern}")
            return default
        self.provenance[f"{path}.{key}"] = "explicit"
        return value

    def strings(self, obj: Dict[str, Any], key: str, path: str, options: Optional[Tuple[str, ...]] = None) -> List[str]:
        if key not in obj:
            return []
        value = obj[key]
        if not isinstance(value, list):
            self.errors.append(f"{path}.{key}: expected a list of strings, got {type(value).__name__}")
            return []
        out: List[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.errors.append(f"{path}.{key}[{i}]: expected a non-empty string, got {item!r}")
            elif options is not None and item not in options:
                self.errors.append(f"{path}.{key}[{i}]: expected one of {', '.join(options)}, got {item!r}")
            else:
                out.append(item)
        self.provenance[f"{path}.{key}"] = "explicit"
        return out


def _default_matches(ftype: Optional[str], value: Any) -> bool:
    if isinstance(value, bool):
        return ftype == "bool"
    if ftype == "str":
        return isinstance(value, str)
    if ftype == "int":
        return isinstance(value, int)
    if ftype == "float":
        return isinstance(value, (int, float))
    if ftype == "decimal":
        return isinstance(value, str) and re.match(r"^-?\d+(\.\d+)?$", value) is not None
    if ftype == "date":
        return isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value) is not None
    if ftype == "datetime":
        return isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$", value) is not None
    return False


def _validate_security(v: _V, document: Dict[str, Any], schema12: bool, package: str, persistence: Dict[str, Any], modules: List[ModuleSpec]) -> Dict[str, Any]:
    """The security block, normalised: ``authorization`` is always an object (``model`` plus, for the legacy string form,
    ``declared``); ``oidc`` is None unless authentication is oauth2_jwt. Every impossible or unsupported combination is an
    error with its path: the contract never records a guarantee the generator cannot build."""
    s_in = v.obj(document, "security", "spec.security")
    v.unknown(s_in, ("authentication", "oidc", "authorization", "multi_tenant", "audit"), "spec.security")
    authentication = v.choice(s_in, "authentication", "spec.security", AUTHENTICATION, "none") or "none"
    for key in ("oidc", "audit"):
        if key in s_in and not schema12:
            v.errors.append(f"spec.security.{key}: needs schema {SPEC_SCHEMA_12!r}")

    # authentication: OIDC configuration (schema 1.2)
    oidc: Optional[Dict[str, Any]] = None
    path = "spec.security.oidc"
    if authentication == "oauth2_jwt" and schema12:
        if "oidc" not in s_in:
            v.errors.append(f"{path}: required for authentication 'oauth2_jwt' (issuer, audience, algorithms, jwks_url, claims)")
        o_in = v.obj(s_in, "oidc", path)
        v.unknown(o_in, ("issuer", "audience", "algorithms", "jwks_url", "leeway_s", "jwks_cache_s", "claims"), path)
        issuer = v.text(o_in, "issuer", path, None, HTTPS_URL_RE, required=True)
        audience = v.text(o_in, "audience", path, None, required=True)
        raw_algorithms = o_in.get("algorithms")
        insecure = [a for a in raw_algorithms if isinstance(a, str) and a in INSECURE_JWT_ALGORITHMS] if isinstance(raw_algorithms, list) else []
        if insecure:
            v.errors.append(f"{path}.algorithms: {', '.join(insecure)} refused: only asymmetric signatures verify a token from an external provider ({', '.join(JWT_ALGORITHMS)})")
        algorithms = v.strings(o_in, "algorithms", path, JWT_ALGORITHMS) if "algorithms" in o_in else ["RS256"]
        if "algorithms" not in o_in:
            v.provenance[f"{path}.algorithms"] = "assumed"
        elif not algorithms and not insecure:
            v.errors.append(f"{path}.algorithms: expected at least one of {', '.join(JWT_ALGORITHMS)}")
        jwks_url = v.text(o_in, "jwks_url", path, (issuer.rstrip("/") + "/.well-known/jwks.json") if issuer else None, HTTPS_URL_RE)
        c_in = v.obj(o_in, "claims", f"{path}.claims")
        v.unknown(c_in, ("subject", "tenant", "roles"), f"{path}.claims")
        claims = {
            "subject": v.text(c_in, "subject", f"{path}.claims", "sub", CLAIM_RE),
            "tenant": v.text(c_in, "tenant", f"{path}.claims", "tenant_id", CLAIM_RE),
            "roles": v.text(c_in, "roles", f"{path}.claims", "roles", CLAIM_RE),
        }
        oidc = {
            "issuer": issuer,
            "audience": audience,
            "algorithms": sorted(set(algorithms), key=JWT_ALGORITHMS.index) if algorithms else ["RS256"],
            "jwks_url": jwks_url,
            "leeway_s": v.integer(o_in, "leeway_s", path, 30, 0, 300),
            "jwks_cache_s": v.integer(o_in, "jwks_cache_s", path, 300, 30, 86_400),
            "claims": claims,
        }
    elif "oidc" in s_in and schema12:
        v.errors.append(f"{path}: only valid with authentication 'oauth2_jwt' (got {authentication!r})")

    # authorization: legacy string (1.0/1.1, a declaration) or the 1.2 object (generated)
    path = "spec.security.authorization"
    raw_authz = s_in.get("authorization")
    authorization: Dict[str, Any] = {"model": "none", "roles": [], "operations": {}, "ownership": {"visibility": "tenant", "admin_roles": []}, "declared": None}
    if raw_authz is None:
        v.provenance[path] = "assumed"
    elif isinstance(raw_authz, str):
        declared = v.choice(s_in, "authorization", "spec.security", AUTHORIZATION, "none")
        if declared and declared != "none":
            authorization["declared"] = declared
        if declared != "none" and authentication == "none":
            v.errors.append(f"{path}: authorization requires an authentication kind other than 'none'")
    elif isinstance(raw_authz, dict):
        if not schema12:
            v.errors.append(f"{path}: the authorization object needs schema {SPEC_SCHEMA_12!r} (schemas 1.0/1.1 accept the strings {', '.join(AUTHORIZATION)})")
        v.provenance[path] = "explicit"
        v.unknown(raw_authz, ("model", "roles", "operations", "ownership"), path)
        if "model" not in raw_authz:
            v.errors.append(f"{path}.model: required (one of {', '.join(AUTHORIZATION_MODELS)})")
        model = v.choice(raw_authz, "model", path, AUTHORIZATION_MODELS, "none") or "none"
        roles = v.strings(raw_authz, "roles", path)
        seen_roles: List[str] = []
        for i, role in enumerate(roles):
            if not ROLE_NAME_RE.match(role):
                v.errors.append(f"{path}.roles[{i}]: {role!r} must match {ROLE_NAME_RE.pattern}")
            elif role in seen_roles:
                v.errors.append(f"{path}.roles[{i}]: duplicate role {role!r}")
            else:
                seen_roles.append(role)
        roles = seen_roles
        with_roles = model in ("roles", "roles_and_ownership")
        with_ownership = model in ("ownership", "roles_and_ownership")
        if with_roles and not roles:
            v.errors.append(f"{path}.roles: model {model!r} needs at least one role")
        if roles and not with_roles:
            v.errors.append(f"{path}.roles: roles are declared but model {model!r} does not use them (use roles or roles_and_ownership)")
        operations: Dict[str, List[str]] = {}
        ops_in = raw_authz.get("operations")
        if ops_in is not None:
            if not isinstance(ops_in, dict):
                v.errors.append(f"{path}.operations: expected an object operation -> list of roles")
            else:
                if not with_roles:
                    v.errors.append(f"{path}.operations: per-operation roles need model roles or roles_and_ownership")
                for op, allowed in ops_in.items():
                    if op not in OPERATIONS:
                        v.errors.append(f"{path}.operations.{op}: unknown operation (one of {', '.join(OPERATIONS)})")
                        continue
                    if not isinstance(allowed, list) or not allowed or not all(isinstance(r, str) for r in allowed):
                        v.errors.append(f"{path}.operations.{op}: expected a non-empty list of roles (omit the operation to allow every authenticated member)")
                        continue
                    unknown_roles = [r for r in allowed if r not in roles]
                    if unknown_roles:
                        v.errors.append(f"{path}.operations.{op}: undeclared role(s) {', '.join(repr(r) for r in unknown_roles)}")
                    operations[op] = sorted(set(allowed), key=allowed.index)
                v.provenance[f"{path}.operations"] = "explicit"
        own_in = v.obj(raw_authz, "ownership", f"{path}.ownership")
        v.unknown(own_in, ("visibility", "admin_roles"), f"{path}.ownership")
        if own_in and not with_ownership:
            v.errors.append(f"{path}.ownership: only valid with model ownership or roles_and_ownership")
        admin_roles = v.strings(own_in, "admin_roles", f"{path}.ownership")
        for r in admin_roles:
            if r not in roles:
                v.errors.append(f"{path}.ownership.admin_roles: undeclared role {r!r}")
        if admin_roles and not with_roles:
            v.errors.append(f"{path}.ownership.admin_roles: admin roles need a role model (roles_and_ownership)")
        ownership = {"visibility": v.choice(own_in, "visibility", f"{path}.ownership", OWNER_VISIBILITY, "tenant"), "admin_roles": sorted(set(admin_roles), key=admin_roles.index)}
        authorization = {"model": model, "roles": roles, "operations": dict(sorted(operations.items(), key=lambda kv: OPERATIONS.index(kv[0]))), "ownership": ownership, "declared": None}
        if model != "none" and authentication == "none":
            v.errors.append(f"{path}.model: {model!r} needs an authenticated principal (authentication is 'none')")
        if model != "none" and authentication == "session":
            v.errors.append(f"{path}.model: {model!r} cannot be generated with authentication 'session' (not generated); use oauth2_jwt or api_key with a key registry")
    else:
        v.errors.append(f"{path}: expected a string ({', '.join(AUTHORIZATION)}) or an object with 'model'")
    owned = [(m.name, e.name) for m in modules for e in m.entities if e.owned]
    if authorization["model"] in ("ownership", "roles_and_ownership") and not owned:
        v.errors.append(f"{path}.model: {authorization['model']!r} needs at least one entity with owned: true")
    if owned and authorization["model"] not in ("ownership", "roles_and_ownership"):
        v.errors.append(f"spec.functional.modules: owned entities ({', '.join(f'{m}.{e}' for m, e in owned)}) need security.authorization.model ownership or roles_and_ownership")

    # tenancy
    path = "spec.security.multi_tenant"
    mt_in = v.obj(s_in, "multi_tenant", path)
    v.unknown(mt_in, ("enabled", "strategy", "tenant_param", "binding", "rls"), path)
    tenant_param = v.text(mt_in, "tenant_param", path, "tenant_id")
    if tenant_param and (not IDENT_RE.match(tenant_param) or keyword.iskeyword(tenant_param)):
        v.errors.append(f"{path}.tenant_param: {tenant_param!r} must be a python identifier")
    enabled = v.boolean(mt_in, "enabled", path, False)
    strategy = v.choice(mt_in, "strategy", path, TENANT_STRATEGIES, "shared_schema_application_scoping" if schema12 else "shared_schema_rls")
    binding = v.choice(mt_in, "binding", path, TENANT_BINDINGS, "none") or "none"
    if strategy == "shared_schema_application_scoping" and "strategy" in mt_in and not schema12:
        v.errors.append(f"{path}.strategy: 'shared_schema_application_scoping' needs schema {SPEC_SCHEMA_12!r}")
    if binding == "principal_claim" and not schema12:
        v.errors.append(f"{path}.binding: 'principal_claim' needs schema {SPEC_SCHEMA_12!r}")
    if "rls" in mt_in and not schema12:
        v.errors.append(f"{path}.rls: needs schema {SPEC_SCHEMA_12!r}")
    rls_in = v.obj(mt_in, "rls", f"{path}.rls")
    v.unknown(rls_in, ("app_role", "context_key"), f"{path}.rls")
    app_role = v.text(rls_in, "app_role", f"{path}.rls", f"{package}_app"[:63], IDENT_RE)
    context_key = v.text(rls_in, "context_key", f"{path}.rls", "app.tenant_id", GUC_RE)
    if rls_in and strategy != "shared_schema_rls":
        v.errors.append(f"{path}.rls: only valid with strategy 'shared_schema_rls'")
    if enabled and authentication == "none":
        v.errors.append(f"{path}.enabled: a multi-tenant API needs authentication (who is the member of the tenant?)")
    if binding == "api_key_registry" and authentication != "api_key":
        v.errors.append(f"{path}.binding: 'api_key_registry' binds API keys to tenants and needs authentication 'api_key'")
    if binding == "principal_claim" and authentication != "oauth2_jwt":
        v.errors.append(f"{path}.binding: 'principal_claim' takes the tenant from a verified token claim and needs authentication 'oauth2_jwt'")
    if binding != "none" and not enabled:
        v.errors.append(f"{path}.binding: a binding needs multi_tenant.enabled: true")
    if schema12 and enabled and strategy == "shared_schema_rls":
        if persistence.get("kind") != "postgresql":
            v.errors.append(f"{path}.strategy: 'shared_schema_rls' is PostgreSQL row-level security; persistence.kind is {persistence.get('kind')!r} (use shared_schema_application_scoping, or postgresql)")
        if binding == "none":
            v.errors.append(f"{path}.strategy: 'shared_schema_rls' sets the tenant context from the authenticated principal and needs a binding (api_key_registry or principal_claim)")
    # identity: a shared API key carries no caller identity
    if authorization["model"] != "none" and authentication == "api_key" and binding != "api_key_registry":
        v.errors.append(f"{path}.binding: authorization model {authorization['model']!r} needs a caller identity; a shared API key has none: use authentication oauth2_jwt, or multi_tenant.binding 'api_key_registry' (one grant per key)")

    # audit requirements
    a_in = v.obj(s_in, "audit", "spec.security.audit")
    v.unknown(a_in, ("authorization_decisions",), "spec.security.audit")
    audit = {"authorization_decisions": v.boolean(a_in, "authorization_decisions", "spec.security.audit", False)}
    if audit["authorization_decisions"] and authentication == "none":
        v.errors.append("spec.security.audit.authorization_decisions: there are no authorization decisions to record without authentication")

    return {
        "authentication": authentication,
        "oidc": oidc,
        "authorization": authorization,
        "multi_tenant": {"enabled": enabled, "strategy": strategy, "tenant_param": tenant_param, "binding": binding, "rls": {"app_role": app_role, "context_key": context_key}},
        "audit": audit,
    }


def _ident(v: _V, value: Any, path: str, what: str) -> Optional[str]:
    if not isinstance(value, str) or not IDENT_RE.match(value) or keyword.iskeyword(value):
        v.errors.append(f"{path}: {what} must be a lowercase python identifier (a-z, 0-9, _), got {value!r}")
        return None
    return value


def canonical_digest(document: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def validate_spec(document: Any, source: Optional[str] = None) -> Tuple[Optional[Spec], List[str]]:
    """``(spec, errors)``: the spec is None when there are errors."""
    v = _V()
    if not isinstance(document, dict):
        return None, ["top level: expected an object"]
    v.unknown(document, TOP_LEVEL, "spec")
    schema = document.get("schema")
    if schema not in SPEC_SCHEMAS:
        v.errors.append(f"spec.schema: expected one of {', '.join(repr(x) for x in SPEC_SCHEMAS)}, got {schema!r}")
    schema12 = schema == SPEC_SCHEMA_12
    name = v.text(document, "name", "spec", None, NAME_RE, required=True) or "service"
    default_package = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "service"
    if default_package[0].isdigit():
        default_package = "svc_" + default_package
    package = v.text(document, "package", "spec", default_package)
    if package is not None and (not IDENT_RE.match(package) or keyword.iskeyword(package)):
        v.errors.append(f"spec.package: {package!r} must be a lowercase python identifier")
    description = v.text(document, "description", "spec", f"{name} backend")

    scope_in = v.obj(document, "scope", "spec.scope")
    v.unknown(scope_in, ("size", "team_size", "expected_rps", "data_volume"), "spec.scope")
    scope = {
        "size": v.choice(scope_in, "size", "spec.scope", SIZES, "small"),
        "team_size": v.integer(scope_in, "team_size", "spec.scope", 2, 1, 1000),
        "expected_rps": v.integer(scope_in, "expected_rps", "spec.scope", 50, 0, 10_000_000),
        "data_volume": v.choice(scope_in, "data_volume", "spec.scope", SIZES, "small"),
    }

    functional = v.obj(document, "functional", "spec.functional", required=True)
    v.unknown(functional, ("modules",), "spec.functional")
    modules: List[ModuleSpec] = []
    raw_modules = functional.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        v.errors.append("spec.functional.modules: expected a non-empty list of modules")
        raw_modules = []
    module_names: List[str] = []
    pending_refs: List[Tuple[str, Optional[str], str]] = []  # (field path, module, "module.Entity" or "Entity")
    for i, raw in enumerate(raw_modules):
        path = f"spec.functional.modules[{i}]"
        if not isinstance(raw, dict):
            v.errors.append(f"{path}: expected an object")
            continue
        v.unknown(raw, ("name", "description", "entities", "depends_on"), path)
        mname = _ident(v, raw.get("name"), f"{path}.name", "module name")
        if mname in RESERVED_MODULE_NAMES:
            v.errors.append(f"{path}.name: {mname!r} is reserved")
            mname = None
        if mname in module_names:
            v.errors.append(f"{path}.name: duplicate module {mname!r}")
        if mname:
            module_names.append(mname)
        mdesc = v.text(raw, "description", path, f"{mname or 'module'} module")
        entities: List[EntitySpec] = []
        raw_entities = raw.get("entities")
        if not isinstance(raw_entities, list) or not raw_entities:
            v.errors.append(f"{path}.entities: expected a non-empty list of entities")
            raw_entities = []
        entity_names: List[str] = []
        for j, ent in enumerate(raw_entities):
            epath = f"{path}.entities[{j}]"
            if not isinstance(ent, dict):
                v.errors.append(f"{epath}: expected an object")
                continue
            v.unknown(ent, ("name", "fields", "operations", "owned"), epath)
            owned = v.boolean(ent, "owned", epath, False)
            if owned and not schema12:
                v.errors.append(f"{epath}.owned: owned entities need schema {SPEC_SCHEMA_12!r}")
            ename = ent.get("name")
            if not isinstance(ename, str) or not CLASS_RE.match(ename):
                v.errors.append(f"{epath}.name: entity name must be a CapitalizedWord, got {ename!r}")
                ename = None
            if ename in entity_names:
                v.errors.append(f"{epath}.name: duplicate entity {ename!r}")
            if ename:
                entity_names.append(ename)
            fields: List[FieldSpec] = []
            raw_fields = ent.get("fields")
            if not isinstance(raw_fields, list) or not raw_fields:
                v.errors.append(f"{epath}.fields: expected a non-empty list of fields")
                raw_fields = []
            seen_fields: List[str] = []
            for k, fld in enumerate(raw_fields):
                fpath = f"{epath}.fields[{k}]"
                if not isinstance(fld, dict):
                    v.errors.append(f"{fpath}: expected an object")
                    continue
                v.unknown(fld, ("name", "type", "required", "max_length", "min_value", "max_value", "description", "references", "on_delete", "default"), fpath)
                fname = _ident(v, fld.get("name"), f"{fpath}.name", "field name")
                if fname in RESERVED_FIELD_NAMES:
                    v.errors.append(f"{fpath}.name: {fname!r} is generated automatically and cannot be declared")
                    fname = None
                if fname in seen_fields:
                    v.errors.append(f"{fpath}.name: duplicate field {fname!r}")
                if fname:
                    seen_fields.append(fname)
                ftype = v.choice(fld, "type", fpath, FIELD_TYPES, None)
                if ftype is None and "type" not in fld:
                    v.errors.append(f"{fpath}.type: required (one of {', '.join(FIELD_TYPES)})")
                required = v.boolean(fld, "required", fpath, True)
                max_length = v.integer(fld, "max_length", fpath, None, 1, 100_000)
                if max_length is not None and ftype != "str":
                    v.errors.append(f"{fpath}.max_length: only valid for type 'str'")
                min_value = fld.get("min_value")
                max_value = fld.get("max_value")
                for key, val in (("min_value", min_value), ("max_value", max_value)):
                    if val is not None:
                        if isinstance(val, bool) or not isinstance(val, (int, float)):
                            v.errors.append(f"{fpath}.{key}: expected a number, got {val!r}")
                        elif ftype not in ("int", "float", "decimal"):
                            v.errors.append(f"{fpath}.{key}: only valid for numeric types")
                        else:
                            v.provenance[f"{fpath}.{key}"] = "explicit"
                if isinstance(min_value, (int, float)) and isinstance(max_value, (int, float)) and not isinstance(min_value, bool) and min_value > max_value:
                    v.errors.append(f"{fpath}: min_value {min_value} is greater than max_value {max_value}")
                fdesc = v.text(fld, "description", fpath, None)
                references = v.text(fld, "references", fpath, None, re.compile(r"^(?:[a-z][a-z0-9_]*\.)?[A-Z][A-Za-z0-9]*$"))
                on_delete = v.choice(fld, "on_delete", fpath, ON_DELETE, "restrict") or "restrict"
                if references is not None and ftype != "uuid":
                    v.errors.append(f"{fpath}.references: only valid for type 'uuid' (the referenced id)")
                if "on_delete" in fld and references is None:
                    v.errors.append(f"{fpath}.on_delete: only valid together with 'references'")
                if on_delete == "set_null" and required:
                    v.errors.append(f"{fpath}.on_delete: 'set_null' needs required: false")
                if references is not None:
                    pending_refs.append((fpath, mname, references))
                has_default = "default" in fld
                default = fld.get("default")
                if has_default:
                    if default is None:
                        v.errors.append(f"{fpath}.default: null is not a default; use required: false for a nullable field")
                        has_default = False
                    elif not _default_matches(ftype, default):
                        v.errors.append(f"{fpath}.default: {default!r} is not a valid default for type {ftype!r} (str, int, float, bool, decimal as a string, date/datetime as ISO strings; uuid defaults are not supported)")
                        has_default = False
                    elif references is not None:
                        v.errors.append(f"{fpath}.default: a reference cannot have a default")
                        has_default = False
                    else:
                        v.provenance[f"{fpath}.default"] = "explicit"
                if fname and ftype:
                    fields.append(FieldSpec(fname, ftype, required, max_length, min_value if isinstance(min_value, (int, float)) else None, max_value if isinstance(max_value, (int, float)) else None, fdesc, references, on_delete, default if has_default else None, has_default))
            operations = v.strings(ent, "operations", epath, OPERATIONS) if "operations" in ent else list(OPERATIONS)
            if "operations" not in ent:
                v.provenance[f"{epath}.operations"] = "assumed"
            elif not operations:
                v.errors.append(f"{epath}.operations: expected at least one of {', '.join(OPERATIONS)}")
            if ename and fields:
                entities.append(EntitySpec(ename, fields, sorted(set(operations), key=OPERATIONS.index), owned))
        depends_on = [d for d in v.strings(raw, "depends_on", path) if _ident(v, d, f"{path}.depends_on", "module name")]
        if mname and mname in depends_on:
            v.errors.append(f"{path}.depends_on: a module cannot depend on itself")
        if mname and entities:
            modules.append(ModuleSpec(mname, mdesc or f"{mname} module", entities, depends_on))
    for m in modules:
        for d in m.depends_on:
            if d not in module_names:
                v.errors.append(f"spec.functional.modules[{module_names.index(m.name)}].depends_on: unknown module {d!r}")
    _check_cycles(v, modules)
    _check_references(v, modules, pending_refs)

    nf_in = v.obj(document, "nonfunctional", "spec.nonfunctional")
    v.unknown(nf_in, ("availability", "latency_p95_ms", "consistency", "auditability", "compliance"), "spec.nonfunctional")
    nonfunctional = {
        "availability": v.text(nf_in, "availability", "spec.nonfunctional", "99.5%"),
        "latency_p95_ms": v.integer(nf_in, "latency_p95_ms", "spec.nonfunctional", 300, 1, 600_000),
        "consistency": v.choice(nf_in, "consistency", "spec.nonfunctional", CONSISTENCY, "strong"),
        "auditability": v.boolean(nf_in, "auditability", "spec.nonfunctional", False),
        "compliance": v.strings(nf_in, "compliance", "spec.nonfunctional"),
    }

    p_in = v.obj(document, "persistence", "spec.persistence")
    v.unknown(p_in, ("kind", "migrations"), "spec.persistence")
    persistence = {"kind": v.choice(p_in, "kind", "spec.persistence", PERSISTENCE, "memory"), "migrations": v.boolean(p_in, "migrations", "spec.persistence", False)}
    if persistence["migrations"] and persistence["kind"] == "memory":
        v.errors.append("spec.persistence.migrations: migrations require persistence.kind 'postgresql'")

    security = _validate_security(v, document, schema12, package or default_package, persistence, modules)

    integrations: List[IntegrationSpec] = []
    raw_int = document.get("integrations", [])
    if not isinstance(raw_int, list):
        v.errors.append("spec.integrations: expected a list")
        raw_int = []
    else:
        v.provenance["spec.integrations"] = "explicit" if "integrations" in document else "assumed"
    seen_int: List[str] = []
    for i, raw in enumerate(raw_int):
        path = f"spec.integrations[{i}]"
        if not isinstance(raw, dict):
            v.errors.append(f"{path}: expected an object")
            continue
        v.unknown(raw, ("name", "kind", "direction", "criticality", "module"), path)
        iname = _ident(v, raw.get("name"), f"{path}.name", "integration name")
        if iname in seen_int:
            v.errors.append(f"{path}.name: duplicate integration {iname!r}")
        if iname:
            seen_int.append(iname)
        kind = v.choice(raw, "kind", path, INTEGRATION_KINDS, None)
        if kind is None and "kind" not in raw:
            v.errors.append(f"{path}.kind: required (one of {', '.join(INTEGRATION_KINDS)})")
        direction = v.choice(raw, "direction", path, DIRECTIONS, "outbound")
        criticality = v.choice(raw, "criticality", path, CRITICALITY, "medium")
        module = raw.get("module")
        if module is not None and module not in module_names:
            v.errors.append(f"{path}.module: unknown module {module!r}")
        if iname and kind:
            integrations.append(IntegrationSpec(iname, kind, direction or "outbound", criticality or "medium", module if isinstance(module, str) else None))

    i_in = v.obj(document, "infrastructure", "spec.infrastructure")
    v.unknown(i_in, ("deployment", "cache", "broker", "regions"), "spec.infrastructure")
    infrastructure = {
        "deployment": v.choice(i_in, "deployment", "spec.infrastructure", DEPLOYMENTS, "container"),
        "cache": v.boolean(i_in, "cache", "spec.infrastructure", False),
        "broker": v.boolean(i_in, "broker", "spec.infrastructure", False),
        "regions": v.integer(i_in, "regions", "spec.infrastructure", 1, 1, 50),
    }

    a_in = v.obj(document, "architecture", "spec.architecture")
    v.unknown(a_in, ("style_preference", "independent_deployment"), "spec.architecture")
    architecture = {
        "style_preference": v.choice(a_in, "style_preference", "spec.architecture", STYLES, None),
        "independent_deployment": v.boolean(a_in, "independent_deployment", "spec.architecture", False),
    }

    decisions: List[DecisionSpec] = []
    raw_dec = document.get("decisions", [])
    if not isinstance(raw_dec, list):
        v.errors.append("spec.decisions: expected a list")
        raw_dec = []
    seen_dec: List[str] = []
    for i, raw in enumerate(raw_dec):
        path = f"spec.decisions[{i}]"
        if not isinstance(raw, dict):
            v.errors.append(f"{path}: expected an object")
            continue
        v.unknown(raw, ("id", "title", "status", "rationale"), path)
        did = v.text(raw, "id", path, None, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"), required=True)
        if did in seen_dec:
            v.errors.append(f"{path}.id: duplicate decision id {did!r}")
        if did:
            seen_dec.append(did)
        title = v.text(raw, "title", path, None, required=True)
        status = v.choice(raw, "status", path, DECISION_STATUS, "pending")
        rationale = v.text(raw, "rationale", path, "")
        if status == "accepted" and not rationale:
            v.errors.append(f"{path}.rationale: an accepted decision must state its rationale")
        if did and title:
            decisions.append(DecisionSpec(did, title, status or "pending", rationale or ""))

    components: List[Dict[str, Any]] = []
    raw_comp = document.get("components", [])
    if not isinstance(raw_comp, list):
        v.errors.append("spec.components: expected a list")
        raw_comp = []
    comp_names: List[str] = []
    for i, raw in enumerate(raw_comp):
        path = f"spec.components[{i}]"
        if not isinstance(raw, dict):
            v.errors.append(f"{path}: expected an object")
            continue
        v.unknown(raw, ("name", "kind", "depends_on", "description"), path)
        cname = _ident(v, raw.get("name"), f"{path}.name", "component name")
        if cname:
            comp_names.append(cname)
            components.append({"name": cname, "kind": raw.get("kind", "component"), "depends_on": v.strings(raw, "depends_on", path), "description": raw.get("description", "")})
    for i, comp in enumerate(components):
        for dep in comp["depends_on"]:
            if dep not in comp_names and dep not in module_names:
                v.errors.append(f"spec.components[{i}].depends_on: unknown component or module {dep!r}")

    t_in = v.obj(document, "testing", "spec.testing")
    v.unknown(t_in, ("levels", "coverage_target"), "spec.testing")
    levels = v.strings(t_in, "levels", "spec.testing", TEST_LEVELS) if "levels" in t_in else ["unit", "integration"]
    if "levels" not in t_in:
        v.provenance["spec.testing.levels"] = "assumed"
    testing = {"levels": levels, "coverage_target": v.integer(t_in, "coverage_target", "spec.testing", 80, 0, 100)}

    assumptions = v.strings(document, "assumptions", "spec")
    open_questions = v.strings(document, "open_questions", "spec")

    if v.errors:
        return None, sorted(set(v.errors))
    spec = Spec(
        schema=str(schema), name=name, package=package or default_package, description=description or "", scope=scope, modules=modules,
        nonfunctional=nonfunctional, persistence=persistence, security=security, integrations=integrations, infrastructure=infrastructure,
        architecture=architecture, decisions=decisions, components=components, testing=testing, assumptions=assumptions,
        open_questions=open_questions, provenance=v.provenance, digest=canonical_digest(document), source=source,
        document=json.loads(json.dumps(document)),
    )
    return spec, []


def _check_references(v: _V, modules: List[ModuleSpec], pending: List[Tuple[str, Optional[str], str]]) -> None:
    """A reference names an existing entity, in the same module or in a declared dependency, and is normalised
    to ``module.Entity`` on the field."""
    by_module = {m.name: m for m in modules}
    for fpath, module_name, raw in pending:
        if module_name is None:
            continue
        target_module, _, entity = raw.rpartition(".")
        target_module = target_module or module_name
        module = by_module.get(module_name)
        target = by_module.get(target_module)
        if target is None or entity not in {e.name for e in target.entities}:
            v.errors.append(f"{fpath}.references: unknown entity {raw!r}")
            continue
        if target_module != module_name and (module is None or target_module not in module.depends_on):
            v.errors.append(f"{fpath}.references: {raw!r} lives in module {target_module!r}; declare it in depends_on of {module_name!r} (references follow the dependency direction)")
            continue
        if module is not None:
            for e in module.entities:
                for f in e.fields:
                    if f.references == raw:
                        f.references = f"{target_module}.{entity}"


def _check_cycles(v: _V, modules: List[ModuleSpec]) -> None:
    graph = {m.name: set(m.depends_on) for m in modules}
    state: Dict[str, int] = {}

    def visit(node: str, stack: List[str]) -> None:
        if state.get(node) == 1:
            v.errors.append("spec.functional.modules: dependency cycle " + " -> ".join(stack + [node]))
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if nxt in graph:
                visit(nxt, stack + [node])
        state[node] = 2

    for name in sorted(graph):
        visit(name, [])


def load_spec(path: str) -> Spec:
    """Read a JSON specification (never executed) and validate it; raises ``SpecError`` listing every problem."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise SpecError(path, [f"cannot read the specification: {exc}"])
    except ValueError as exc:
        raise SpecError(path, [f"the specification is not valid JSON: {exc}"])
    spec, errors = validate_spec(document, path)
    if errors or spec is None:
        raise SpecError(path, errors)
    return spec

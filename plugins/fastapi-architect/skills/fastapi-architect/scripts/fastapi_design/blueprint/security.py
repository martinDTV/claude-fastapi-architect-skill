"""Generated security modules: API-key grants, the OIDC/JWKS client and the resource authorization policy."""

from __future__ import annotations

import json

from ..spec import Spec
from ._helpers import _claim_binding, _header, _oidc, _principal_dep, _registry, _roles_admin, _tenant, _visibility


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

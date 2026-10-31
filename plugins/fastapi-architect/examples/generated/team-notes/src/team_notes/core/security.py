"""OIDC bearer tokens issued by an external provider (authentication oauth2_jwt).

A token is accepted only when: its header algorithm is one of ALGORITHMS (asymmetric; HS*/none are refused before
any key is looked up), its signature verifies against the provider key named by ``kid``, ``iss`` equals the configured
issuer, ``aud`` contains the configured audience, ``exp``/``nbf``/``iat`` hold within the leeway and the required claims
are present.
The principal is built from the verified claims only; nothing but the header is read before the signature check. The
JWKS is fetched over https, cached for ``oidc_jwks_cache_s`` seconds and refreshed at most once per unknown key id
(a forged kid cannot force a fetch per request); a static JWKS is accepted only in the test environment. A provider
that cannot be reached is 503, not 401. There are no refresh tokens, sessions or token issuance here.
"""

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import PyJWKError, PyJWKSetError

from team_notes.core.config import Settings, SettingsDep

ALGORITHMS = ("RS256",)
SUBJECT_CLAIM = "sub"
TENANT_CLAIM = "workspace_id"
ROLES_CLAIM = "roles"
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "workspace_id")
MIN_REFRESH_INTERVAL_S = 30  # key rotation: one JWKS refresh per unknown key id, never more often than this
FETCH_TIMEOUT_S = 5
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, from verified claims only: subject, tenant and roles."""

    subject: str
    tenant_id: UUID
    roles: frozenset[str]


class JwksSource:
    """Keys of the provider: the https JWKS endpoint with a bounded cache, or a static document (tests only)."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.oidc_jwks_url
        self._lifespan = settings.oidc_jwks_cache_s
        self._lock = threading.Lock()
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0
        self._static = False
        if settings.oidc_jwks_json:
            if settings.environment != "test":
                raise RuntimeError("a static JWKS (OIDC_JWKS_JSON) is accepted only when ENVIRONMENT is 'test'")
            self._keys = PyJWKSet.from_json(settings.oidc_jwks_json)
            self._static = True
        elif not self._url.startswith("https://"):
            raise RuntimeError("OIDC_JWKS_URL must be an https URL")

    def _fetch(self) -> PyJWKSet:
        with urllib.request.urlopen(self._url, timeout=FETCH_TIMEOUT_S) as response:  # https enforced in __init__
            return PyJWKSet.from_dict(json.load(response))

    def _match(self, kid: str) -> PyJWK | None:
        for key in self._keys.keys if self._keys else []:
            if key.key_id == kid:
                return key
        return None

    def key_for(self, kid: str) -> PyJWK | None:
        """The signing key with this id; the cache is refreshed when stale or when the id is unknown (rate limited)."""
        with self._lock:
            now = time.monotonic()
            if not self._static and (self._keys is None or now - self._fetched_at > self._lifespan):
                self._keys, self._fetched_at = self._fetch(), now
            found = self._match(kid)
            if found is None and not self._static and now - self._fetched_at > MIN_REFRESH_INTERVAL_S:
                self._keys, self._fetched_at = self._fetch(), now
                found = self._match(kid)
            return found


def get_jwks(request: Request) -> JwksSource:
    return request.app.state.jwks


def _unauthorized(detail: str) -> HTTPException:
    challenge = {"WWW-Authenticate": "Bearer"}
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=challenge)


def verify_token(token: str, source: JwksSource, settings: Settings) -> dict[str, object]:
    """The verified claims of ``token``: 401 for anything wrong with it, 503 when the provider keys cannot be read."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("malformed token") from exc
    if header.get("alg") not in ALGORITHMS:
        raise _unauthorized("token algorithm is not accepted")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _unauthorized("token has no key id")
    try:
        key = source.key_for(kid)
    except (OSError, ValueError, PyJWKError, PyJWKSetError) as exc:
        detail = "identity provider keys are unavailable"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
    if key is None:
        raise _unauthorized("unknown signing key")
    try:
        return jwt.decode(
            token,
            key.key,
            algorithms=list(ALGORITHMS),
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_leeway_s,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.InvalidTokenError as exc:
        raise _unauthorized(f"invalid token ({type(exc).__name__})") from exc


def _roles(claims: dict[str, object]) -> frozenset[str]:
    raw = claims.get(ROLES_CLAIM, [])
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
        raise _unauthorized("roles claim is malformed")
    return frozenset(raw)


def require_bearer_token(
    settings: SettingsDep, source: Annotated[JwksSource, Depends(get_jwks)], authorization: AuthorizationHeader = None
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized("missing bearer token")
    claims = verify_token(authorization[len("Bearer ") :].strip(), source, settings)
    subject = claims.get(SUBJECT_CLAIM)
    if not isinstance(subject, str) or not subject:
        raise _unauthorized("token has no usable subject claim")
    raw_tenant = claims.get(TENANT_CLAIM)
    try:
        tenant_id = UUID(str(raw_tenant))
    except (TypeError, ValueError) as exc:
        raise _unauthorized("token has no usable tenant claim") from exc
    return Principal(subject=subject, tenant_id=tenant_id, roles=_roles(claims))


AuthenticatedPrincipal = Annotated[Principal, Depends(require_bearer_token)]


def require_tenant_member(workspace_id: UUID, principal: AuthenticatedPrincipal) -> Principal:
    if principal.tenant_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="the token is not a member of this tenant")
    return principal


TenantPrincipalDep = Annotated[Principal, Depends(require_tenant_member)]

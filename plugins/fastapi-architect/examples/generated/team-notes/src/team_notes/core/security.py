"""OIDC bearer tokens issued by an external provider (authentication oauth2_jwt).

A token is accepted only when: its header algorithm is one of ALGORITHMS (asymmetric; HS*/none are refused before
any key is looked up), its signature verifies against the provider key named by ``kid``, ``iss`` equals the configured
issuer, ``aud`` contains the configured audience, ``exp``/``nbf``/``iat`` hold within the leeway and the required claims
are present.
The principal is built from the verified claims only; nothing but the header is read before the signature check. The
JWKS is fetched over https with TLS verification always on (a private CA may be added through oidc_ca_bundle, never
removed), the response is capped in size, the keys are cached for ``oidc_jwks_cache_s`` seconds, a refresh is
attempted at most once per MIN_REFRESH_INTERVAL_S (an unknown or forged kid cannot force a fetch per request), and
when a refresh fails the last keys are served for at most ``oidc_jwks_stale_max_s``. A static JWKS is accepted only
in the test environment. A provider that cannot be reached is 503 (JwksUnavailable), never 401: an outage is not a
statement about the token. There are no refresh tokens, sessions or token issuance here.
"""

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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
MIN_REFRESH_INTERVAL_S = 30  # one JWKS fetch attempt per interval, whatever the reason (expiry, unknown kid, failure)
MAX_JWKS_BYTES = 262_144  # a key set is a few kilobytes; anything larger is not one
LOG = logging.getLogger("team_notes.security")
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, from verified claims only: subject, tenant and roles."""

    subject: str
    tenant_id: UUID
    roles: frozenset[str]


class JwksUnavailable(RuntimeError):
    """The provider keys could not be obtained or read: an infrastructure failure, never a verdict on a token."""


class JwksSource:
    """Keys of the provider: the https JWKS endpoint behind a bounded, rate-limited cache, or a static document (tests
    only). ``clock`` is injectable so the cache and refresh policy can be tested without waiting.
    """

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self._url = settings.oidc_jwks_url
        self._lifespan = settings.oidc_jwks_cache_s
        self._stale_max = settings.oidc_jwks_stale_max_s
        self._timeout = settings.oidc_fetch_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0
        self._last_attempt: float | None = None
        self._static = False
        if settings.oidc_jwks_json:
            if settings.environment != "test":
                raise RuntimeError("a static JWKS (OIDC_JWKS_JSON) is accepted only when ENVIRONMENT is 'test'")
            self._keys = PyJWKSet.from_json(settings.oidc_jwks_json)
            self._static = True
        elif not self._url.startswith("https://"):
            raise RuntimeError("OIDC_JWKS_URL must be an https URL")
        # certificate and hostname verification are always on; a private CA can only be added, never substituted
        self._tls = ssl.create_default_context(cafile=settings.oidc_ca_bundle or None)

    def _fetch(self) -> PyJWKSet:
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout, context=self._tls) as response:
                body = response.read(MAX_JWKS_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise JwksUnavailable(f"http {exc.code}") from exc
        except (OSError, ssl.SSLError, TimeoutError) as exc:  # refused, unreachable, timed out, TLS failure
            raise JwksUnavailable(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from exc
        if len(body) > MAX_JWKS_BYTES:
            raise JwksUnavailable("document too large")
        try:
            document = json.loads(body)
            if not isinstance(document, dict):
                raise TypeError("not an object")
            keys = PyJWKSet.from_dict(document)
        except (ValueError, TypeError, PyJWKError, PyJWKSetError) as exc:  # malformed JSON, not a key set, no key
            raise JwksUnavailable(f"invalid JWKS document ({type(exc).__name__})") from exc
        if not any(key.key_type in ("RSA", "EC", "OKP") for key in keys.keys):  # symmetric-only sets cannot sign for us
            raise JwksUnavailable("the JWKS holds no asymmetric key")
        return keys

    def _refresh(self, now: float) -> None:
        """One attempt; on failure the last keys stay in service for at most stale_max seconds, then nothing does."""
        self._last_attempt = now
        try:
            self._keys, self._fetched_at = self._fetch(), now
        except JwksUnavailable as exc:
            age = now - self._fetched_at
            if self._keys is not None and age <= self._stale_max:
                LOG.warning("JWKS refresh failed (%s); serving the keys fetched %.0f s ago", exc, age)
                return
            LOG.warning("JWKS unavailable (%s) and no key set to fall back on", exc)
            raise

    def _match(self, kid: str) -> PyJWK | None:
        for key in self._keys.keys if self._keys else []:
            if key.key_id == kid:
                return key
        return None

    def _may_attempt(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= MIN_REFRESH_INTERVAL_S

    def key_for(self, kid: str) -> PyJWK | None:
        """The signing key with this id: refreshed when the cache expired or the id is unknown, at most once per
        MIN_REFRESH_INTERVAL_S; JwksUnavailable when no usable key set exists."""
        if self._static:
            return self._match(kid)
        with self._lock:
            now = self._clock()
            expired = self._keys is None or now - self._fetched_at > self._lifespan
            if expired and self._may_attempt(now):
                self._refresh(now)
            elif self._keys is None:
                raise JwksUnavailable("provider unavailable; next attempt after the refresh interval")
            found = self._match(kid)
            if found is None and self._may_attempt(now):
                self._refresh(now)  # key rotation: the id may have been published since the last fetch
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
    except JwksUnavailable as exc:
        LOG.warning("identity provider keys unavailable: %s", exc)
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

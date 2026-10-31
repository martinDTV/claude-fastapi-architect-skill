# Security and configuration

## Contents
- Settings
- Secrets
- Authentication
- Authorization
- CORS and browser-facing concerns
- Input, output and data protection
- Dependency hygiene
- Security checklist

## Settings

- A single `Settings(BaseSettings)` class in `core/config.py`, accessed through `@lru_cache get_settings()`. Example in `project-structure.md`.
- Every value has a type. Required values have no default, so a missing variable fails at startup instead of at the first request.
- `.env` is for local development only and is git-ignored. Commit `.env.example` listing every variable with safe placeholder values.
- An `environment` field (`local`, `test`, `staging`, `production`) drives behavior differences, such as disabling `/docs` in production. Avoid scattered `if DEBUG` checks.
- For large configs, group with nested models and `env_nested_delimiter="__"` (`DATABASE__POOL_SIZE`).
- Pass the needed settings values into services and adapters at construction time in `dependencies.py` or `lifespan`, so business code does not depend on the global settings object.

## Secrets

- Typed as `SecretStr`; read with `.get_secret_value()` only where the secret is used. This keeps secrets out of logs and reprs.
- In deployed environments, come from the platform's secret manager (Kubernetes Secrets with external-secrets, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault) injected as environment variables or files. Never baked into images or committed.
- Rotate by supporting two valid values during the rotation window where possible (for example, two JWT verification keys).

## Authentication

Prefer delegating identity to a dedicated provider (an OIDC identity provider such as Keycloak, Auth0, Entra ID, Cognito, Firebase Auth). The API then only verifies tokens.

```python
# core/security.py (OIDC access-token verification)
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _get_jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(get_settings().oidc_jwks_url, cache_keys=True)


class Principal:
    def __init__(self, subject: str, roles: frozenset[str]) -> None:
        self.subject = subject
        self.roles = roles


def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    settings = get_settings()
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    return Principal(subject=claims["sub"], roles=frozenset(claims.get("roles", [])))


CurrentPrincipalDep = Annotated[Principal, Depends(get_current_principal)]
```

This assumes three extra fields in `Settings`: `oidc_issuer: str`, `oidc_audience: str` and `oidc_jwks_url: str` (replace `jwt_secret` from the base example when tokens come from an identity provider).

The design engine (v2.0, increment 5) generates this module from `security.oidc` in a schema 1.2 contract, with a few additions worth copying by hand: the header algorithm is checked against the contract's asymmetric list before any key is looked up, the JWKS cache refreshes at most once per unknown `kid` and never more often than every 30 s (a forged `kid` cannot force a fetch per request), the JWKS URL must be https and a static JWKS is accepted only in the test environment, `exp`/`nbf`/`iat`/`iss`/`aud`/subject are required claims, a provider that cannot be reached is 503 rather than 401, and the generated `tests/test_auth.py` keeps the negative controls (algorithm confusion, unsigned token, altered claims, rotated key, wrong audience or issuer). See `references/design-contracts.md`, section 8.

`get_current_principal` is a plain `def` because `PyJWKClient` fetches keys with a blocking HTTP call on cache miss; FastAPI runs it in the threadpool.

Rules:
- Always pin `algorithms`, and always verify `aud`, `iss` and `exp`. Never accept `none` or let the token header choose the algorithm.
- Short-lived access tokens (minutes). Refresh tokens are the identity provider's concern, not the API's.
- If the API must issue its own tokens (no external provider): hash passwords with Argon2 through `pwdlib` (passlib is unmaintained), sign with an asymmetric key (RS256/EdDSA) when other services verify tokens, and rate-limit login endpoints.
- Service-to-service calls use their own credentials (client credentials flow, workload identity or mTLS), not forwarded user tokens with extended lifetimes.

## Authorization

- Authentication answers *who*; authorization answers *may they do this to this resource*. Both are required on every non-public endpoint.
- Coarse checks (role) as dependencies:

```python
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.security import CurrentPrincipalDep, Principal


def require_role(role: str) -> Callable[[Principal], Principal]:
    def checker(principal: CurrentPrincipalDep) -> Principal:
        if role not in principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN)
        return principal

    return checker


router = APIRouter(dependencies=[Depends(require_role("admin"))])
```

- Fine-grained checks (ownership, tenant, state) in the service, raising `PermissionDeniedError`, because they depend on data the service loads.
- Return 404 instead of 403 when revealing the existence of a resource is itself a leak.
- Every query on tenant-scoped data filters by tenant. Add a test proving a user of tenant A cannot read tenant B.

## CORS and browser-facing concerns

- CORS only when a browser client on another origin calls the API. List explicit origins from settings. `allow_origins=["*"]` must never be combined with `allow_credentials=True`: browsers reject it and it signals that the configuration was not thought through.
- Cookie-based sessions require `Secure`, `HttpOnly`, `SameSite` and CSRF protection. Bearer tokens in the `Authorization` header avoid CSRF but must not be stored where XSS can read them on the client.
- Security headers (`Strict-Transport-Security`, `X-Content-Type-Options: nosniff`) are usually set at the gateway or ingress; add a middleware only if there is no gateway.
- Rate limiting belongs at the gateway or ingress. If it must live in the app, use a shared store (Redis), not per-process memory.

## Input, output and data protection

- Validate everything at the edge with schemas: bounded string lengths, bounded list sizes, numeric ranges. Unbounded input is a denial-of-service vector.
- File uploads: limit size, check content type by inspecting bytes, store outside the web root (object storage), never trust the client filename.
- SQL only through SQLAlchemy constructs or bound parameters. Never format user input into SQL text.
- Response schemas list exactly the fields to expose. Never return ORM objects without a schema; that is how password hashes and internal flags leak.
- Do not log tokens, passwords, full card numbers or personal data. Log identifiers.
- Outbound HTTP to user-supplied URLs (webhooks, imports) needs an allowlist or SSRF protection.

## Dependency hygiene

- Lockfile committed (`uv.lock`); builds use `uv sync --frozen`.
- Automated update PRs (Dependabot or Renovate) and a vulnerability scan in CI (`pip-audit` or the platform's scanner).
- Container images from slim official bases, rebuilt regularly, running as a non-root user.

## Security checklist

- [ ] All non-public routes depend on the principal dependency
- [ ] Authorization checked for every resource access, including tenant isolation tests
- [ ] JWT verification pins algorithms and checks audience, issuer and expiry
- [ ] Secrets typed as `SecretStr`, sourced from a secret manager in deployed environments
- [ ] CORS origins explicit
- [ ] `/docs` and `/openapi.json` disabled or protected in production if the API is not public
- [ ] Input sizes bounded; uploads limited and validated
- [ ] No sensitive data in logs or error responses
- [ ] Dependency scanning in CI

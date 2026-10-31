"""Test fixtures: one application per test with its own in-memory state. Identities: alice (tenant A, every role), bob
(tenant A, member roles), admin and limited (tenant A) when the policy declares them, carol (tenant B).
"""

import json
import time
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from team_notes.core.config import Settings
from team_notes.main import create_app

TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
# name -> (tenant, roles); the credentials of each identity come from credentials(name)
IDENTITIES: dict[str, tuple[UUID, list[str]]] = {
    "alice": (TENANT_A, ["admin", "editor", "viewer"]),
    "bob": (TENANT_A, ["editor", "viewer"]),
    "admin": (TENANT_A, ["admin"]),
    "limited": (TENANT_A, []),
    "carol": (TENANT_B, ["admin", "editor", "viewer"]),
}
ISSUER = "https://login.example.com/"
AUDIENCE = "team-notes-api"
ALGORITHM = "RS256"
TENANT_CLAIM = "workspace_id"
ROLES_CLAIM = "roles"
SUBJECT_CLAIM = "sub"
KID = "test-key-1"


def new_private_key():
    """A fresh signing key of the contract algorithm family (the fake provider of tests/test_jwks.py rotates them)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": ALGORITHM, "use": "sig"})
    return jwk


PRIVATE_KEY = new_private_key()  # one key pair per session


def jwks_json() -> str:
    return json.dumps({"keys": [public_jwk(PRIVATE_KEY, KID)]})


def mint(
    identity: str = "alice",
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_in: int = 300,
    not_before: int = -5,
    kid: str | None = KID,
    algorithm: str = ALGORITHM,
    key: object = PRIVATE_KEY,
    overrides: dict[str, object] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    """A token for an identity, with the knobs the negative tests need (issuer, expiry, key, missing claims)."""
    tenant, roles = IDENTITIES[identity]
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        SUBJECT_CLAIM: identity,
        "iat": now - 5,
        "nbf": now + not_before,
        "exp": now + expires_in,
        TENANT_CLAIM: str(tenant),
        ROLES_CLAIM: roles,
    }
    claims.update(overrides or {})
    for name in drop:
        claims.pop(name, None)
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


def credentials(identity: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(identity)}"}


@pytest.fixture
def app():
    settings = Settings(app_name="test", environment="test", oidc_jwks_json=jwks_json())
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return credentials("alice")


@pytest.fixture
def auth():
    """credentials(identity): the headers of alice, bob, admin, limited or carol."""
    return credentials


@pytest.fixture
def tenant() -> UUID:
    """The tenant of alice and bob (the default headers); carol belongs to TENANT_B."""
    return TENANT_A

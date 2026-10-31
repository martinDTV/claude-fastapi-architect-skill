"""Authentication tests: what the OIDC validation refuses (issuer https://login.example.com/, audience team-notes-api,
algorithms RS256) and how failures are told apart: 401 for a bad token, 403 for another tenant, 503 when the provider
cannot be reached.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from team_notes.core.config import Settings
from team_notes.main import create_app
from tests.conftest import (
    AUDIENCE,
    ISSUER,
    KID,
    PRIVATE_KEY,
    ROLES_CLAIM,
    SUBJECT_CLAIM,
    TENANT_A,
    TENANT_CLAIM,
    jwks_json,
    mint,
)

URL = f"/api/v1/tenants/{TENANT_A}/notes"


def call(client, headers: dict[str, str]):
    return client.get(URL, headers=headers)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_a_valid_token_is_accepted(client, headers):
    assert call(client, headers).status_code == 200


def test_missing_or_non_bearer_authorization_is_401_with_a_challenge(client):
    response = call(client, {})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert call(client, {"Authorization": "Basic YWxpY2U6c2VjcmV0"}).status_code == 401


def test_a_token_with_altered_claims_keeps_its_signature_but_is_rejected(client):
    header, payload, signature = mint("bob").split(".")
    claims = claims_of(f"{header}.{payload}.{signature}")
    claims[SUBJECT_CLAIM] = "alice"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    assert call(client, bearer(f"{header}.{forged}.{signature}")).status_code == 401


def test_an_expired_token_is_rejected_beyond_the_leeway(client):
    assert call(client, bearer(mint(expires_in=-120))).status_code == 401


def test_a_token_not_yet_valid_is_rejected(client):
    assert call(client, bearer(mint(not_before=600))).status_code == 401


def test_wrong_audience_or_issuer_is_rejected(client):
    assert call(client, bearer(mint(audience="another-api"))).status_code == 401
    assert call(client, bearer(mint(issuer="https://evil.example.com/"))).status_code == 401


def test_a_symmetric_signature_with_the_public_key_is_rejected(client):
    """Algorithm confusion: HS256 with the provider public key as the secret must never verify."""
    encoding, fmt = serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    public_pem = PRIVATE_KEY.public_key().public_bytes(encoding, fmt)
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, SUBJECT_CLAIM: "alice", "iat": now, "exp": now + 300}
    # PyJWT itself refuses to sign HS256 with a PEM, so the forged token is assembled by hand
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    signing_input = f"{header}.{b64(json.dumps(claims).encode())}"
    signature = b64(hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest())
    assert call(client, bearer(f"{signing_input}.{signature}")).status_code == 401


def test_an_unsigned_token_is_rejected(client):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, SUBJECT_CLAIM: "alice", "iat": now, "exp": now + 300}
    token = jwt.encode(claims, None, algorithm="none", headers={"kid": KID})
    assert call(client, bearer(token)).status_code == 401


def test_an_unknown_key_id_or_a_missing_key_id_is_rejected(client):
    assert call(client, bearer(mint(kid="rotated-away"))).status_code == 401
    assert call(client, bearer(mint(kid=None))).status_code == 401


def test_a_token_signed_by_another_key_with_the_same_key_id_is_rejected(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert call(client, bearer(mint(key=other))).status_code == 401


def test_malformed_tokens_are_rejected(client):
    for token in ("not-a-jwt", "a.b", "a.b.c", ""):
        assert call(client, bearer(token)).status_code == 401, token


def test_missing_required_claims_are_rejected(client):
    for claim in ("exp", "iat", "iss", "aud", SUBJECT_CLAIM):
        assert call(client, bearer(mint(drop=(claim,)))).status_code == 401, claim


def test_a_malformed_roles_claim_is_rejected(client):
    assert call(client, bearer(mint(overrides={ROLES_CLAIM: 5}))).status_code == 401
    assert call(client, bearer(mint(overrides={ROLES_CLAIM: [1, 2]}))).status_code == 401


def test_roles_may_come_as_a_space_separated_string(client):
    roles = " ".join(claims_of(mint())[ROLES_CLAIM])
    assert call(client, bearer(mint(overrides={ROLES_CLAIM: roles}))).status_code == 200


def test_a_token_without_a_usable_tenant_claim_is_401(client):
    assert call(client, bearer(mint(drop=(TENANT_CLAIM,)))).status_code == 401
    assert call(client, bearer(mint(overrides={TENANT_CLAIM: "not-a-uuid"}))).status_code == 401


def test_a_member_of_another_tenant_is_403_not_401(client):
    response = call(client, bearer(mint("carol")))
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")


def test_a_static_jwks_is_refused_outside_the_test_environment():
    with pytest.raises(RuntimeError, match="static JWKS"):
        create_app(Settings(app_name="x", environment="local", oidc_jwks_json=jwks_json()))


def test_a_non_https_jwks_url_is_refused():
    with pytest.raises(RuntimeError, match="https"):
        create_app(Settings(app_name="x", oidc_jwks_url="http://issuer.example.com/jwks.json"))


def test_an_unreachable_provider_is_503_not_401():
    app = create_app(Settings(app_name="x", oidc_jwks_url="https://127.0.0.1:9/jwks.json"))
    with TestClient(app) as client:
        response = call(client, bearer(mint()))
    assert response.status_code == 503, response.text

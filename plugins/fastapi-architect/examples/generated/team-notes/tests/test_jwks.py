"""The JWKS client against a local HTTPS identity provider: a certificate authority created for the session signs the
provider certificate and is trusted only through oidc_ca_bundle (certificate and hostname verification stay on); the
provider serves whatever document the test asks for, counts its hits, and rotates keys. Every failure of the provider
is 503 without a WWW-Authenticate challenge; every failure of a token is 401 with one.
"""

import base64
import datetime
import ipaddress
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from team_notes.core.config import Settings
from team_notes.core.security import MAX_JWKS_BYTES, MIN_REFRESH_INTERVAL_S, JwksSource, JwksUnavailable
from team_notes.main import create_app
from tests.conftest import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    ROLES_CLAIM,
    SUBJECT_CLAIM,
    TENANT_A,
    TENANT_CLAIM,
    new_private_key,
    public_jwk,
)

URL = f"/api/v1/tenants/{TENANT_A}/notes"
TENANT_VALUE = str(TENANT_A)


def _certificate(subject: str, key, issuer_name=None, issuer_key=None, *, ca: bool, san: bool = False):
    """A certificate signed by issuer_key (self-signed when None): the session CA, or the provider leaf with SANs."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=not ca,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if issuer_key is not None:  # strict verifiers want the authority key identifier and server-auth usage on a leaf
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key())
        builder = builder.add_extension(aki, critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    if san:
        names = [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return builder.sign(issuer_key or key, hashes.SHA256())


def _pem(obj) -> bytes:
    if hasattr(obj, 'public_bytes'):
        return obj.public_bytes(serialization.Encoding.PEM)
    fmt, enc = serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    return obj.private_bytes(serialization.Encoding.PEM, fmt, enc)


class Provider:
    """The fake identity provider: keys by kid, a response mode, a hit counter, an HTTPS server on 127.0.0.1."""

    def __init__(self, directory: Path) -> None:
        self.keys = {"k1": new_private_key()}
        self.mode = "ok"
        self.delay = 0.0
        self.hits = 0
        self.lock = threading.Lock()
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_cert = _certificate("test provider CA", ca_key, ca=True)
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf_cert = _certificate("127.0.0.1", leaf_key, ca_cert.subject, ca_key, ca=False, san=True)
        self.ca_path = directory / "provider-ca.pem"
        self.ca_path.write_bytes(_pem(ca_cert))
        cert_path, key_path = directory / "provider.pem", directory / "provider-key.pem"
        cert_path.write_bytes(_pem(leaf_cert))
        key_path.write_bytes(_pem(leaf_key))
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_ca_path = directory / "other-ca.pem"
        self.other_ca_path.write_bytes(_pem(_certificate("another CA", other_key, ca=True)))
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                provider.respond(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}/jwks.json"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def document(self) -> bytes:
        return json.dumps({"keys": [public_jwk(key, kid) for kid, key in self.keys.items()]}).encode()

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        with self.lock:
            self.hits += 1
            mode, delay = self.mode, self.delay
        if delay:
            time.sleep(delay)
        bodies = {
            "ok": (200, self.document()),
            "malformed": (200, b"{not json"),
            "invalid": (200, json.dumps({"keys": [{"kty": "oct", "k": "AAAA"}]}).encode()),
            "list": (200, b"[1, 2, 3]"),
            "error": (500, b"boom"),
            "missing": (404, b"not here"),
            "huge": (200, b"{\"keys\": [], \"pad\": \"" + b"x" * (MAX_JWKS_BYTES + 1) + b"\"}"),
        }
        status, body = bodies[mode]
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope="module")
def provider(tmp_path_factory):
    p = Provider(tmp_path_factory.mktemp("provider"))
    yield p
    p.close()


@pytest.fixture
def reset(provider):
    provider.keys = {"k1": new_private_key()}
    provider.mode = "ok"
    provider.delay = 0.0
    provider.hits = 0
    return provider


def make_client(provider, clock, **overrides):
    """An application whose JWKS source reads the fake provider over HTTPS through the session CA and a fake clock."""
    kwargs = {"app_name": "jwks", "oidc_jwks_url": provider.url, "oidc_ca_bundle": str(provider.ca_path)}
    kwargs["oidc_fetch_timeout_s"] = 0.5
    kwargs.update(overrides)
    app = create_app(Settings(**kwargs))
    app.state.jwks = JwksSource(app.state.settings, clock=clock)
    client = TestClient(app)
    client.__enter__()
    return client


def token(provider, kid: str, key=None, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        SUBJECT_CLAIM: "alice",
        "iat": now - 5,
        "exp": now + 300,
        ROLES_CLAIM: ["admin", "editor", "viewer", "member", "moderator", "writer", "dispatcher"],
    }
    if TENANT_VALUE is not None:
        claims[TENANT_CLAIM] = TENANT_VALUE
    claims.update(overrides)
    return jwt.encode(claims, key or provider.keys[kid], algorithm=ALGORITHM, headers={"kid": kid})


def call(client, bearer: str):
    headers = {"Authorization": f"Bearer {bearer}"}
    return client.get(URL, headers=headers)


def test_keys_are_fetched_over_https_and_a_valid_signature_is_accepted(reset):
    client = make_client(reset, Clock())
    assert call(client, token(reset, "k1")).status_code == 200
    assert reset.hits == 1


def test_the_cache_serves_repeated_requests_without_refetching(reset):
    client = make_client(reset, Clock())
    for _ in range(5):
        assert call(client, token(reset, "k1")).status_code == 200
    assert reset.hits == 1


def test_a_rotated_key_is_fetched_once_the_refresh_interval_has_passed(reset):
    clock = Clock()
    client = make_client(reset, clock)
    assert call(client, token(reset, "k1")).status_code == 200
    reset.keys["k2"] = new_private_key()
    assert call(client, token(reset, "k2")).status_code == 401  # just fetched: the unknown id is not fetched again yet
    clock.advance(MIN_REFRESH_INTERVAL_S)
    assert call(client, token(reset, "k2")).status_code == 200
    assert reset.hits == 2


def test_unknown_key_ids_cannot_force_a_fetch_per_request(reset):
    clock = Clock()
    client = make_client(reset, clock)
    assert call(client, token(reset, "k1")).status_code == 200
    clock.advance(MIN_REFRESH_INTERVAL_S)
    for i in range(10):
        forged = token(reset, "k1").split(".")
        assert call(client, _with_kid(forged, f"kid-{i}")).status_code == 401
    assert reset.hits == 2  # one refresh for the first unknown id, none for the other nine


def _with_kid(parts: list, kid: str) -> str:
    """The same token with another kid in its header (the signature no longer matters: the key is unknown)."""
    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    header["kid"] = kid
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    return ".".join([encoded, parts[1], parts[2]])


def test_a_key_replaced_under_the_same_id_is_rejected_until_the_cache_expires(reset):
    clock = Clock()
    client = make_client(reset, clock, oidc_jwks_cache_s=120)
    assert call(client, token(reset, "k1")).status_code == 200
    reset.keys["k1"] = new_private_key()
    assert call(client, token(reset, "k1")).status_code == 401  # the cached key no longer matches
    clock.advance(121)
    assert call(client, token(reset, "k1")).status_code == 200
    assert reset.hits == 2


def test_a_removed_key_is_rejected_after_the_cache_expires(reset):
    clock = Clock()
    client = make_client(reset, clock, oidc_jwks_cache_s=120)
    old = token(reset, "k1")
    assert call(client, old).status_code == 200
    reset.keys = {"k2": new_private_key()}
    clock.advance(121)
    assert call(client, old).status_code == 401
    assert call(client, token(reset, "k2")).status_code == 200


@pytest.mark.parametrize("mode", ["malformed", "invalid", "list", "error", "missing", "huge"])
def test_an_unusable_or_failing_provider_is_503_not_401(reset, mode):
    reset.mode = mode
    client = make_client(reset, Clock())
    response = call(client, token(reset, "k1"))
    assert response.status_code == 503, (mode, response.text)
    assert "www-authenticate" not in response.headers
    assert response.json()["detail"] == "identity provider keys are unavailable"


def test_a_slow_provider_times_out_as_503(reset):
    reset.delay = 2.0
    client = make_client(reset, Clock())
    started = time.monotonic()
    assert call(client, token(reset, "k1")).status_code == 503
    assert time.monotonic() - started < 1.9  # the 0.5 s fetch timeout, not the 2 s delay


def test_tls_verification_is_never_disabled(reset):
    assert call(make_client(reset, Clock(), oidc_ca_bundle=""), token(reset, "k1")).status_code == 503  # system store
    other = make_client(reset, Clock(), oidc_ca_bundle=str(reset.other_ca_path))
    assert call(other, token(reset, "k1")).status_code == 503  # a CA that did not sign the provider certificate
    assert reset.hits == 0  # the handshake failed before any request reached the provider


def test_outage_without_a_cache_is_503_and_the_provider_is_retried_after_the_interval(reset):
    reset.mode = "error"
    clock = Clock()
    client = make_client(reset, clock)
    assert call(client, token(reset, "k1")).status_code == 503
    assert call(client, token(reset, "k1")).status_code == 503
    assert reset.hits == 1  # the second failure is answered without hammering the provider
    reset.mode = "ok"
    clock.advance(MIN_REFRESH_INTERVAL_S)
    assert call(client, token(reset, "k1")).status_code == 200


def test_a_warm_cache_survives_an_outage_for_the_stale_window_only(reset):
    clock = Clock()
    client = make_client(reset, clock, oidc_jwks_cache_s=60, oidc_jwks_stale_max_s=600)
    assert call(client, token(reset, "k1")).status_code == 200
    reset.mode = "error"
    clock.advance(61)
    assert call(client, token(reset, "k1")).status_code == 200  # stale keys, provider failing
    clock.advance(600)
    assert call(client, token(reset, "k1")).status_code == 503  # beyond the stale window nothing is served


def test_concurrent_cold_starts_fetch_once(reset):
    source = JwksSource(make_client(reset, Clock()).app.state.settings, clock=Clock())
    results: list = []

    def worker() -> None:
        try:
            results.append(source.key_for("k1") is not None)
        except JwksUnavailable as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 8
    assert reset.hits == 1

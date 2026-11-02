"""The JWKS fixture of generated projects encodes EC public keys with fixed-length coordinates (v2.2.0 acceptance fix).

PyJWT 2.9's ``ECAlgorithm.to_jwk`` dropped a leading zero byte of ``x`` or ``y`` for about one P-256 key in a hundred
(the integer was encoded minimally); its own ``PyJWKSet`` then refused the key, the generated client correctly fell
back to the stale keys it had, and the generated ``tests/test_jwks.py`` failed intermittently. The generated
``public_jwk`` now builds the JWK from the public numbers with the curve's byte length, so a leading zero is kept
whatever the installed PyJWT. These tests are deterministic: private scalars whose public coordinates start with a
zero byte on each supported curve (P-256, P-384, P-521), a round trip through PyJWT with those keys, a sample of random
keys, the unchanged RSA path, and the dependency declaration (`pyjwt[crypto]>=2.10.1,<3`).
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import sys
import unittest
from typing import Any, Dict

from _audit_support import FIXTURES, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402

HAS_CRYPTO = all(importlib.util.find_spec(m) is not None for m in ("cryptography", "jwt"))
SPEC = FIXTURES / "design20" / "specs" / "valid" / "oidc_owner_scoped.json"
# private scalars whose public point has a leading zero byte in the named coordinate (found once, checked by the test)
LEADING_ZERO = {"ES256": ("P-256", 32, {"y": 43, "x": 379}), "ES384": ("P-384", 48, {"y": 176, "x": 197}), "ES512": ("P-521", 66, {"x": 1, "y": 2})}


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _spec(algorithm: str):
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    doc["security"]["oidc"]["algorithms"] = [algorithm]
    result = design.validate_spec(doc)
    spec = result[0] if isinstance(result, tuple) else result
    assert spec is not None, result
    return spec


def _fixture(algorithm: str) -> Dict[str, Any]:
    """The generated ``tests/conftest.py`` of a project with this algorithm, and the executable pieces of its JWKS
    helpers (the module itself imports the generated application, which is not on the path here)."""
    spec = _spec(algorithm)
    blueprint = design.build(spec, design.recommend(spec))
    source = blueprint.files["tests/conftest.py"]
    tree = ast.parse(source)
    wanted = {"_b64url", "public_jwk", "new_private_key", "ALGORITHM"}
    nodes = [n for n in tree.body if (isinstance(n, (ast.FunctionDef, ast.Assign)) and ({n.name} if isinstance(n, ast.FunctionDef) else {t.id for t in n.targets if isinstance(t, ast.Name)}) & wanted)]
    namespace: Dict[str, Any] = {}
    exec("import base64, json\nfrom cryptography.hazmat.primitives.asymmetric import ec, rsa, ed25519\nfrom jwt.algorithms import RSAAlgorithm, OKPAlgorithm", namespace)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f"<conftest {algorithm}>", "exec"), namespace)
    return {"source": source, "pyproject": blueprint.files["pyproject.toml"], **namespace}


@unittest.skipUnless(HAS_CRYPTO, "cryptography and PyJWT are needed to exercise the generated fixture")
class FixedLengthCoordinateTests(unittest.TestCase):
    def test_keys_with_a_leading_zero_byte_keep_the_curve_length_on_every_supported_curve(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1()}
        for algorithm, (crv, size, scalars) in LEADING_ZERO.items():
            fixture = _fixture(algorithm)
            self.assertEqual(fixture["ALGORITHM"], algorithm)
            for coordinate, scalar in scalars.items():
                key = ec.derive_private_key(scalar, curves[crv])
                numbers = key.public_key().public_numbers()
                value = getattr(numbers, coordinate)
                self.assertLess(value.bit_length(), size * 8 - 7, f"{algorithm} scalar {scalar}: {coordinate} does not start with a zero byte any more")
                jwk = fixture["public_jwk"](key, "zero")
                self.assertEqual((jwk["kty"], jwk["crv"], jwk["alg"], jwk["use"], jwk["kid"]), ("EC", crv, algorithm, "sig", "zero"))
                for name in ("x", "y"):
                    raw = _b64decode(jwk[name])
                    self.assertEqual(len(raw), size, f"{algorithm} {name}: {len(raw)} bytes, the curve needs {size}")
                    self.assertNotIn("=", jwk[name], "base64url without padding")
                self.assertEqual(_b64decode(jwk[coordinate])[0], 0, f"{algorithm} {coordinate}: the leading zero byte is kept")
                self.assertEqual(int.from_bytes(_b64decode(jwk[coordinate]), "big"), value)

    def test_round_trip_through_pyjwt_with_the_leading_zero_keys(self) -> None:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import ec
        from jwt import PyJWK, PyJWKSet

        curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1()}
        for algorithm, (crv, _size, scalars) in LEADING_ZERO.items():
            fixture = _fixture(algorithm)
            for scalar in scalars.values():
                key = ec.derive_private_key(scalar, curves[crv])
                jwk = fixture["public_jwk"](key, f"k{scalar}")
                key_set = PyJWKSet.from_dict({"keys": [jwk]})
                self.assertEqual([k.key_id for k in key_set.keys], [f"k{scalar}"], "the set parses and keeps the key")
                token = jwt.encode({"sub": "alice", "aud": "a"}, key, algorithm=algorithm, headers={"kid": f"k{scalar}"})
                claims = jwt.decode(token, PyJWK.from_dict(jwk).key, algorithms=[algorithm], audience="a")
                self.assertEqual(claims["sub"], "alice")
                with self.assertRaises(jwt.InvalidSignatureError):  # another key of the same curve does not verify it
                    jwt.decode(token, PyJWK.from_dict(fixture["public_jwk"](fixture["new_private_key"](), "other")).key, algorithms=[algorithm], audience="a")

    def test_random_keys_always_encode_to_the_curve_length(self) -> None:
        from jwt import PyJWKSet

        for algorithm, (_crv, size, _scalars) in LEADING_ZERO.items():
            fixture = _fixture(algorithm)
            for _ in range(150):
                jwk = fixture["public_jwk"](fixture["new_private_key"](), "r")
                self.assertEqual((len(_b64decode(jwk["x"])), len(_b64decode(jwk["y"]))), (size, size))
                PyJWKSet.from_dict({"keys": [jwk]})

    def test_the_rsa_path_is_unchanged_and_still_parses(self) -> None:
        from jwt import PyJWKSet

        fixture = _fixture("RS256")
        self.assertIn("RSAAlgorithm.to_jwk(private_key.public_key())", fixture["source"])
        self.assertNotIn("_b64url", fixture["source"])
        jwk = fixture["public_jwk"](fixture["new_private_key"](), "rsa")
        self.assertEqual((jwk["kty"], jwk["alg"]), ("RSA", "RS256"))
        self.assertEqual(len(PyJWKSet.from_dict({"keys": [jwk]}).keys), 1)

    def test_the_generated_fixture_and_dependency_declare_what_they_need(self) -> None:
        for algorithm in ("ES256", "ES384", "ES512"):
            fixture = _fixture(algorithm)
            self.assertIn("import base64", fixture["source"])
            self.assertNotIn("from jwt.algorithms import ECAlgorithm", fixture["source"], "the fixture no longer imports PyJWT's EC serialiser")
            self.assertNotIn("ECAlgorithm.to_jwk(", fixture["source"].split("def public_jwk")[1].split("return jwk")[0].replace("ECAlgorithm.to_jwk dropped", ""), "and no longer calls it")
            self.assertIn('"pyjwt[crypto]>=2.10.1,<3"', fixture["pyproject"])
        self.assertIn('"pyjwt[crypto]>=2.10.1,<3"', _fixture("RS256")["pyproject"])
        inventory = design.load_spec(str(FIXTURES.parent.parent / "plugins" / "fastapi-architect" / "examples" / "specs" / "inventory-service.json"))
        self.assertNotIn("pyjwt", design.build(inventory, design.recommend(inventory)).files["pyproject.toml"], "projects without OIDC do not depend on PyJWT")


if __name__ == "__main__":
    unittest.main()

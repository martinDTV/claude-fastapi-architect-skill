# Guarantees regression fixtures (v2.0 security hardening)

One file per example or fixture contract with the exact `guarantees` block the design report must produce for it
(`fastapi_design.security_guarantees`). `tests/test_design_v20_security.py` compares the live block with the file, so any
change in what the engine claims, or stops claiming, is a deliberate edit of the fixture reviewed with the code. The
`not_claimed` lists are the contract of honesty: nothing a generated project does not demonstrate may disappear from
them without a test that demonstrates it.

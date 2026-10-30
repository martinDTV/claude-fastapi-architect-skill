# remediation19 — fixture for the verified remediation pipeline (v1.9)

`shop/service.py` holds three defects with an automatic recipe (`TX_COMMIT_IN_LOOP` per item,
`TX_COMMIT_INSIDE_BEGIN_BLOCK`, `SA_ASYNC_SESSION_NOT_AWAITED`) and one correct control.
`tests/test_defects.py` observes each defect against `tests/fakes.py`, a fake AsyncSession faithful to the
semantics the recipes rely on: the suite **fails on this code** (three of five tests) and **passes once the
recipes have been applied** in the isolated copy. The pipeline uses it as its reproduction and verification:
`python3 -m unittest discover -s tests -t . -q` is the authorized command in `tests/test_audit_v19.py`.
The fixture is never modified by the pipeline; `tests/test_audit_v19.py` asserts its digest.

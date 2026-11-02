# FastAPI Architect v2.2.0 — acceptance report (release candidate, 2026-11-01)

Scope: the six increments of v2.2.0 reviewed as one system, on top of the published v2.1.0
(`7aff2b1b04604acfaedc79c7ea3e98c1f1b08b07`): foundations (increment 0), safe column renames (1), retire and the
destructive contract with its acceptance hardening (2), the opt-in three-way merge (3), adopted projects II (4),
recovery and concurrency hardening (5) and this acceptance (6). Everything below was executed on 2026-11-01 on macOS
(arm64), Python 3.13.5 and 3.9.6, PostgreSQL 16.10 throwaway clusters, Docker 29.5.2, `uv` 0.11.28. Nothing was
pushed; no consumer repository was touched; no user database was used.

## 1. Integrity

| Check | Result |
|---|---|
| HEAD at the start of the acceptance | `53c7c41`, 29 linear commits over `origin/main` = `7aff2b1`, no merge commit, one identity (`martinDTV`), no `Co-Authored-By`, clean tree |
| Consumer repositories | `plataforma-citas-medicas` and `agentforge-notify` clean and untouched |
| Pending operations and temporaries in the repository | none outside the frozen compatibility fixture `tests/fixtures/compat/operation-1.0` (a journal kept on purpose) |
| Baseline before any change of increment 6 | 792 tests on 3.13: one failure, `test_design_v20_security …single_tenant_journal_passes_its_generated_suite`, in the generated project's `tests/test_jwks.py`. First classified as load-sensitive timing; the pre-publication review (section 9) showed it was neither timing nor load: a reproducible defect of the generated test fixture under PyJWT 2.9.0, fixed after the acceptance and before the publication |

## 2. Capability and guarantee matrix

Legend: **implemented** (the code exists), **verified** (a test executes it on the real thing), **partial** (a subset is
verified, the rest is stated), **not supported** (refused by name), **residual risk** (what the tests cannot show),
**documented guarantee** (where the promise and its limits are written). No guarantee below is wider than its test.

| Capability | Status | Verified by | Residual risk | Documented guarantee |
|---|---|---|---|---|
| Span-exact edits with an additive proof | implemented, verified | `test_spans_v22.py`, every transform of adoption | a semantic change invisible to the AST comparison (none known) | reference section 5, ADR 2.2-07 |
| Pure three-way merge (python, json), structured conflicts | implemented, verified | `test_merge_v22.py`, `test_merge_integration_v22.py` | semantic conflicts the textual merge, the header rule, the duplicate checks and the suite cannot see | ADR 2.2-05 |
| Rename expand (new column, sync trigger, transitional code, mirror backfill) | implemented, verified on PostgreSQL | `test_renames_v22_postgresql.py` (A and B side by side, hammered concurrently, locked rows, checkpoints) | a writer that bypasses the trigger (superuser `session_replication_role`) | ADR 2.2-01, guide section 10 |
| A/B/C coexistence (N-1 compatibility) | verified on PostgreSQL | `test_renames_v22_postgresql.py` test_01, `test_retire_contract_v22_postgresql.py` test_01 | application versions the tool did not generate | ADR 2.2-01 |
| Retire (truth moves to the new column; reversible) | implemented, verified | `test_retire_contract_v22_postgresql.py` test_01 | — | reference section 11 |
| Destructive contract (16 named preconditions, `--authorize-contract`, `--confirm-irreversible`, sessions, evidence age, retire age, catalogue dependents, lock timeout, `RESTRICT`) | implemented, verified | `test_retire_contract_v22_postgresql.py` test_02/03, `test_destructive_acceptance_v22_postgresql.py` (write patterns, defaults, every dependency kind, checks+lock+drop failing together and the retry, aged evidence) | **an old instance that is disconnected or hidden behind a pooler is not demonstrably retired**: the absence of incompatible sessions is necessary, never sufficient; the operator's `old_versions_stopped` is a declaration and the plan says so | reference section 11, ADR 2.2-01, `DEPLOYMENT_ASSURANCE` in the generated phase |
| Contradictory writes refused (trigger, request schemas) | verified | `test_destructive_acceptance_v22_postgresql.py` test_01 | — | ADR 2.2-01 |
| Downgrade of the destructive contract | partial | `test_retire_contract_v22_postgresql.py` (column recreated from the new one's current values, lock timeout) | the dropped content is never promised back | reference section 11 |
| Opt-in merge of integration points (`files.merge` + `--allow-merge`, authenticated BASE, ownership regions) | implemented, verified | `test_merge_integration_v22.py` (scenarios A–E, adversarial), `test_acceptance_v22.py` step 4 | a tool upgrade that changes a template makes BASE unreconstructible until regeneration (refused, not guessed) | ADR 2.2-05, guide section 10 |
| Adoption 1.1 (declared boundary, one mount point, conventions, allowed dependencies, operations, Alembic facts) | implemented, verified | `test_adopted_v22.py`, `test_adopted_v22_postgresql.py` | one mount point only; packages with absolute imports only | ADR 2.2-06, reference sections 2 and 13 |
| Generated modules inside adopted projects (in memory; PostgreSQL on the project's own Alembic) | implemented, verified (PostgreSQL round trip, downgrade, second revision) | `test_adopted_v22.py`, `test_adopted_v22_postgresql.py` | a generated module edited by hand is the developer's (no longer regenerated) | ADR 2.2-06 |
| Bounded extensions of adopted routers (typed parameters, allowed dependencies, one delegate, response model; shadowed handlers refused) | implemented, verified | `test_adopted_v22.py`, `test_acceptance_v22.py` step 5 | — | ADR 2.2-06 |
| Journal (prepare, publish, recover; durability; validation; guards; per-file re-check) | implemented, verified | `test_evolve_v20_recovery.py`, `test_foundations_v21.py`, `test_journal_v22.py`, `test_recovery_v22.py` (fault matrix) | **no global atomicity across files**; the rename itself is the window against a writer that ignores the lock; durability stops where `fsync` stops | ADR 2.2-07, reference section 6 |
| Lock (one writer, fail fast, bounded wait, statuses, other host never broken by age, reused pid) | implemented, verified with real processes | `test_journal_v22.py`, `test_concurrency_v22.py`, `test_acceptance_v22.py` step 8 | nothing can judge a process on another host; a pid start time comes from `ps` (second resolution) | ADR 2.2-07 |
| Recovery (rollback, forward, resumable, idempotent, strategy-stable, conflicts untouched, `completed` finished not re-published) | implemented, verified | `test_recovery_v22.py`, `test_recovery_v22_postgresql.py`, `test_acceptance_v22.py` steps 6 and 8 | a kill between a rename and the journal save leaves a landed file the journal does not list; classification by digest finds it | ADR 2.2-07 |
| Generation through the journal | implemented, verified | `test_recovery_v22.py` (interrupted generation rolled back to an empty target, finished forward to the identical tree) | — | ADR 2.2-07 |
| Trust model (no project code executed statically; tests only with an authorised executor) | verified | `test_evolve_v20*.py`, `test_container_v21.py`, `test_design_v20_security.py` | the local executor is process hygiene, not a sandbox | README, guide of isolated execution |
| Container executor (Docker) | verified on a real daemon | `FA_CONTAINER=1 test_container_v21.py`: 27 OK, 51.1 s, Docker 29.5.2 | the daemon, the runtime and the kernel are the trust boundary; container escapes are not protected against | ADR 2.1-03 |
| Auditor: 59 rules, fingerprints, suppressions, JSON/SARIF | verified | `test_audit_*.py`, `test_sarif_schema.py`, `test_compat_artifacts.py` | heuristics are static | references of the auditor |
| Remediation (verified patches in isolated copies) | verified | `test_audit_v18.py`, `test_audit_v19.py` | never writes a user's project | reference of remediation |
| Compatibility of historical artefacts (manifests 1.0–1.6, evolution 1.0–1.6, adoption 1.0/1.1, journal 1.0/1.1, report 1.7, evidence 1.0–1.2) | verified | `test_compat_artifacts.py`, the frozen fixtures, the per-file oracle | — | release notes of each increment |
| Not supported, by name | — | in-place renames, renames of defaulted or indexed columns, `CASCADE`, type changes, arbitrary data migrations, merges with resolution policies, several mount points, dynamic router registration, persistence without a linear Alembic chain, a second Alembic environment | — | the reference's "limits" paragraphs |

## 3. The rename cycle on PostgreSQL (A → expand → B → retire → C → contract → D)

All on throwaway PostgreSQL 16.10 clusters through the project's own `uv` environments, twice: once on the code of
increment 5 (`53c7c41` plus the reused-pid fix) and once more after the bump to 2.2.0 (the numbers below are the
second pass). No user database was used.

| Stage and control | Module | Result |
|---|---|---|
| A → expand → B: the expanded schema, the sync trigger per rename, the transitional code; A and B hammered concurrently; locked rows, drift and checkpoints of the mirror backfill; adversarial plans and schemas; a row-level-security table mirrored under the application role | `test_renames_v22_postgresql.py` | PASS: 4 OK, 46.5 s, 127 MB |
| B → retire → C: B and C coexist on the retire schema (N-1), the truth moves to the new column, the retire revision carries NOT NULL and the default online and stamps the sync function, the downgrade recreates and copies back under a lock timeout; C → contract → D only when everything holds: A and B connected are identified and refused, an unidentified session blocks, the flags alone change nothing, the phrase is printed by the plan, D verified on its own database; a contract before the retire and a retire without evidence refused on a live project | `test_retire_contract_v22_postgresql.py` | PASS: 3 OK, 82.0 s, 126 MB |
| Every write pattern on the retire schema keeps the pair or is refused (contradictory values, SQLSTATE 23514); a rename of a defaulted field refused; every catalogue dependency kind (view, index, constraint, trigger, function, generated column, foreign key) blocks the plan and makes the revision refuse with the schema intact; the checks, the lock and the drop fail together and a retry succeeds; evidence that aged between the plan and the upgrade is refused by the revision | `test_destructive_acceptance_v22_postgresql.py` | PASS: 5 OK, 128.8 s, 134 MB |
| The whole cycle on one database: A → expand → B (backfilled, new columns mirror the old ones, two triggers) → retire → C (from B's evidence) → the contract plan refused without the flags, planned with them → D applied and upgraded: `reference` and `weight_kg` gone, `label` and `weight` present, no sync trigger left, 300 rows kept | `test_acceptance_v22.py` (`AcceptancePostgresTests`) | PASS: A+expand+B 7.3 s, retire 4.5 s (evolution 1.2 s, upgrade 2.8 s), contract D 3.6 s |
| Phased expand, concurrent index, interrupted and resumed backfill (clean stop, two workers, SIGKILL mid-run), verify evidence, contract; connection failure and time limit never report success; RLS tables need a bypassing role | `test_migrations_v21_postgresql.py`, `test_acceptance_v21.py` | PASS: 4 OK, 72.1 s and 4 OK, 82.0 s |
| v2.0 PostgreSQL scenarios (generation, evolution with columns, backfill check, migration failure, RLS) | `test_acceptance_v20.py`, `test_design_v20_postgresql.py` | PASS: 3 OK, 60.9 s and 9 OK, 98.1 s |
| Adopted project with its own Alembic; interrupted phased evolution against the database state and the evidence | `test_adopted_v22_postgresql.py`, `test_recovery_v22_postgresql.py` | PASS: 2 OK, 12.7 s and 2 OK, 12.6 s |

What the cycle does not show and the plan says so: an old instance that is disconnected, or whose connections hide
behind a pooler's identity, is not demonstrably retired; the sessions check is necessary and the operator's
`old_versions_stopped` is a declaration the report records as such.

## 4. Merge and adoption

| Scenario | Result |
|---|---|
| 1. Generated project with an independent manual change and the opt-in merge | PASS: `test_merge_integration_v22.py` scenario A and `test_acceptance_v22.py` step 4 (blocked without the contract, blocked without the flag, merged with both, manual lines preserved, `noop` on repeat) |
| 2. Semantic or structural conflict refused | PASS: scenario B (same insertion point), the header rule, duplicate symbols, imports and registrations, lost comments, line endings, generator-owned regions (`test_merge_integration_v22.py`) |
| 3. Hand-written project with a module added and its router mounted | PASS: `test_adopted_v22.py` scenario A (factory and module level), `test_acceptance_v22.py` step 5 (exactly two lines added to the mount file, unadopted files byte-identical, routes resolved at their effective paths) |
| 4. Hand-written project with SQLAlchemy and Alembic | PASS on PostgreSQL: `test_adopted_v22_postgresql.py` (model on the declared base, revision after the head, upgrade, round trip, downgrade, second column revision, refusals) |
| 5. Second evolution of a generated module | PASS: `test_adopted_v22.py` (field and operation added, `noop`, a manual edit blocks regeneration), `test_adopted_v22_postgresql.py` (column revision) |
| 6. Mount file edited between plan and apply | PASS: `test_adopted_v22.py` scenario C (the writer refuses, nothing pending, re-adoption), `test_recovery_v22.py` scenario B (guards on a renamed delegate) |
| 7. Interrupted publication followed by recovery | PASS: `test_recovery_v22.py` scenarios A and B, `test_acceptance_v22.py` step 6 |
| Merge never enabled automatically by adoption | PASS: no `allow_merge` in `adoption.py` or `adopted.py`; the adopted flows edit by span and never merge |

## 5. Concurrency and recovery

| Control | Result |
|---|---|
| External change between the check and `os.replace` | PASS: re-checked immediately before each rename; a change inside the window is refused and recorded (`test_recovery_v22.py` foreign changes); the rename itself remains the window (documented) |
| Writers without the lock | PASS as a stated limit: content keeps them apart (digests), never the lock; documented in ADR 2.2-07 |
| Malformed journals and symlinks | PASS: twelve malformed documents reported, never acted upon; a symlink planted after the plan refused by publication and by recovery |
| Rollback against roll-forward | PASS: both strategies from every interruption point; the strategy sticks once a file was handled |
| `completed` with a pending cleanup | PASS: finished by the next reader; `recover` on it publishes nothing twice (`test_acceptance_v22.py` step 8) |
| Lock of another host | PASS: `unknown` whatever its age; removed only by `ops unlock --other-host NAME` |
| Reused pid | PASS (fixed in this increment): the lock record carries the holder's process start time; an alive pid with another start is `stale`, not `held` |
| Tampered staging and backups | PASS: compared with the journal before use; mismatches are conflicts |
| fsync errors | PASS: a failing file fsync leaves the destination whole and a recoverable journal; a failing directory fsync is an error, an unsupported one is reported, not fatal |
| Recovery interrupted twice | PASS: the third run finishes (`test_acceptance_v22.py` step 8) |
| Ten multiprocess cases | PASS: `test_concurrency_v22.py` 10 OK |

## 6. Trust model and Docker

Untrusted code is still refused by default: without `--authorize-execution` and an executor that accepts the profile
no project test runs (`audit_clean` states this), and nothing static imports project code (the adoption, merge and
recovery modules use `ast`, digests and the filesystem; no `exec`, `eval`, `importlib` or `subprocess` on project
code; the lock's `ps` call inspects a pid, not project code). The real Docker module ran against Docker 29.5.2:
27 OK in 52.1 s (the eight real-container tests included: the restrictions verified from the daemon's view and from inside, the preparation container, the refusal paths). Not protected and not claimed: the daemon's trust boundary, the runtime and the kernel, container escapes.

## 7. Historical compatibility

| Artefact or behaviour | Result |
|---|---|
| The 59 audit rules, identifiers, categories, schema 1.7, fingerprints, suppressions, JSON and SARIF | PASS: `RULES` is 59, `SCHEMA_VERSION` 1.7; `test_audit_*.py` and `test_sarif_schema.py` (5) green on both interpreters; SARIF validates against the vendored OASIS 2.1.0 schema |
| Holdout corpora (`tests/fixtures/truth17/holdout`, `truth18/holdout`) | PASS as frozen expectations in the suite; no rule or heuristic changed in v2.2, so nothing was tuned against them |
| Design and generation manifests 1.0 to 1.6, evolution contracts 1.0 to 1.6, adoption 1.0 and 1.1, journal 1.0 and 1.1, report 1.7, migration evidence 1.0 to 1.2 | PASS: `test_compat_artifacts.py` 13 OK; the frozen fixtures under `tests/fixtures/compat` still read and mean the same; a 1.0 adoption keeps its behaviour and its historical blocker name |
| Generated projects (per-file oracle against the published v2.1.0 engine on every specification and example evolution) | PASS: before the bump identical to the increment-5 oracle; after the bump the only difference is the tool version inside `generation.json`; the five committed snapshots regenerated with only that line changed |
| Remediation | PASS: `test_audit_v18.py`, `test_audit_v19.py`; it writes only in isolated copies |
| CLI (`audit_fastapi.py`, `design_fastapi.py`, `evolve_fastapi.py` with `discover`, `propose`, `adopt`, `plan`, `apply`, `recover`, `ops`) | PASS: every historical flag kept; additions are opt-in (`--allow-merge`, `--confirm-irreversible`, `--other-host`) |
| Python 3.9 and 3.13 | PASS: 802 OK on each (the end-to-end scenarios that need FastAPI skip on 3.9, where it is not installed) |
| Plugin installation | PASS: `claude plugin validate` passes; `plugin.json` and `marketplace.json` agree on 2.2.0 with the version constant (`validate_structure.py`) |
| Deliberate semantic changes (not regressions), each documented in its increment | a handler shadowed by an earlier parametrised route is refused (`route_not_shadowed`, increment 4); a lock of another host is `unknown`, never `stale` by age (increment 5); the operation returned by `write_recoverable` says `completed` (increment 5); `ops list` exits 1 on residual temporaries (increment 5); renames of defaulted or indexed columns refused (increment 2) |

## 8. End-to-end harness

`tests/test_acceptance_v22.py` (`AcceptanceFlowTests`, always; `AcceptancePostgresTests`, opt-in) walks design and
generation of `team-notes` from its specification, the static audit and the SARIF document, the generated suite
through the trust model, an evolution that adds a module (verified) and repeats as `noop`, the opt-in merge of a
hand edit on the integration point (refused without the contract, refused without the flag, merged with both), the
adoption of the hand-written `workshop` with a generated module mounted at the declared point and a bounded
extension, an interrupted publication rolled back and retried, the expand files of the phased rename on `parcels`
and the contract refused before the retire by name, the recovery properties of increment 5, and writes the evidence
(`acceptance-v2.2-evidence.json`, under `FA_ACCEPTANCE_EVIDENCE` when set). The PostgreSQL class runs A → expand →
B → retire → C → contract → D on one database and records the columns left after D. In-memory flow: 9 OK (34.8 s, FastAPI importable, the generated suites executed through the trust model). With `FA_DESIGN_PG=1`: 10 OK, 51.6 s. Evidence written under `/tmp` during the acceptance; the document lists per step the states, the digests of the merge, the mount statements, the guards, the classification of the interrupted publication and the rollback report.

## 9. Defects found and fixed by the acceptance

- **The JWKS fixture of generated projects (found by the pre-publication review, fixed before the publication).**
  PyJWT 2.9.0's `ECAlgorithm.to_jwk` encodes the EC coordinates `x` and `y` as minimal-length integers, so a key whose
  coordinate starts with a zero byte (about one P-256 key in a hundred) was serialised one byte short; PyJWT's own
  `PyJWKSet.from_dict` then refused the key set ("did not contain any usable keys"). The generated fixture used that
  helper to build the fake provider's document, the generated client did what it should (logged `JWKS refresh failed
  (invalid JWKS document (PyJWKSetError))` and served the keys it had, inside the stale window) and the generated
  `tests/test_jwks.py` failed whenever the rotated key drew a short coordinate: 23 and 30 of 3 000 fresh keys, 2
  failing runs of 40 of the ES256 project's `test_jwks.py` with PyJWT 2.9.0, none with 2.10.1 (which pads), none for
  RSA projects (a 2048-bit modulus never starts with a zero byte). The generated application code and the engine have
  no defect; the earlier classification as load-sensitive timing was wrong and is corrected here. Fix: the generated
  `public_jwk` of EC projects now builds the JWK from the public numbers with the curve's byte length (32, 48, 66
  bytes for P-256, P-384, P-521; base64url without padding), independent of the installed PyJWT; the generated
  dependency is `pyjwt[crypto]>=2.10.1,<3` (requires Python 3.9 or later; the generated projects require 3.12);
  RSA and OKP paths unchanged. Verified by `tests/test_jwks_fixture_v22.py` (deterministic private scalars whose
  public coordinate starts with a zero byte on each curve, a round trip through PyJWT, random samples, the RSA path,
  the dependency) and by the repeated runs of section 10. The only snapshot affected is `team-notes` (RS256):
  its `pyproject.toml` dependency line and the manifest digest of that file; no other generated file changed.

- A same-host lock whose pid the system had reused by an unrelated process was `held` and could not be removed with
  `ops unlock` (only by hand). The record now carries the holder's process start time (`process_started_at`); an
  alive pid whose start differs is `stale` (`pid_reused: true`), removable on request; records without the field use
  the lock's own start time.

## 10. Suites, environment, performance

**Verification of the JWKS fix (2026-11-02, the tree of the three fix commits; the Python under test did not change
after the chain started, only this report, the release notes and `validate_structure.py`, re-checked afterwards).**

| Run | Result |
|---|---|
| Regression under PyJWT 2.9.0 (the version that drops the byte), ES256 project generated by the fixed template, `tests/test_jwks.py` × 40 | 0 failing runs (before the fix: 2 of 40); the full generated suite 52 passed |
| Same project under PyJWT 2.10.1 in an isolated `uv` environment, `tests/test_jwks.py` × 25 and the full generated suite | 0 failing runs; 52 passed |
| `tests/test_jwks_fixture_v22.py` | 5 OK on 3.13; skipped on 3.9 (no `cryptography`/PyJWT in that interpreter) |
| `tests/test_design_v20_security.py` (the ES256 and RS256 slices run their generated suites through the trust model: rotation, stale cache, unknown `kid`, invalid signature, 401/503) | 29 OK |
| Full suite, Python 3.13.5 | 807 OK, 43 skipped, 183.3 s, 145 MB |
| Full suite, Python 3.9.6 | 807 OK, 69 skipped, 173.9 s, 177 MB |
| PostgreSQL, all ten modules on throwaway clusters (the `team-notes` snapshot changed, so everything ran again) | 46 OK: renames 4 (33.4 s), retire/contract 3 (65.2 s), destructive acceptance 5 (86.8 s), migrations v21 4 (45.9 s), acceptance v21 4 (49.6 s), acceptance v20 3 (38.5 s), design v20 9 (68.1 s), adopted 2 (8.9 s), recovery 2 (8.5 s), acceptance cycle A → D 10 (36.6 s) |
| Real Docker, `FA_CONTAINER=1 test_container_v21.py` | 27 OK, 47.4 s |
| Snapshots and oracle | only `team-notes/pyproject.toml` (the dependency line) and its digest in `generation.json`; the per-file oracle shows no other difference |
| Ruff, `validate_structure.py` (3.9 and 3.13), `claude plugin validate`, `tests/test_docs_links.py`, `git diff --check`, secrets scan | pass |

The earlier runs of this section (before the fix) stay below as the record of the release candidate at `7feb25f`.


All runs on 2026-11-01, macOS (arm64), one machine; the suites were run alone (the baseline run of section 1 was
the only one under concurrent load).

| Run | Result |
|---|---|
| Full suite, Python 3.13.5, after the bump | 802 OK, 43 skipped (the opt-in PostgreSQL and container modules), 283.9 s, 126 MB |
| Full suite, Python 3.9.6, after the bump | 802 OK, 64 skipped (plus the scenarios that need FastAPI), 262.5 s, 177 MB |
| PostgreSQL modules, second pass after the bump | 46 OK across ten modules (section 3), 10.7 minutes in total, 106–134 MB each |
| Real Docker, `FA_CONTAINER=1 test_container_v21.py` | 27 OK, 52.1 s (Docker 29.5.2) |
| Multiprocess concurrency, `test_concurrency_v22.py` | 10 OK (inside the suite; 14 s alone) |
| Adversarial controls | `test_recovery_v22.py` 29, `test_merge_integration_v22.py` 14, `test_adopted_v22.py` 16, `test_retire_contract_v22.py` 13, `test_renames_v22.py` 14, the destructive acceptance on PostgreSQL 5: all inside the green suites |
| Ruff (engine, tests, scripts), `validate_structure.py` (3.9 and 3.13), `claude plugin validate`, `tests/test_docs_links.py`, `git diff --check`, secrets scan | pass |
| Timing | the full suite varies between 130 s and 284 s for the same code depending on the machine's load; the baseline of increment 5 measured 173 s and 217 s on two runs; the numbers above are from the runs they name |

## 11. Residual risks

- No atomicity across files; the journal makes an interruption recoverable, not invisible.
- Durability stops where `fsync` stops (the drive cache on macOS, directory entries on Windows, filesystems that
  refuse directory syncs).
- A lock on another host, or a pid this host cannot judge, needs an operator.
- The destructive contract cannot see an old instance that is disconnected or behind a pooler; the operator's
  declaration and the identified sessions are what it has, and the plan says so.
- The local executor is process hygiene; the container executor trusts the daemon, the runtime and the kernel.
- Generated projects created by earlier versions keep the old `public_jwk` and the `>=2.9` pin: with PyJWT 2.9.0
  their `tests/test_jwks.py` can still fail for about one EC key in a hundred; regenerating the tests or installing
  PyJWT 2.10.1 or later removes it. Projects generated by v2.2.0 are not affected.
- Timing of the suites varies with the machine's load by a factor of almost two between runs; the numbers above are
  from the runs they name.

## 12. Verdict

**READY** as a release candidate for the human review. Every capability of the matrix is verified at the level it
claims, the rename cycle ran end to end on a real PostgreSQL twice, the trust model and the real container executor
hold, the historical artefacts read as before, the suites are green on both interpreters, and the one defect found
by the acceptance (the reused pid) is fixed and tested. Nothing is published: the push, the tag and the release wait
for the decision.

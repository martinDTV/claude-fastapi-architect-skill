# FastAPI Architect v2.1.0 — discovery and architecture proposal

Status: **reviewed and decided** (proposal and decisions of 2026-10-31; the decisions are in section 10 and override
anything above them; increments 0, 1 and 2 are implemented, see `docs/releases/RELEASE-NOTES-v2.1.md` and
`adr-2.1-03-isolated-container-executor.md`). Baseline: the published v2.0.0, commit
`d2d50d7ba98e2f75b554b5616f2ef0c865fff7c2` (`origin/main`). Nothing in this document is implemented; every statement
about the current state was checked read-only against that commit. The plugin stays universal: no project-specific
coupling is proposed anywhere below.

Method: inventory of the design engine (`fastapi_design/`, 7 876 lines over 13 modules), the audit engine
(`fastapi_audit/`, 10 188 lines plus `remediate/`, 2 308 lines), the test suite (551 tests, 24 modules, 20 fixture
trees, 9 opt-in PostgreSQL tests, 48 plugin eval cases), the published roadmap (`RELEASE-NOTES-v2.0.md` § Roadmap) and
the acceptance report (`ACCEPTANCE-v2.0.0.md`). Tool availability on the development machine was checked for the
sandbox analysis (Docker 29.5.2 and Podman present; `sandbox-exec` present; no `bwrap`/`unshare`, macOS).

---

## 1. Area analyses

Each area: current state and reusable capabilities, real limitations, risks, dependencies, complexity, priority,
tests and acceptance criteria, and what belongs in v2.1.0 versus later.

Complexity scale: **S** (one module, a day of focused work, under 20 tests), **M** (two or three modules, one
increment, 20 to 50 tests), **L** (a new sub-system or contract bump, one full increment with PostgreSQL scenarios),
**XL** (two increments or more).

### 1.1 Advanced migrations: expand/contract, safe backfills, resumable and idempotent operations, cross-version compatibility

**Current state (reusable).**

- One deterministic Alembic revision per generation or evolution. Ids are derived from content, not the clock:
  `initial_revision` = sha1("initial|" + spec digest), `delta_revision` = sha1("delta|" + head + "|" + evolved digest)
  (`blueprint.py:1424-1433`). The manifest records the chain in order (`migrations: [{revision, down_revision, file,
  tables}]`, `generator.build_manifest`).
- The delta revision is strictly additive: `create_table`, `add_column` (nullable, or NOT NULL with a server default),
  foreign keys, non-concurrent indexes, and for RLS projects the grants, `ENABLE`/`FORCE` and the policy through
  `op.execute` (`blueprint._migration`, lines 1476-1621). Downgrades drop what the revision added.
- `env.py` runs every revision inside `context.begin_transaction()`; on PostgreSQL DDL is transactional, so a failure
  leaves the previous revision (proven by `test_postgresql_failure_during_the_migration_leaves_the_previous_revision`).
- The evolution planner classifies every field change as `compatible`, `potentially_incompatible` or `unsupported`
  and blocks the unsafe forms with named preconditions: `backfill_strategy` (NOT NULL without default, NOT NULL foreign
  key), `compatible_request_contract` (new required field without default), `existing_routes_unchanged`,
  `no_destructive_schema_change`, `historical_migrations_unchanged` (`evolution._apply_module_changes`,
  lines 445-512; `INVARIANTS`, line 60). Removals, renames and type changes are rejected at validation
  (`UNSUPPORTED_MODULE_KEYS`, line 62).
- Discovery reads the chain from the files by AST (`revision`, `down_revision`), detects several heads, unknown
  parents and historical files modified since generation (`discovery._alembic`, lines 211-274); planning blocks on
  any of them.
- The generated suite proves one head, no drift against the models (`compare_metadata`), constraints, and that
  upgrade/downgrade are repeatable (`blueprint._migration_tests`); the opt-in PostgreSQL module proves backfill by
  server default and downgrade behaviour on real data.
- The auditor already carries the guard rails an expand/contract generator must respect: `MIG_DDL_WITH_DML`,
  `MIG_DOWNGRADE_NOOP`, `MIG_DROP_COLUMN`, `MIG_DROP_CONSTRAINT`, `MIG_DROP_TABLE`, `MIG_INDEX_NOT_CONCURRENT`,
  `MIG_NOT_NULL_WITHOUT_DEFAULT`, `MIG_RAW_SQL`, `MIG_TYPE_CHANGE` (`rules/migrations.py`). Generated migrations are
  audited by `verify_in_copy`, so a generator that emits a destructive statement is caught by the plugin's own audit.

**Real limitations.**

1. There is no notion of a multi-phase change. An evolution is exactly one revision applied in one transaction, so
   anything that needs "deploy, backfill, then constrain" cannot be expressed.
2. Backfill exists only as a server default at DDL time. A volatile default (`now()`) rewrites the table under an
   exclusive lock; there is no batched, resumable backfill and no "NOT NULL after backfill".
3. Renames, drops and type changes are unsupported, so a mistake in a field name is permanent at the contract level
   (the only recourse is a new field and a manual migration outside the tool).
4. Indexes are created non-concurrently inside the transaction. Correct for a new table, wrong for an existing large
   one (`CREATE INDEX CONCURRENTLY` cannot run inside a transaction; it needs `op.get_context().autocommit_block()`).
5. The manifest knows which revisions exist in the repository, not which were applied to which database. Multi-phase
   plans need the database to carry its own progress (the `alembic_version` table does it for revisions; a backfill
   needs its own record).
6. Cross-version compatibility of the application code is not modelled. During an expand phase the previous
   application must keep working against the expanded schema, and during a rename both column names must be served.
   The generator currently produces a single code state per contract.

**Risks.**

- Data loss in a contract phase (dropping a column or table) executed before the expand phase is complete
  everywhere; irreversible by downgrade.
- Long locks on large tables (table rewrites, non-concurrent indexes, `SET NOT NULL` full scans).
- A backfill interrupted half-way and re-run with non-idempotent statements.
- Identifier injection: migrations interpolate table and column names into raw SQL for RLS (`op.execute(f"…")`); the
  names come from identifiers validated by the contract (`spec.py`), and the invariant must be kept explicit when the
  surface grows (renames add a second identifier per statement).
- RLS: a copied column on an RLS table must keep the policy semantics; a renamed tenant column is not a rename the tool
  should accept at all.
- Audit noise: `MIG_RAW_SQL` already fires on RLS migrations; the new statements must not add findings the
  generated-project baseline cannot explain.

**Dependencies.** Alembic ≥ 1.13 (`autocommit_block`), PostgreSQL ≥ 12 (`NOT VALID` check constraints then `VALIDATE
CONSTRAINT` to add NOT NULL without a long exclusive lock; PostgreSQL 11+ non-volatile defaults without rewrite),
asyncpg, the throwaway cluster test support (`tests/_pg_support.py`), evolution contract 1.2 and manifest 1.3
(section 4), the RLS blueprint for policy-aware copies.

**Complexity.** XL as a whole; L for the recommended v2.1.0 subset.

**Priority.** High. It is item 1 of the published roadmap and the most requested capability a generated project needs
after its first deploy.

**Tests and acceptance criteria.**

- Unit: plan classification for every new change kind (`set_not_null`, `rename_field`, `drop_field`,
  `add_index`) in every state (missing backfill evidence, wrong phase order, RLS tables, tenant column); contract 1.2
  validation errors with JSON paths; deterministic revision ids per phase; generated migration text golden-tested.
- Generated project suite: still one head; expand phase has a clean downgrade; contract phase declares itself
  irreversible and its downgrade is a documented refusal, never a silent no-op (so `MIG_DOWNGRADE_NOOP` does not fire).
- PostgreSQL opt-in: expand applies on populated tables; the **previous** generated test suite (N-1 code) still passes
  against the expanded schema; the batched backfill is idempotent (second run changes nothing) and resumable
  (interrupted by fault injection, resumed, final state identical to an uninterrupted run); the contract phase refuses
  while rows remain unbackfilled and succeeds afterwards; concurrent index creation leaves one head and a valid index;
  all of it as the restricted role on RLS projects.
- Audit: the generated migrations stay at the v2.0 baseline (`MIG_RAW_SQL` on RLS only), everything else clean.

**v2.1.0 versus later.** In v2.1.0: NOT NULL after a batched, resumable backfill; concurrent indexes on existing
tables; drop of a field previously declared deprecated (contract phase with evidence); field rename as a two-step
evolution (expand with dual-write code state, then contract). Later: entity (table) renames and splits, type changes
other than widening (`str` length, nullable → not null), data migrations with business logic, cross-database moves,
anything that needs triggers.

### 1.2 Evolution of existing code: advanced structural transformations without destroying manual code

**Current state (reusable).**

- Generated projects evolve by regenerating managed files and a fixed list of integration points
  (`DEFAULT_INTEGRATION_POINTS`, `evolution.py:61`); any manual change on an integration point blocks with
  `manual_change_on_integration_point`; files the manifest does not know are never touched; seven conflict kinds are
  reported with their path (`evolution.py:705-745`).
- Adopted hand-written projects are transformed only inside adopted modules and only by three append-only
  primitives: `ensure_import`, `insert_function` (a route handler appended after the last statement, duplicate symbol
  and duplicate route refused, router bound exactly once) and `insert_class_field` (one annotated field at the end of a
  class body). Every primitive proves, by comparing `ast.dump` of the top-level nodes as a subsequence, that no existing
  node changed (`transforms._verify_additive`); edits are line insertions, so comments and formatting of untouched
  regions survive. Handler bodies are limited to returning a literal JSON value; added fields to `str|int|float|bool`.
- Adopted files must be unchanged since adoption (`adopted_files_unchanged_since_adoption`), otherwise the user adopts
  again to refresh the baseline.
- Every plan is applied to a copy and verified: audit diff with blocking new high/critical findings, route invariants
  (missing or changed routes), digest of every file outside the plan, tests under the trust model
  (`evolution.verify_in_copy`). Writes go through the journal (section 1.5 for durability).

**Real limitations.**

1. A hand edit on any integration point of a generated project freezes all further evolutions of that project until the
   user restores the file or folds the change into the contract. In practice `main.py`, `config.py` and `conftest.py`
   are edited early in a project's life, so this is the most common dead end.
2. Adopted projects cannot receive a new module (`add_modules` is blocked for them) because nothing can insert an
   `include_router` call into a hand-written router or application file.
3. Added handlers cannot take parameters or call project code, so they are placeholders rather than features.
4. No transform touches the inside of an existing function or class method; no transform inserts a method.
5. No three-way merge exists anywhere: the tool knows the generated baseline (the manifest digest and the blueprint can
   regenerate the exact previous text), the current text and the new generated text, which is all a merge needs.

**Risks.**

- A textually clean three-way merge that is semantically wrong (two independent additions that conflict at runtime,
  an import shadowing). Mitigation: merged files are never written without the copy verification (audit, routes,
  protected files, tests) and are recorded in the plan and the manifest history as merged.
- Handler delegation that imports arbitrary project symbols widens what generated code can call; the auditor sees
  the call, and the transform must refuse names that do not resolve in the project (the audit `facts` resolver can
  answer that statically).
- Python syntax variety in hand-written files (decorators with arguments, nested routers, `if TYPE_CHECKING:` imports,
  CRLF files, tabs): every new anchor needs adversarial fixtures.

**Dependencies.** `transforms.py` and its evidence model, `discovery.ProjectModel` scopes, `fastapi_audit.facts`
resolution for symbol existence, `difflib` (standard library) for the merge, the journal.

**Complexity.** M for parameters and delegation plus `include_router` insertion; M for the three-way merge; L together.

**Priority.** High for the merge (it removes the most common dead end of generated projects); medium for richer
adopted-module transforms.

**Tests and acceptance criteria.**

- Transform unit tests on adversarial fixtures: idempotency (second application is a no-op with evidence), refusal of
  ambiguous anchors, byte-level preservation of every untouched line (not only AST equality), CRLF and tab files,
  Python 3.9 and 3.13 parse parity.
- Merge: clean merge applied and verified; overlapping hunks block with the conflict reported and nothing written;
  merged file recorded in the manifest history; a second evolution after a merge works (the baseline is updated).
- End-to-end: adopted project gets a new module mounted through an inserted `include_router`, routes before are
  unchanged, the new routes are visible to the auditor, tests pass under the trust model.

**v2.1.0 versus later.** In v2.1.0: opt-in three-way merge for managed integration points; handlers with declared
parameters and delegation to a project callable; `include_router` insertion so adopted projects can gain generated
modules. Later: method insertion, edits inside existing bodies, renames across files, layout migrations, removals.

### 1.3 Execution isolation: a real sandbox for untrusted code

**Current state (reusable).**

- The seam exists and is already consumed by the three flows. `Executor` declares `name`, `isolation_level`,
  `guarantees`, `not_provided`, `accepts(trust)` and `run(command, workspace, policy)`; `REGISTRY` holds only
  `LocalProcessExecutor`; `select_executor` fails closed: an `untrusted` project yields no executor and the reason code
  `executor_unavailable` (`remediate/executors.py:75-166`). `design_fastapi.py`, `evolve_fastapi.py` and
  `audit_remediate.py` all run tests through `run_generated_tests` / `verify_in_copy` / the remediation pipeline, so one
  new executor serves all of them.
- Process hygiene is real and tested: copy of the project without `.git`, environments and caches
  (`Workspace.create`, `DEFAULT_IGNORED_DIRS`), fresh `HOME`, environment reduced to an allow-list, no shell, program
  allow-list, closed stdin, wall-clock timeout, `RLIMIT_CPU` and `RLIMIT_FSIZE`, digest of the Python tree before and
  after, evidence bound to a nonce by the runner adapters (pytest JUnit, unittest JSON).
- The limits are documented everywhere the user reads them (`safe-refactoring.md:18`, the executor's `not_provided`,
  the guarantees fixtures): no network isolation, no filesystem confinement, no memory limit, tests run with the
  operator's privileges.

**Real limitations.** Exactly those four, plus: `uv` is allow-listed and `uv run` executes arbitrary scripts (accepted
under `trusted`, irrelevant under `untrusted` because nothing runs); the PostgreSQL-backed generated suites need a
database the tests can reach, which any "network off" design must account for.

**Architecture options.**

| Option | Isolation obtained | Availability | Assessment |
|---|---|---|---|
| A. OCI container executor driven through the `docker`/`podman` CLI | separate mount, pid and network namespaces; memory, cpu and pid limits; dropped capabilities; read-only root; non-root user | macOS (Docker Desktop, Colima, Podman machine) and Linux CI | recommended: the only option that gives all four missing properties on every platform the plugin targets |
| B. `sandbox-exec` profile (macOS) | deny network, deny writes outside the workspace; same kernel, same user, no memory limit | macOS only, deprecated API | useful as a `confined` intermediate level, not as `isolated`; defer |
| C. `bwrap`/`unshare` user namespaces + seccomp | similar to B plus pid namespace | Linux only | second implementation of `confined`; defer |
| D. microVM (Firecracker, libkrun) | strongest | Linux KVM or Apple Virtualization framework; heavy setup | later, behind the same seam |

**Proposed design (option A).**

- `ContainerExecutor` (`name: container`, `isolation_level: isolated`), accepting `fixture`, `trusted` and
  `untrusted`. Runtime auto-detected in the order `podman` (rootless preferred), `docker`; selectable by flag; never a
  silent fallback to `local`.
- Two phases, both inside containers, never on the host:
  1. **prepare**: install the project's dependencies from its lock file into a per-run cache volume (network allowed,
     because package indexes are needed; the workspace copy is the only host path mounted, read-write; no host
     environment). A malicious project can, at most, read its own copy and talk to the network from inside the
     container during this phase; the report says so.
  2. **run**: the test command with `--network none`, read-only root filesystem, the workspace mounted read-write and
     a tmpfs `/tmp`, `--memory`, `--cpus`, `--pids-limit`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
     non-root uid, no host environment except the policy pass-through, the evidence directory mounted for the runner
     adapter, image pinned by digest (one small Python image per supported minor version), wall-clock timeout enforced
     by killing the container, every container labelled with the run nonce so orphans can be reaped.
- The digest check before and after, the runner adapters and the nonce binding stay as they are: the executor only
  changes where the process runs.
- PostgreSQL-backed suites: a sidecar `postgres` container on an `--internal` network shared only with the test
  container (no egress), URLs passed through the policy pass-through list, lifecycle and cleanup owned by the
  executor. This is a separate increment (section 5) because it doubles the lifecycle surface.
- Honest levels: `process_hygiene` (local), `isolated` (container). `confined` (B/C) is reserved as a name, not
  implemented in v2.1.0.

**Risks.**

- The Docker socket is root-equivalent on the host; prefer rootless Podman and say so. The plugin never asks for
  `--privileged`, never mounts anything but the workspace copy, the cache volume and the evidence directory.
- Kernel sharing: container escapes are out of scope and stay in `not_provided`.
- Image supply chain: pinned digests recorded in the run report; no `latest`.
- Orphaned containers and volumes after a crash of the plugin: label-based reaping on the next run and an explicit
  `--reap` command.
- Behavioural drift between host and container runs (different Python patch version, missing system libraries):
  the run report records image digest and interpreter version; the acceptance compares outcomes of the fixtures on
  both executors.
- Availability: when no runtime is present, untrusted projects keep failing closed with the same reason code as today.

**Dependencies.** `docker` or `podman` CLI; a reachable daemon or machine; images; `uv` inside the image for the
prepare phase. No new Python dependency (the executor shells out through `subprocess` with argv lists, like today).

**Complexity.** L (executor, policies, fake-runtime unit tests, opt-in integration tests, docs); the PostgreSQL
sidecar adds M.

**Priority.** High. It closes the most repeated `not_claimed` line of v2.0 and is the stated purpose of the seam.

**Tests and acceptance criteria.**

- Unit, with a fake runtime binary that records argv: every hardening flag present on every `run`, none missing;
  no host path other than the three mounts; nonce label; timeout kills; absence of runtime → `executor_unavailable`,
  never `local`.
- Opt-in integration (`FA_CONTAINER=1`): a fixture suite passes inside the container with evidence bound to the nonce;
  a test that opens a TCP connection to a public address fails fast (network off); a test that writes outside the
  workspace fails (read-only root); a test that allocates beyond the memory limit is killed and reported as such; the
  workspace digest check still works; an `untrusted` fixture is verified end to end for the first time.
- Parity: the four committed examples reach the same outcome on `local` (trusted) and `container` (untrusted).
- Guarantees fixtures (`tests/fixtures/design20/guarantees`) updated deliberately: `not_claimed` loses the network and
  filesystem lines only when the container executor is the one used, and the report says which executor ran.

**v2.1.0 versus later.** In v2.1.0: the container executor for projects whose tests need no external service, used by
design, evolve and remediate. Stretch or v2.2: the PostgreSQL sidecar. Later: `confined` level (B, C), microVM (D).

### 1.4 Quality and compatibility: preserving contracts, fingerprints, remediations and verifications

**Current state (reusable).**

| Artefact | Version at v2.0.0 | Where | Readers accept |
|---|---|---|---|
| Architecture contract | `fastapi-architect/spec/1.2` | `spec.py` | 1.0, 1.1, 1.2 |
| Generation manifest | `fastapi-architect/generation/1.2` | `generator.py` | 1.0, 1.1, 1.2 (1.0 blocks evolution: no lineage) |
| Evolution contract | `fastapi-architect/evolution/1.1` | `evolution.py` | 1.0, 1.1 |
| Adoption baseline | `fastapi-architect/adoption/1.0` | `adoption.py` | 1.0 |
| Operation journal | `fastapi-architect/operation/1.0` | `journal.py` | 1.0 (others reported unreadable) |
| Audit report | schema 1.7, 59 rules | `fastapi_audit/model.py` | consumers of 1.0+ (additive fields) |
| Finding fingerprint | `fastapiArchitect/fingerprint/v1` (sha1 of stable fields, 16 hex) | `model.py`, `report.py` | suppressions and `audit_diff` |
| Remediation record | schema "2" | `remediation.py` | 1.9 consumers |
| Reports | `architecture_report/1.0`, `evolution_report/1.1`, SARIF 2.1.0 | `report.py`, `evolution.py` | validated by `test_sarif_schema` |
| Precision harness | `scripts/audit_precision.py` on truth16/17/18 dev and acceptance corpora, calibration on two external projects read-only | | holdout never re-scored |
| Golden snapshots | four committed examples equal to a fresh generation, byte for byte | `test_design_v20.SnapshotTests` | |

**What v2.1.0 must keep.** Every reader above keeps accepting what it accepts today; fingerprints do not change for
existing findings (new rules get new ids, never reuse); the 59 rule ids and severities are stable; CLI flags and exit
codes are additive; SARIF stays valid; the 551 tests stay green on 3.9 and 3.13; the snapshots change only when the
blueprint changes deliberately and are regenerated in place; precision on the corpora is identical per rule against the
previous HEAD unless a change is deliberate and documented; the holdout corpora are not touched.

**What needs to evolve (section 4 has the matrix).** Evolution contract 1.2 (phased changes, renames, drops, merge
opt-in); manifest 1.3 (phase records, applied-plan ids, merge records, executor used for verification); evolution
report 1.2; remediation run record gains the executor description (additive); guarantees fixtures gain the container
executor claims. The spec stays at 1.2: a rename or a drop is a statement about the *change*, so it belongs in the
evolution contract, while the spec keeps describing the desired state.

**Improvements needed to keep this cheap.**

- Generalise the acceptance harness: `tests/test_acceptance_v20.py` hard-codes two scenarios; a small table-driven
  runner (spec, list of evolution contracts, executor, database) lets every new feature add one scenario row.
- An "N-1 compatibility" step in the harness: run the previous code state's tests against the new schema (needed by
  expand/contract, useful for every PostgreSQL evolution).
- A compatibility corpus of **old artefacts** committed as fixtures: a manifest 1.0, 1.1 and 1.2, an evolution 1.0
  and 1.1 contract, an operation journal 1.0, an adoption 1.0 baseline, an audit report 1.7 with suppressions. Today
  their acceptance is tested by constructing them in code; frozen files make the guarantee explicit and survive
  refactors of the constructors.
- Precision run as a scripted gate (already exists) extended to compare the generated-project audits of the four
  snapshots against the previous HEAD, so a blueprint change that adds findings is noticed before the snapshot test.

**Complexity.** M spread across the increments. **Priority.** High but never a separate feature: each increment
carries its compatibility work and the version acceptance checks the whole.

### 1.5 Technical debt

Items found during this discovery, with evidence.

1. **Artefact filtering (the `.ruff_cache` incident).** The snapshot test reads every file under the committed
   examples as UTF-8 and excludes only `__pycache__` (`tests/test_design_v20.py:566`); sixteen test sites walk trees with
   `rglob("*")`. Any tool cache or a Finder `.DS_Store` inside an example breaks the suite although git ignores them.
   The repository `.gitignore` lists neither `.ruff_cache/` nor `.pytest_cache/` nor `.mypy_cache/`; the directories
   looked ignored only because Ruff writes its own `.gitignore` inside its cache. One ignore set already exists
   (`remediate/isolation.DEFAULT_IGNORED_DIRS`) and a second one in the audit discovery (`DEFAULT_EXCLUDED_DIRS`).
   Fix: one shared `ARTEFACT_DIRS` constant used by the workspace copy, the audit discovery, the design discovery and
   every test walk; `.gitignore` completed; the snapshot test compares against `git ls-files`-equivalent walks.
   Complexity S. Priority high (it blocks releases by accident).
2. **Duplicated helpers.** `_no_symlink_on_path` in `generator.py` and `journal.py`; `_resolve_project` in `adoption.py`
   and `evolution.py`; four digest helpers (`discovery._sha`, `generator.sha256_text`, `journal.sha256_bytes`,
   `remediate/patches._sha`). One `fastapi_design/_fs.py` (or a shared common module) removes them. Complexity S.
3. **Near-duplicate regexes.** `ROLE_RE` (`contracts.py:43`) and `ADMIN_RE` (`rules/security.py:56`) differ by three
   alternatives (`require` vs `require_`, `needs`, `checker`). Unify behind one documented definition with the
   precision harness proving no change on the corpora. Complexity S, with the harness as the gate.
4. **`blueprint.py` is 3 552 lines** mixing layout, settings, security, persistence, migrations, tests and README
   generation. Expand/contract adds a migrations generator and a backfill generator; adding them to this module makes
   it unreviewable. Split into a package (`blueprint/{layout,core,security,persistence,migrations,tests,docs}.py`)
   with the snapshot test as a byte-identical oracle. Complexity M, risk low because of the oracle.
5. **Cross-package coupling.** `fastapi_design` imports `fastapi_audit` in one direction only (version, audit engine,
   report comparison, trust model). Healthy, but the plugin version lives in `fastapi_audit.model.TOOL_VERSION` and the
   executor registry in `fastapi_audit.remediate`. The container executor is consumed by both engines; a small shared
   package (`fastapi_common`: version, filesystem helpers, artefact set, executor registry re-export) makes the dependency
   explicit without moving the executors. Complexity S to M; decide in the ADR before the executor work starts.
6. **Durability of the writer.** `journal._write_atomic` writes a temp file and `os.replace`s it without `fsync` of the
   file or the directory (documented as "no durability against power loss"). Adding `fsync` on the temp file and the
   parent directory on publish is a few lines and lets the guarantee be upgraded to "durable on POSIX once the call
   returns" (macOS caveat: `fsync` does not force the drive cache; `F_FULLFSYNC` would). Complexity S; the guarantees
   docs and fixtures change deliberately.
7. **No lint configuration for the plugin's own code.** Ruff is proven on the generated projects and `tenant-ledger`
   only; running it on the plugin package picks up whatever configuration the environment has (an ad-hoc run reported
   thousands of `UP006`/`UP045` suggestions that would break the Python 3.9 floor). A `ruff.toml` at the plugin root
   with `target-version = "py39"` and an explicit rule set, plus a test that runs it when Ruff is available, makes the
   claim "the plugin lints clean" true and cheap. Complexity S to M (unknown number of real findings; estimate low).
8. **Test organisation.** Modules named by release (`test_audit_v16` … `test_evolve_v20_recovery`) are fine as history;
   new tests should be named by concern (`test_migrations_phased.py`, `test_executor_container.py`). No renames of
   existing modules (churn without value).
9. **Suite wall time.** 110 s per interpreter plus 110 s PostgreSQL opt-in plus 43 s acceptance scenario. Acceptable;
   the new PostgreSQL scenarios should share one throwaway cluster per module (the support class already allows it) to
   keep the opt-in module under five minutes.

---

## 2. Recommended scope of v2.1.0

Theme: **"Safe change at scale"**: changes that span deployments (expand/contract), changes to code the tool does not
own (merge and richer transforms), and verification of code the operator does not trust (isolated executor), on the
same contracts, the same journal and the same evidence model as v2.0.

In scope:

1. Foundations and debt (section 1.5 items 1 to 7).
2. Isolated executor (OCI container) for design, evolve and remediate, without external services.
3. Expand/contract I: NOT NULL after a batched, resumable, idempotent backfill; concurrent indexes on existing tables;
   drop of deprecated fields as a contract phase with evidence.
4. Expand/contract II: field rename as a two-step evolution (expand with dual-write, contract with drop).
5. Advanced transforms: opt-in three-way merge on managed integration points; handlers with parameters and delegation;
   `include_router` insertion for adopted projects.
6. Acceptance of v2.1.0 with a table-driven end-to-end harness and the N-1 compatibility step.

Decided on review (section 10): v2.1.0 is increments 0, 1, 2 and 6. The rename (3), transform (4) and sidecar (5)
increments are deferred to a later version.

Explicitly out of v2.1.0 (recorded for v2.2+): entity renames and table splits; type changes beyond widening; data
migrations with business logic; `confined` isolation level (`sandbox-exec`, `bwrap`) and microVMs; schema or database
per tenant; the reviewed migration path from application scoping to RLS; background jobs with tenant context; public
reads inside an authenticated API; operations assets (Docker, CI, pre-commit), outbox and workers; sub-application
mounts in the auditor. The last four groups are roadmap items 2 to 4 of v2.0 and remain valid; they are not part of the
five areas requested for this discovery.

---

## 3. ADRs to write before implementation

| ADR | Decision to take | Options | Recommendation |
|---|---|---|---|
| ADR-2.1-01 Phased migrations | How a change that spans deployments is represented and tracked | (a) one revision with autocommit blocks; (b) one Alembic revision per phase plus a backfill runner with database-side progress; (c) external orchestration | (b): phases are revisions (`expand`, `contract`), each deterministic (`sha1("expand|head|digest")`); the backfill is a generated, idempotent, batched command (`python -m <pkg>.ops.backfill <id>`) whose progress lives in a small `<pkg>_backfills` table; the contract revision checks completion and raises otherwise; the manifest records phases and the evolution report the phase applied |
| ADR-2.1-02 Renames | How a field rename reaches production without breaking the running version | (a) `ALTER … RENAME` in one step; (b) add-copy-drop with a trigger; (c) add-copy-drop with a dual-write code state produced by the expand evolution | (c): no triggers (invisible to the auditor and to tests), two evolutions, both verified; the tenant and `owner_id` columns are never renamable |
| ADR-2.1-03 Isolated executor | Which isolation technology, phases and defaults; trust mapping | A to D of section 1.3 | A (OCI via CLI, Docker first, runtime-agnostic interface so that Podman follows without coupling), two phases, hardened defaults, `isolated` level, untrusted accepted only there, never a fallback; sidecar as a separate decision |
| ADR-2.1-04 Three-way merge of managed files | Whether and how manual changes on integration points are merged | (a) keep blocking; (b) clean merges only, opt-in, verified in copy; (c) always merge | (b): `files.allow_merge: [globs]` in evolution 1.2; `difflib`-based three-way merge from the regenerated baseline; any overlapping hunk blocks; the merged file is listed in the plan, the diff is shown, the manifest history records it |
| ADR-2.1-05 Transform language for adopted modules | What a declared handler may contain | (a) literals only (today); (b) parameters and delegation to one resolvable project callable; (c) free code | (b): still one appended function, parameters typed from the contract or adopted Pydantic classes, the callee must resolve statically in the project; `include_router` insertion as a new anchored primitive |
| ADR-2.1-06 Shared infrastructure and blueprint split | Where common helpers, the artefact set and the version live; how the blueprint is split | keep as is; `fastapi_common`; move the executors | `fastapi_common` for version, filesystem helpers and the artefact set; executors stay in `fastapi_audit.remediate` with a documented import path; blueprint becomes a package with byte-identical snapshots |
| ADR-2.1-07 Writer durability | Whether publish syncs to disk | no fsync (today); fsync file and directory; `F_FULLFSYNC` on macOS | fsync file and directory by default (decided; implemented in increment 0), documented as POSIX durability with the macOS and Windows caveats; `durable=False` only for throwaway verification copies |
| ADR-2.1-08 Contract versioning for v2.1 | Which schemas bump and what readers keep accepting | per section 4 | evolution 1.2, manifest 1.3, evolution report 1.2, remediation run record additive; spec stays 1.2; journal stays 1.0 |

---

## 4. Public contracts that could evolve

| Contract | v2.0.0 | v2.1.0 proposal | Readers keep | Writers emit | Notes |
|---|---|---|---|---|---|
| `fastapi-architect/spec` | 1.2 | 1.2 unchanged | 1.0, 1.1, 1.2 | 1.2 | desired state only; no change kinds here |
| `fastapi-architect/evolution` | 1.1 | **1.2**: `modify_modules[].entities[].set_not_null`, `rename_fields`, `drop_fields` (requires `deprecated_since` evidence), `add_indexes`; `phase: expand|contract`; `files.allow_merge`; `add_modules` allowed for adopted projects when a mount point is declared | 1.0, 1.1, 1.2 | 1.2 | 1.0/1.1 documents keep meaning exactly what they meant; the new keys are rejected under 1.1 with the JSON path, as `modify_modules` is rejected under 1.0 today |
| `fastapi-architect/generation` (manifest) | 1.2 | **1.3**: `migrations[].phase`, `backfills[]`, `history[].merged_files`, `verification.executor` | 1.0 to 1.3 | 1.3 | 1.0 still blocks evolution (no lineage), unchanged |
| `fastapi-architect/adoption` | 1.0 | 1.0 unchanged (a `mount_points` note may be added as `x-` or in 1.1 if ADR-05 needs it) | 1.0 | 1.0 | |
| `fastapi-architect/operation` (journal) | 1.0 | 1.0 unchanged | 1.0 | 1.0 | durability change is behavioural, not structural |
| `evolution_report` | 1.1 | **1.2**: `phase`, `backfill`, `merges`, `executor` | consumers of 1.1 (additive) | 1.2 | |
| `architecture_report` | 1.0 | 1.0 (plus `verification.executor`, additive) | 1.0 | 1.0 | |
| Audit report | 1.7 | 1.7 (no new finding fields planned); new rule ids only if a generator needs a guard rail that does not exist | 1.0+ | 1.7 | fingerprint v1 unchanged |
| Remediation run record | 1.1 | 1.1 plus `executor.image_digest`, `executor.runtime` (additive) | 1.1 | 1.1 | |
| CLI | `design_fastapi.py`, `evolve_fastapi.py`, `audit_remediate.py` flags | new flags only: `--executor container`, `--runtime`, `--image`, `--reap`, `--allow-merge`, `--phase`; `recover` unchanged | all existing flags and exit codes | | |
| Guarantees fixtures | `tests/fixtures/design20/guarantees` | updated deliberately for the executor claims and durability | | | they remain the claim contract |

---

## 5. Plan: vertical, verifiable increments

Each increment ends with the full suite green on 3.9 and 3.13, the snapshots regenerated if the blueprint changed,
the precision harness identical against the previous HEAD, the docs updated, thematic commits, and a stop for review.

| # | Increment | Delivers | Verification that closes it | Size |
|---|---|---|---|---|
| 0 | Foundations and debt | shared artefact set and `.gitignore`; `fastapi_common` with version and fs helpers; regex unification; blueprint split; `ruff.toml` py39 and clean plugin lint; fsync on publish; compatibility fixtures of old artefacts | snapshots byte-identical; precision identical per rule on corpora and calibration projects; 551 tests green; new compatibility fixtures read; Ruff clean on the plugin | M |
| 1 | Isolated executor | `ContainerExecutor`, prepare/run phases, hardened defaults, reaping, reports and guarantees, used by design, evolve and remediate | fake-runtime unit tests; opt-in container integration (network off, read-only root, memory limit, timeout kill, nonce evidence); parity of the four examples on both executors; first `untrusted` fixture verified | L |
| 2 | Expand/contract I | evolution 1.2 and manifest 1.3; `set_not_null` with batched resumable backfill and `NOT VALID`/`VALIDATE`; concurrent indexes; `drop_fields` as contract phase with evidence; generated backfill runner and its tests | PostgreSQL opt-in: populated tables, N-1 suite against expanded schema, idempotent and resumable backfill under fault injection, contract refused until complete, RLS projects as the restricted role; audit baseline unchanged | L |
| 3 | Expand/contract II | `rename_fields` as two evolutions (expand with dual-write code, contract with drop); RLS-aware; tenant and owner columns refused | PostgreSQL opt-in: old and new names served during expand, data copied, contract drops only after completion; the generated suites of both states pass; one head | L |
| 4 | Advanced transforms | three-way merge opt-in; handlers with parameters and delegation; `include_router` insertion and `add_modules` for adopted projects | adversarial transform fixtures; merge clean/blocked cases; end-to-end adopted project gains a generated module; routes invariants; tests under the trust model | M to L |
| 5 | Stretch: PostgreSQL sidecar in the isolated executor | internal network, sidecar lifecycle, URLs via pass-through | opt-in: the tenant-projects suite passes as `untrusted` inside the container | M |
| 6 | v2.1.0 acceptance | table-driven end-to-end harness (spec × evolutions × executor × database), N-1 step, acceptance report, release notes | the criteria of section 8 | M |

Suggested order: 0 → 1 → 2 → 3 → 4 → (5) → 6. Increment 1 before 2 so the PostgreSQL scenarios of 2 and 3 can be run
both locally and, once the sidecar exists, isolated; increment 0 first because the blueprint split and the shared
artefact set reduce the risk of everything after it.

---

## 6. Risks and mitigations

| Risk | Where | Mitigation |
|---|---|---|
| Irreversible data loss in a contract phase | 1.1, increments 2 and 3 | contract revisions require database-side completion evidence and an explicit `phase: contract` in the contract; downgrade of a contract phase is a documented refusal; the plan names the data that will be lost |
| Long locks on large tables | increments 2 and 3 | no table rewrites (nullable add, batched backfill, `NOT VALID` then `VALIDATE`), `CONCURRENTLY` for indexes on existing tables inside `autocommit_block`; the plan states the lock profile of each statement |
| Non-idempotent or non-resumable backfill | increment 2 | generated runner works in keyed batches, records progress per batch in its table, second run is a no-op; fault injection tests resume mid-way |
| Semantically wrong clean merge | increment 4 | merges only opt-in, only clean, always verified in copy (audit, routes, protected files, tests); recorded in the manifest history; a second evolution after a merge is tested |
| Container runtime privileges and escapes | increment 1 | rootless Podman preferred; no privileged flags; three mounts only; `not_provided` names kernel sharing; untrusted still fails closed without a runtime |
| Drift between host and container outcomes | increment 1 | parity test on the four examples; image digest and interpreter recorded in the report |
| Blueprint split changes generated output | increment 0 | byte-identical snapshot test is the oracle; any diff is a defect, never regenerated to hide it |
| Regex unification changes auditor precision | increment 0 | precision harness per rule against the previous HEAD on corpora and calibration projects; holdout untouched |
| Contract bumps break old documents | all | compatibility fixtures of old artefacts committed in increment 0 and read by every later increment's suite |
| Scope creep from the v2.0 roadmap items 2 to 4 | planning | out of v2.1.0 by decision; recorded in section 2 |
| Opt-in PostgreSQL and container suites not run before a release | acceptance | the v2.1.0 acceptance criteria require both, recorded with environment, time and memory as in v2.0 |

---

## 7. Compatibility matrix

| Dimension | Supported in v2.0.0 | v2.1.0 target | How it is proven |
|---|---|---|---|
| Python | 3.9 and 3.13 (3.9 skips optional-dependency tests) | same floor; 3.14 to be checked once available in the environment | full suite on both interpreters; Ruff `target-version = "py39"` |
| FastAPI/SQLAlchemy/Alembic in generated projects | pinned ranges in `_pyproject` | same ranges; Alembic ≥ 1.13 already required | generated suites |
| PostgreSQL | 16 (throwaway clusters) | 16; the `NOT VALID` path needs ≥ 12 and is documented | opt-in module |
| Design contracts | 1.0, 1.1, 1.2 | same | fixtures and validation tests |
| Evolution contracts | 1.0, 1.1 | 1.0, 1.1, 1.2 | frozen old documents plus new tests |
| Manifests | 1.0, 1.1, 1.2 | 1.0 to 1.3 | frozen old manifests; discovery notes |
| Journals | 1.0 | 1.0 | recovery tests incl. corrupt journals |
| Adoption | 1.0 | 1.0 | adoption tests |
| Audit rules and fingerprints | 59 rules, fingerprint v1 | 59 (+ new ids only), fingerprint v1 | precision harness; suppression tests; SARIF schema test |
| Remediation records | schema "2", run 1.1 | additive executor fields | remediation tests |
| CLIs | all flags and exit codes | additive flags | CLI tests (`test_audit_v14_cli` and the design/evolve CLI tests) |
| Executors | `local` | `local`, `container` | fake-runtime and opt-in tests |
| Plugin install and discovery | `claude plugin validate`, marketplace 2.0.0 | 2.1.0 | validator and structure script |
| Docs | 28 references, 3 guides, design record | plus the v2.1 design record, executor guide, phased-migrations guide | `test_docs_links` and `validate_structure.py` |
| Snapshots | four examples byte-identical | four (a fifth only if a phased-migration example is wanted) | snapshot test |

---

## 8. Acceptance criteria for v2.1.0

PASS requires all of the following, each recorded in the acceptance report with environment, time and memory:

1. Full suite green on Python 3.9 and 3.13; no test removed or weakened; every v2.0 test still present.
2. Precision per rule identical against `d2d50d7` on the development and acceptance corpora and on the two
   calibration projects (read-only), except for changes declared in an ADR; the holdout corpora untouched.
3. The four committed snapshots equal a fresh generation byte for byte; their audits unchanged from the v2.0 baseline.
4. Opt-in PostgreSQL module green, including: N-1 suite against an expanded schema, interrupted-and-resumed backfill,
   contract refused until completion, rename in two steps, concurrent index with one head, everything as the restricted
   role on RLS projects.
5. Opt-in container module green: network off, read-only root, memory limit, timeout kill, nonce evidence, parity with
   the local executor on the four examples, one `untrusted` fixture verified end to end; without a runtime, untrusted
   still fails closed.
6. Old artefacts (manifests 1.0 to 1.2, evolution 1.0 and 1.1, journal 1.0, adoption 1.0, audit report 1.7) read and
   behave as in v2.0 (frozen fixtures).
7. Merge scenarios: clean merge verified and recorded; overlapping hunks block with nothing written; evolution after a
   merge works.
8. Guarantees fixtures and `not_claimed` updated deliberately and matched by the executed evidence; no claim without a
   test that exercises it.
9. Ruff clean on the plugin's own code with the committed configuration, on the generated projects and on
   `tenant-ledger`; structural validator, plugin validator, docs links, JSON/SARIF, secrets scan and `git diff --check`
   pass.
10. The acceptance report lists capabilities by verb (recommend, generate, execute, verify, modify), defects found and
    fixed, residual risks and the verdict; no publication without the author's authorisation.

---

## 9. Open questions raised for the review (answered in section 10)

1. Scope: increments 0 to 4 plus 6 as proposed, or a narrower v2.1.0 (for example 0, 1, 2 and 6) with renames and
   transforms moved to v2.2?
2. Executor: Podman-first with Docker as the second runtime, or Docker-first because it is the common installation?
3. Durability: fsync on by default (recommended) or opt-in?
4. Whether a fifth committed example showing a phased migration is wanted (it costs a snapshot to regenerate on
   every blueprint change but documents the feature end to end).

---

## 10. Decisions taken on review (2026-10-31)

The author reviewed the proposal and approved the direction with the adjustments below. They override the
recommendations above wherever the two differ.

1. **Scope of v2.1.0**: increments 0, 1, 2 and 6. Renames with dual-write (3), advanced transforms (4) and the
   PostgreSQL sidecar (5) move to a later version.
2. **Container runtime**: Docker first. The executor interface stays runtime-agnostic (the runtime is a selectable
   implementation detail behind the same `Executor` seam) so that Podman can follow without coupling.
3. **Durability**: `fsync` on by default for the critical journal operations, including the directory syncs the
   platform needs; what is and is not guaranteed is documented. Implemented in increment 0.
4. **Fifth example**: authorised, small, dedicated to demonstrating phased PostgreSQL migrations; it must not grow
   into a business application.

Mandatory adjustments for the executor (increment 1) and the migrations (increment 2):

- dependency preparation also runs in a restricted environment; installations of untrusted projects never run on the
  host;
- the Docker executor carries explicit policies for filesystem, network, user, capabilities, memory, CPU and
  processes; it never mounts the Docker socket and never exposes host credentials;
- images are identified by digest where viable and their provenance is validated;
- untrusted projects keep failing closed when no suitable executor exists;
- no claim of absolute isolation or of protection against runtime escapes;
- migration changes record phases and preconditions; destructive operations are never executed automatically;
- backfills resume without duplicating or skipping rows;
- compatibility with contracts, fingerprints, audits and historical remediations is mandatory.

Increment 0 ("Engineering Foundations & Technical Debt") was authorised first and implemented under these decisions;
its record, validations and deliberate differences are in `docs/releases/RELEASE-NOTES-v2.1.md`.

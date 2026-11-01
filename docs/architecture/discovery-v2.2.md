# FastAPI Architect v2.2.0 — Discovery & Architecture Planning: Safe Evolution II

Status: **reviewed and decided** (2026-10-31; the decisions are in section 11 and override anything above them);
increments 0, 1, 2 and 3 are implemented (`docs/releases/RELEASE-NOTES-v2.2.md`; the rename decision record is
`adr-2.2-01-safe-renames.md`), increments 2 to 6 need an authorisation each.
The analysis sections were written before any code changed. The baseline is the published v2.1.0
(`origin/main` = `7aff2b1b04604acfaedc79c7ea3e98c1f1b08b07`; release at `219383d`). Decisions the author takes on
review go into section 11 and override the recommendations above them, as in
[`discovery-v2.1.md`](discovery-v2.1.md).

Method: every statement about the current state names the module and, where it matters, the function that holds
it; the two hand-written consumer projects were read, never modified, only to calibrate what adopted projects look
like (Ruff-formatted code, `Annotated` dependencies, 24 and 4 `include_router` calls, a module-per-feature layout).
The six topics requested are analysed in section 2; the ADRs, contracts, guarantees, risks, compatibility matrix,
roadmap and acceptance criteria follow.

## 1. Starting point: what v2.0 and v2.1 already provide

| Capability | Where | Reusable for v2.2 |
|---|---|---|
| Evolution contracts 1.0–1.2, classification of every change (`compatible`, `potentially_incompatible`, `unsupported`), refusal list `UNSUPPORTED_MODULE_KEYS` (`remove_fields`, `rename_fields`, `change_fields`, …) | `fastapi_design/evolution.py` (`_validate_module_changes`, `_apply_module_changes`) | the place where `rename_fields` stops being refused |
| Phased changes: expand / index / contract revisions, deterministic revision ids, `PhaseState`, the generated backfill runner with `copy_column`, the evidence writer, the twelve `contract_*` preconditions, the reopen semantics | `blueprint/phased.py`, `blueprint/_base.py`, `fastapi_design/migrations.py` | a rename is an expand with `copy_column`, a code state that writes both columns, a verified contract that drops the old one |
| Manifest 1.3 (`phased_changes[]`, `concurrent_indexes`, `migrations[].phase`), evidence 1.0, report 1.3 | `generator.py`, `phased.py`, `evolution.py` | additive extensions only |
| File planning of generated projects: integration points, seven conflict kinds, `preserved_manual_changes`, regeneration of module files | `evolution.py` (file loop after `_module_files`) | the single `manual_change_on_integration_point` branch is where a three-way merge plugs in; base text is regenerable byte for byte (proven by the per-file oracle of v2.1) |
| Structural transforms: `ensure_import`, `insert_function`, `insert_class_field`, `compose`, `_verify_additive` (AST subsequence proof) | `fastapi_design/transforms.py` | the evidence model and the refusal style; new primitives follow the same contract |
| Adoption 1.0: modules, router bound once, file digests, protected files, `_plan_adopted` | `adoption.py`, `evolution.py` | mount points and generated modules extend it |
| Discovery: routers with effective prefixes and resolution state, routes, pydantic and SQLAlchemy models, dependencies, Alembic chain, ownership | `discovery.py`, `fastapi_audit/facts.py` (`collect_facts`, `router_prefix_resolution`, `resolve_import`) | mount-point detection and callee resolution exist already |
| Recoverable writer: per-file atomic publish, journal, forward/rollback, unreadable journals block, fsync | `journal.py` (`operation_id`, `prepare`, `publish`, `recover`) | multi-step operations and a lock extend it without changing the file protocol |
| In-body edits with AST preconditions (delete a statement, insert `await`, move a statement after a loop) | `fastapi_audit/remediate/patches.py` (`apply_edits`, `_statement_of`, `_block_containing`) | the only code that already edits inside function bodies; its style (line edits, AST re-check, blocked reasons) is the model for in-body transforms |
| Executors (local, container), verification in a copy, trust model | `fastapi_audit/remediate/executors.py`, `containers.py`, `evolution.verify_in_copy` | every new write is still verified in a copy before it lands |
| Throwaway PostgreSQL clusters, the five examples (`parcels` built for phased migrations), the acceptance modules | `tests/_pg_support.py`, `tests/test_migrations_v21*.py`, `tests/test_acceptance_v21.py` | the rename slice reuses `parcels` and the N-1 suite is the committed snapshot itself |

Facts that size the work: a single field name appears in five generated files of `parcels` (model, three schema
classes, service, the initial migration, four lines of tests); the planner regenerates all of them from the evolved
contract, so a rename of a *generated* field is a blueprint change plus a migration strategy, not a text edit.
Python 3.9 (the plugin's floor) has `end_lineno` / `end_col_offset` on every node and `ast.unparse`, so span-exact
edits are possible with the standard library. `libcst` is not installed on either host interpreter and the plugin has
been standard-library-only since v1.0.

## 2. Area analyses

### 2.1 Safe renames of columns and fields (expand / migrate / contract) with cross-version compatibility

**Current state.** `rename_fields` is refused with a path under 1.0–1.2. A rename today means a new field plus a
manual migration outside the tool. v2.1 delivered every building block of the online path except the second column
and the code states: nullable expand, `copy_column` backfill (resumable, reopenable), fresh evidence, authorised
contract, concurrent indexes.

**What a rename is, honestly.** Three code states and two schema states, in this order:

| Step | Schema | Generated code state | Who may be running |
|---|---|---|---|
| V0 (today) | `old` | reads and writes `old` | V0 |
| E: expand evolution | `old` + `new` nullable (no default), index on `new` if `old` had one | **transitional**: writes both, reads `new` falling back to `old`, API accepts both names, responses carry both (`old` marked deprecated in OpenAPI) | V0 and V1 together: V0 writes only `old`; V1 reads it through the fallback; the backfill reopens for V0's late rows (v2.1 semantics) |
| M: migrate | `backfill run` copies `old` → `new`; `verify` adds **columns agree** (`count(*) WHERE old IS DISTINCT FROM new` = 0) | unchanged | V1 |
| R: retire evolution | unchanged | **new-only**: no reference to `old` in model, schemas, service or tests; `new` NOT NULL at the API level if `old` was required | V1 and V2 together: V1 writes both, V2 writes `new`; V1 reads `new` first |
| C: contract evolution (destructive) | `SET NOT NULL new` (if required), `DROP COLUMN old` | unchanged (V2) | V2 only: V1 still maps `old` and would fail at SELECT once the column is gone |

The retire state exists because a running V1 instance maps the old column: dropping it with V1 alive breaks V1 at
the first query. Two evolutions (E and C) would force the operator to guarantee that the drop revision runs only
after the last V1 process exits, which most "migrate before deploy" pipelines cannot promise. Three evolutions make
the dangerous step a release of its own; section 11 asks whether a declared deployment order may collapse R and C.

**Two strategies, two claims.**

- `strategy: expand_contract` (default on PostgreSQL): the table above; online across versions; three evolutions;
  the drop reversible by a downgrade that re-adds `old` nullable and runs a reverse copy backfill (`new` holds the
  data), so the destructive step is recoverable by the same runner.
- `strategy: in_place`: `ALTER TABLE … RENAME COLUMN` (a metadata-only change in PostgreSQL, brief ACCESS EXCLUSIVE,
  no rewrite) plus one code state; **not** online across versions: the code and the schema must switch together,
  which the tool states as the claim. It is the only strategy for in-memory persistence (no schema) and the honest
  choice for small projects that deploy with a maintenance window. Refused on tables with a declared live-rows
  concern? No: the claim is enough; the plan prints it as a risk and the report records `online: false`.

**Never renamable** (refused with the reason): the primary key `id`, tenant columns, `owner_id`, columns referenced
by a foreign key or by an RLS policy predicate, columns of a phased change still in `expand` state, and any column
the auditor's declared contracts name. Entity (table) renames stay out of v2.2 (section 2.6 of the v2.1 discovery
holds: splits and table renames need a different lineage model).

**Cross-version compatibility, stated per pair.** V0/V1 compatible (schema expanded, fallback read, reopen);
V1/V2 compatible (both write `new`); V2/contracted schema compatible; V1/contracted schema **incompatible** (the
reason for R). The tool cannot observe which versions run; it records the operator's declaration (as
`contract_application_version_accredited` does today) and makes the drop refuse when the retire state is younger
than a declared window or the evidence predates the retire evolution.

**N-1 test, cheap and real.** The committed snapshot of the baseline is V0; its own suite run against the expanded
database proves "V0 works on the expanded schema" without any stand-in. The V1 suite is generated; the V2 suite is
generated. All three run on the throwaway cluster in the opt-in module.

**Risks.** A rename that changes a column referenced by raw SQL in a hand-written file of a generated project
(an unmanaged file the tool never touches): the audit of the copy cannot see SQL strings; the plan must list every
unmanaged Python file that mentions the old identifier as a risk (`grep`-level, named as heuristic). Pydantic alias
subtleties (`populate_by_name`, `AliasChoices`) on 2.x; the transitional schemas need golden tests. Indexes and
unique constraints on `old` must be recreated on `new` concurrently before R (a unique constraint built concurrently
is `CREATE UNIQUE INDEX CONCURRENTLY` plus `ADD CONSTRAINT … USING INDEX`). The audit baseline must not gain findings
from the transitional code (two writes of the same value are not a dual write in the reliability-rule sense; the
rule looks for two stores, not two columns — to be verified by a fixture before claiming it).

**Complexity.** L (E and M: M; R and C: M; the N-1 harness: S).

### 2.2 Dual-write and compatible read during transitions

**Options for the write side.** (a) Application-level in generated code: the service's `create` passes
`new=data.new, old=data.new`; `update` mirrors through a generated `_MIRRORS = {"new": "old"}` map after the
`setattr` loop. Visible to the auditor and to the generated tests; one code state per contract, which is what the
blueprint already produces. (b) A database trigger `BEFORE INSERT OR UPDATE` copying `new` ↔ `old`: invisible to the
auditor and to the tests, needs its own drop in C, hides a write the application does not see; rejected in
ADR-2.1-02 and still rejected. (c) A stored generated column (`new GENERATED ALWAYS AS (old) STORED`): one-way and
read-only, cannot carry the transition to V2; rejected. (d) ORM-level synonym (`new` as a `synonym("old")`): no
second column, so no overlap window across versions; it is the `in_place` strategy's code shape, not a dual write.

**Read side.** The fallback belongs to the read schema, not to SQL: `ShipmentRead` gets a `model_validator(mode=
"before")` that reads `new` and falls back to `old` when `new` is `None` (works with `from_attributes`); the
request schemas accept both names through `validation_alias=AliasChoices("new", "old")` on `Create` and `Update`;
responses carry both names in V1 (`old` with `deprecated=True` in the OpenAPI schema) so N-1 clients keep working;
V2 drops the alias and the field. Repositories and SQL are untouched in every state, which keeps the RLS and
transaction rules of the auditor at their baseline.

**Guarantees to claim.** Every row written by V1 has `new == old`; a row written by V0 has `new IS NULL` until the
backfill or the reopen copies it; a read in V1 never returns `None` for a value V0 wrote; nothing in SQL changes.
Not claimed: that a hand-written query outside the managed files sees the new column; that clients stop sending the
old name (they may, through R; only C's API drop ends it, and the plan prints the route contract diff).

**Tests.** Golden tests of the three code states for every field type and for optional/required fields; the
generated `test_shipments.py` of V1 asserts the old name is accepted, both names are returned and an update through
either name lands in both columns; the PostgreSQL slice interleaves V0 writes (through the baseline snapshot's own
service, executed in its venv) with V1 writes and checks `columns agree` after the backfill.

**Complexity.** M, inside 2.1.

### 2.3 Advanced structural transforms that preserve manual code, comments and formatting

**Current state.** Three append-only primitives at line boundaries (`transforms.py`), proven additive by an AST
subsequence check; `patches.py` deletes, inserts and moves whole statements inside bodies with AST preconditions;
both preserve every untouched line byte for byte because they never re-render.

**What v2.2 needs, in order of risk.**

1. *Statement insertion inside a body* (an `include_router(...)` call after the last one in `create_app()`, a
   method at the end of a class, a parameter-free statement at a block's end): still a line insertion at a statement
   boundary found through `lineno` / `end_lineno` / `col_offset`; the additive proof generalises to "the enclosing
   body grew by exactly one statement and every other node is unchanged". Standard library.
2. *Bounded span rewrites* (add a keyword argument to an existing call such as `include_router(r, prefix=…)`, add a
   parameter to a handler signature such as `dep: X = Depends(y)`, change a default value): replace exactly the
   source span of one node (`end_col_offset` is exact on 3.8+) with new text that is the old text plus an insertion
   at a token boundary (before the closing parenthesis, after the last argument), never a re-rendering. Guard: the
   span must contain no `COMMENT` token (checked with `tokenize`) and no line continuation inside the inserted
   region; otherwise refuse. Proof: the AST of the module with the rewritten node is the old AST with exactly that
   node replaced by the expected new node; every byte outside the span is unchanged (compared literally).
3. *Formatter stability as evidence, not assumption*: when the project declares a formatter (`[tool.ruff]` in both
   consumer projects and in every generated project; `black` detected the same way), the verification copy runs
   `ruff format --check` (or `black --check`) on the edited files through the trust model; a transform whose output
   the project's formatter would rewrite is reported `format_unstable` and, under the default policy, refused.
   Comments are preserved by construction (nothing inside a comment-bearing span is touched) and the claim is
   checked by a `tokenize` comparison of the COMMENT tokens before and after.
4. *Not in v2.2*: edits that re-render a node (`ast.unparse` loses comments and formatting), renames across files,
   moving code between files, layout migrations. These are the cases where a concrete-syntax tree would be
   needed, and the decision in ADR-2.2-04 is to refuse them rather than take the dependency.

**Why not libcst.** It would allow arbitrary node rewrites with comment preservation, at the price of the first
third-party dependency of the plugin's own scripts (installed how, pinned how, on both 3.9 and 3.13, inside the
offline evals and inside `uvx`-less environments), of a second parser whose tree differs from `ast` (every proof in
`transforms.py` and every fact in the auditor is `ast`-based), and of a capability that would silently vary between
machines if made optional. The span primitives cover every transform the roadmap needs; the refusal list is
explicit; the dependency can be reconsidered when a needed transform cannot be expressed as an insertion.

**Adversarial fixtures** (synthetic, modelled on the consumer shapes, never their code): decorators with keyword
arguments and `response_model`, `Annotated` dependencies, nested routers, `if TYPE_CHECKING:` imports, CRLF files,
tabs, a comment inside the call being extended, a call split across lines with a trailing comma, a signature with
`*,` and defaults, a class with a nested class, Python 3.9 and 3.13 parse parity, idempotency (a second application
is a no-op with evidence).

**Complexity.** M for 1 and 3; M for 2 with its guards; L together.

### 2.4 Three-way merge for integration points modified by the user

**Current state.** A hand edit on any of the sixteen `DEFAULT_INTEGRATION_POINTS` blocks every further evolution
(`manual_change_on_integration_point`); the most edited ones in practice are `main.py`, `core/config.py` and
`tests/conftest.py`. The three texts a merge needs exist: *base* (the blueprint at the baseline contract regenerates
the exact previous text; the v2.1 oracle proved byte identity), *ours* (the new generated text), *theirs* (the file on
disk).

**Algorithm.** A line-level three-way merge with `difflib` (standard library): hunks of base→theirs and
base→ours; non-overlapping hunks are both applied; an overlapping hunk is a conflict and the plan blocks, naming the
file and both line ranges; nothing partial is ever written. Then two structural checks on Python files: the result
parses, and every top-level node that *theirs* added over base and every node that *ours* added over base is present
in the result (the AST-subsequence proof of `transforms.py`, applied to both sides); a merge that loses either is
refused even when textually clean. For `.audit-fastapi.json` the merge is structural (a three-way merge of JSON
objects by key, arrays as sets where the schema says so); `pyproject.toml`, `.env.example` and `README.md` stay
line-based (3.9 has no `tomllib`; a TOML merge that only recognises whole lines is the honest option).

**Policy.** Opt-in by `files.allow_merge: [globs]` in evolution 1.3 and `--allow-merge` on the CLI (ADR-2.1-04's
recommendation, confirmed): a merged file is always verified in the copy with the whole machinery (audit, routes,
protected files, tests) and never written otherwise; it is listed under `plan.files.merged[]` with the base, theirs
and ours digests and the hunks taken from each side; the manifest records it under `history[].merged_files` and the
ownership becomes `managed_merged`: the file's manual hunks stay the diff between the regenerable base and the file,
so the next evolution merges them again (which is the behaviour the user wants: the hand edit keeps living there).
Section 11 asks whether merge becomes the default once the adversarial corpus is in place.

**Risks.** Textually clean, semantically wrong: an import added by both sides under the same name, two
`include_router` calls that mount the same prefix twice, a manual change inside a function the new generation
rewrites. Mitigations: the double-inclusion proof, the route invariants of the copy (duplicate mounts show as
changed or duplicated routes), the audit diff (shadowed imports, unused names appear as findings only when a rule
exists; the plan must say the merge was verified by tests and routes, not by a semantic merge), and the formatter
stability check of 2.3 on merged files.

**Complexity.** M (algorithm and proofs: S–M; wiring, evidence, manifest and docs: M).

### 2.5 Fuller evolution of manually adopted projects

> Implemented by increment 4 (2026-11-01) as adoption 1.1 and evolution 1.6, with one mount point per adoption
> instead of `mount_points[]`, self-contained generated packages, bounded extensions and persistence only on the
> project's own verified Alembic environment: [`adr-2.2-06-adopted-projects-ii.md`](adr-2.2-06-adopted-projects-ii.md).

**Current state.** Transforms only inside adopted modules; handlers return a literal; fields `str|int|float|bool`;
no new modules (nothing can mount a router); no persistence; adopted files must be unchanged since adoption; a
hand-written project with a manifest is refused.

**Proposals, each bounded.**

1. *Mount points* (adoption 1.1 `mount_points[]`): the discovery already resolves `include_router` calls and
   effective prefixes (`facts.router_prefix_resolution`); a mount point is a file, the enclosing function or module
   level, the router variable and the statement after which new mounts go. `propose` lists them; `adopt` records
   the chosen one.
2. *Generated modules inside adopted projects* (`add_modules` allowed when a mount point exists): the module is
   generated in the plugin's own shape under the project's package, in the detected layout family (layered or
   modular; `unknown` refuses), and mounted by a statement insertion (2.3, item 1). The new files are recorded in
   adoption 1.1 as `generated_modules` with digests and a spec fragment, so later evolutions regenerate them exactly
   as in a generated project while hand-written modules stay append-only. Persistence of such a module: in-memory
   always; PostgreSQL only when the adopted project's Alembic environment is resolvable (metadata import found by
   discovery, one head, naming convention readable) — otherwise the module is refused with the reason, never generated
   half-way.
3. *Handlers with parameters and delegation* (ADR-2.1-05 option b): path, query and body parameters typed from the
   contract or from adopted Pydantic classes; the body delegates to **one** project callable that must resolve
   statically (`facts.resolve_import` and the call-graph summaries), with the declared parameters forwarded; the
   response model is an adopted class or a literal shape. Anything else stays a contract error.
4. *`add_dependency`* on an existing handler: append `name: Type = Depends(callable)` (or the `Annotated` form the
   file already uses, detected per file) to the signature of one handler — a span rewrite (2.3, item 2) with all its
   guards; the dependency callable must resolve. This is the first edit of an existing node in an adopted file and
   is gated behind the transform engine increment.
5. *`add_fields` on adopted SQLAlchemy models* plus an Alembic revision in the project's own environment: nullable
   columns only, or phased (expand with backfill) when required, reusing the v2.1 templates; refused when the
   environment cannot be resolved. The migration file lands in the project's versions directory and is recorded in
   adoption 1.1 (`migrations[]`), so the "historical revisions unchanged" invariant extends to adopted projects.
6. *`adopt --refresh`*: re-baseline the files of named modules after manual changes without re-declaring routers
   (the same validations as `adopt`); and a weaker precondition for `add_endpoints` and `add_fields`: a changed
   adopted file is accepted when the change does not touch the anchors the transform needs (proven by comparing the
   relevant AST nodes with the adoption snapshot), otherwise the refresh is required. This removes the most common
   friction without weakening the rule that the tool never guesses.
7. *Discovery improvements that the above need*: routers created by factories, prefixes from constants in other
   modules (today `unresolved`), `Annotated` dependency aliases (exists in facts), class-based views refused with a
   note.

**Not in v2.2.** Converting an adopted project into a generated one; rewriting existing handler bodies; adopting a
project with a generation manifest; multi-package repositories.

**Complexity.** L (1–2: M; 3: M; 4: S after 2.3; 5: M; 6–7: S–M).

### 2.6 Recovery, concurrency and idempotency of complex transformations

**Current state.** One journaled operation per `apply` or `adopt`: identity = sha256 of (paths, expected digests,
new digests, kind, description); staged content and backups; atomic per file; `recover --forward|--rollback`;
conflicts left alone; unreadable journals block; fsync by default. No lock: two concurrent `apply` runs are kept
apart only by the expected-digest re-check before publication, which leaves a window between that check and the
renames. No notion of a multi-step operation that spans the repository and a database; the phased migrations of
v2.1 avoid the problem by making every database step the operator's command with its own database-side record.

**Proposals.**

1. *A lock around prepare → publish → complete and around `recover`*: `.fastapi-architect/ops/LOCK` created with
   `O_EXCL`, holding pid, host, start time and the op id; a stale lock (owner pid gone on the same host, or older
   than a declared age on another host) is reported, never silently broken; `plan` does not take the lock (it reads).
   Default when the lock is held: fail with the holder's identity (section 11 asks whether to wait briefly instead).
2. *Multi-step operations in the journal* (operation 1.1, additive): `steps[]` naming what produced each file
   (`regenerate`, `merge`, `transform`, `manifest`), their evidence digests and a `parent_op` for chains; the file
   protocol does not change, so a 1.0 reader still recovers a 1.1 journal (it ignores `steps`). An `ops list | show`
   subcommand replaces reading JSON by hand.
3. *Sagas across repository and database, written down*: each rename phase is one journaled repository operation
   plus the operator's database commands; the compensation of every step is named in the plan (E: downgrade drops
   the new column; M: idempotent, reopenable; R: a new evolution restores the transitional state; C: downgrade re-adds
   the column and the reverse copy fills it). There is no distributed transaction and the tool says so; what it
   guarantees is that every step is individually recoverable and that the next step refuses until the previous one
   is observed complete (evidence, preconditions, `alembic current`).
4. *Idempotency of every new primitive*: a merge whose hunks are already present yields `noop` with evidence; a span
   rewrite whose argument or parameter already exists skips with evidence; a mount already present is `noop`; a plan
   whose every step is `noop` is `noop` as a whole, and a plan with any real step keeps the same identity on retry
   (same inputs, same digests, same id) so a crash before publication is a plain retry and a crash after it is a
   `recover`.
5. *Fault injection matrix extended to the new steps*: before and after a merge is computed, during the publication
   of merged and transformed files, during recovery of each, with the lock held by a dead pid, with two processes
   racing (one must publish, the other must refuse with nothing written), with a database step failing between two
   repository steps of the saga (the plan must refuse to continue and say which evidence is missing).

**Complexity.** M (lock and steps: S–M; saga documentation and preconditions: S; the fault matrix: M).

## 3. ADRs to write before implementation

| ADR | Decision | Options | Recommendation |
|---|---|---|---|
| ADR-2.2-01 Rename state machine | How a field rename is declared, staged and completed | (a) two evolutions (expand, contract); (b) three (expand, retire, contract) ; (c) one evolution with a declared deployment order | **(b)**; (c) offered as an explicit, documented shortcut only if section 11 accepts the risk statement |
| ADR-2.2-02 Rename strategies and claims | Which strategies exist and what each claims | `expand_contract` only; `in_place` only; both | **both**, each with its own claim (`online: true|false`), `expand_contract` default on PostgreSQL, `in_place` the only one in memory |
| ADR-2.2-03 Dual-write mechanism | Where the second write and the fallback read live | application code; trigger; generated column; ORM synonym | **application code** (service writes, read-schema fallback, request aliases); triggers and generated columns rejected again |
| ADR-2.2-04 Transform engine | How existing code is edited | stdlib span primitives with guards; `libcst`; re-rendering with `ast.unparse` | **stdlib span primitives**: insertions at statement and token boundaries, bounded span rewrites with the comment guard, formatter-stability evidence; re-rendering refused; `libcst` reconsidered only for a transform that cannot be an insertion |
| ADR-2.2-05 Three-way merge policy | When manual changes on integration points are merged | keep blocking; opt-in clean merges verified in copy; default-on | **opt-in** (`files.allow_merge`, `--allow-merge`), structural double-inclusion proof, JSON merged structurally; default-on reconsidered after the adversarial corpus |
| ADR-2.2-06 Adopted projects II | What an adopted project may receive | append-only as today; generated modules at a mount point plus delegation plus bounded signature edits; free transforms | **the middle option**, each capability refused when its precondition (mount point, resolvable callee, resolvable Alembic environment) is missing |
| ADR-2.2-07 First destructive operation | How a column drop is authorised and reversed | never drop; drop in contract with evidence, retire window, explicit flag and reverse-backfill downgrade; drop on evidence alone | **the middle option**: `--authorize-destructive` plus `declared_by`, fresh evidence that postdates the retire evolution, `columns agree`, a minimum retire age (hours, declared in the contract, default 24), downgrade re-adds and reverse-copies |
| ADR-2.2-08 Concurrency and recovery | Lock, multi-step journal, saga statement | none; lock plus steps plus documented compensations; a daemon or server-side coordinator | **lock plus steps plus compensations**; fail-fast on a held lock |
| ADR-2.2-09 Contract versioning for v2.2 | Which schemas bump | per section 4 | evolution 1.3, manifest 1.4, evidence 1.1, report 1.4, adoption 1.1, operation 1.1; spec stays 1.2 |

Each ADR is written in the increment that implements it (as ADR-2.1-01 and ADR-2.1-03 were), with context,
options, decision and consequences; the discovery holds the recommendation only.

## 4. Public contracts that should evolve

| Contract | v2.1.0 | v2.2.0 proposal (additive) | Readers keep | Notes |
|---|---|---|---|---|
| `fastapi-architect/spec` | 1.2 | 1.2 unchanged | 1.0–1.2 | desired state only; a rename is a change, not a state |
| `fastapi-architect/evolution` | 1.2 | **1.3**: `modify_modules[].entities[].rename_fields[] {from, to, strategy, deprecated_since?}`, `retire_fields[] {operation}`, `contract_fields[].operation` may name a rename operation (drop), `authorization.contract.destructive {declared_by, min_retire_age_hours}`, `files.allow_merge[]`, adopted: `add_modules` with `mount_point`, `add_endpoints[].parameters/delegate_to`, `add_dependency[]`, `add_columns[]` | 1.0–1.3 | new keys refused under 1.0–1.2 with the path, as today |
| `fastapi-architect/generation` (manifest) | 1.3 | **1.4**: `phased_changes[].kind: add|rename`, `.from_column`, `.code_state: transitional|retired|contracted`, `history[].merged_files[]`, ownership `managed_merged` in discovery, `migrations[].destructive` | 1.0–1.4 | 1.0 still blocks evolution (no lineage) |
| `fastapi-architect/migration-evidence` | 1.0 | **1.1**: `operations[].columns_agree`, `.mismatch_rows`, `.old_column_present`, `retired_at` from the manifest | 1.0–1.1 | written by the generated runner |
| `evolution_report` | 1.3 | **1.4**: `plan.files.merged[]`, `plan.transforms[]` extended with `span` evidence, `code_states`, `online`, `destructive_authorization` | consumers of 1.3 | additive |
| `fastapi-architect/adoption` | 1.0 | **1.1**: `mount_points[]`, `generated_modules{}`, `migrations[]`, `history[].refresh` | 1.0–1.1 | |
| `fastapi-architect/operation` (journal) | 1.0 | **1.1**: `steps[]`, `parent_op`, lock metadata outside the journal | 1.0–1.1 | file protocol unchanged |
| `architecture_report`, audit report, remediation run | 1.1 / 1.7 / 1.3 | unchanged | | no new rule ids planned; a fixture must prove the transitional code adds no finding |
| CLI | v2.1 flags | new flags only: `--allow-merge`, `--authorize-destructive`, `adopt --refresh`, `ops list|show`; exit codes unchanged | all existing | |
| Guarantees fixtures | `tests/fixtures/design20/guarantees` | unchanged for existing examples; new entries for the transitional code states | | they remain the claim contract |

## 5. Safety guarantees to claim, and what stays unclaimed

- **Renames**: online across versions only with `expand_contract` and the three-evolution sequence; `in_place`
  claims a coordinated deploy, nothing more; protected columns are never renamed; the old column is dropped only in
  an authorised contract whose preconditions include evidence that postdates the retire evolution, `columns agree`,
  the minimum retire age and the explicit destructive flag; the downgrade of the drop re-adds the column and
  reverse-copies the data.
- **Dual write and read**: every V1 write lands in both columns; a V1 read never loses a V0 value; SQL and
  repositories are untouched; the auditor's baseline does not change (to be proven by fixture before the claim).
- **Transforms**: bytes outside the edited span are unchanged, proven by comparison; comments are preserved by
  construction and checked by tokens; formatter stability is checked when the project declares a formatter;
  ambiguity, comment-bearing spans and unresolvable callees are refused.
- **Merge**: conflicts never write; both sides' additions are proven present; every merged file is verified in the
  copy before it lands; the manual hunks are carried forward, not absorbed.
- **Recovery**: per-file atomicity and the journal as today; one writer at a time per project through the lock;
  every multi-step operation has its compensations named; no cross-domain atomicity is claimed.
- **Unclaimed**: that hand-written SQL or files outside the manifest see a renamed column; that clients stop using
  the old name before the API drop; that a merge is semantically right beyond what tests, routes and the audit
  check; that a running process version is known to the tool.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Dropping `old` while a V1 process is alive | the retire evolution; the drop refuses until the retire age and fresh evidence exist; the saga and its compensation documented; downgrade with reverse copy |
| A unique constraint or foreign key on `old` not carried to `new` | refusal for foreign keys and RLS predicates; unique indexes rebuilt concurrently in E and verified by the evidence before R |
| Transitional code triggering audit findings | a fixture in increment 1 with the generated transitional project audited against the baseline; the claim made only after it passes |
| Pydantic alias behaviour differing between versions | golden tests on the generated schemas; the generated project's own tests assert both names |
| Span rewrite breaking formatting the project enforces | formatter-stability check in the copy; `format_unstable` refuses by default |
| Merge that loses a side or duplicates a mount | double-inclusion proof; route invariants; blocking on overlap |
| Adopted Alembic environment that differs from the generator's assumptions | resolution preconditions; refusal with the reason; the revision is generated only when metadata and head resolve |
| Two operators applying at once | the lock; expected-digest re-checks stay |
| Scope creep into a code-rewriting engine | the explicit refusal list in ADR-2.2-04 and the unsupported keys kept in the contract |
| Hand-written files mentioning the old identifier | listed in the plan as a heuristic risk (identifier search over unmanaged Python files), never edited |

## 7. Compatibility matrix

| Artefact written by | Read by v2.2.0 | Meaning preserved | Proof |
|---|---|---|---|
| Evolution contracts 1.0, 1.1, 1.2 (the eight committed examples, the compat fixtures) | yes | yes; the new keys are refused under old schemas | `tests/test_compat_artifacts.py` extended with a 1.2 phased contract and a 1.2 plan |
| Manifests 1.0–1.3 (compat fixtures, the five snapshots) | yes | yes; 1.0 still has no lineage and blocks evolution | compat tests; snapshots regenerated only in their manifests when 1.4 keys are added |
| Migration evidence 1.0 written by a v2.1 runner | yes | yes; `columns_agree` absent means "not a rename" | unit |
| Journals 1.0 (interrupted operation fixture) | yes | recover unchanged | compat tests |
| Adoption 1.0 | yes | yes; no mount point means `add_modules` refused as today | compat tests |
| Reports 1.3 consumers | additive keys only | yes | schema tests |
| Audit 1.7, 59 rules, fingerprints, SARIF, suppressions, remediation runs 1.1–1.3 | untouched | yes | precision harness against `7aff2b1` in the acceptance |
| CLI flags and exit codes of v2.0 and v2.1 | kept | yes | CLI tests |
| Generated projects of v2.0/v2.1 without renames | byte-identical code | yes | per-file oracle against the v2.1.0 engine on every spec |
| Python 3.9 and 3.13 | both | | full suites |

## 8. Roadmap: vertical, verifiable increments

| # | Increment | Content | Depends on | Size | Acceptance criteria (each with a PostgreSQL or adversarial test where it applies) |
|---|---|---|---|---|---|
| 0 | Foundations for Safe Evolution II | span primitives and guards in `transforms.py` (statement insertion in bodies, bounded span rewrite, comment guard, formatter-stability check), the pure three-way merge functions, the journal lock and `steps`, the adversarial transform corpus (synthetic fixtures), `ops list|show` | — | M | every primitive proven byte-exact outside its span on the corpus, 3.9/3.13 parity; two racing writers: one publishes, one refuses with nothing written; journals 1.0 still recover; no change in any generated project |
| 1 | Renames I: expand and migrate | evolution 1.3 `rename_fields` (both strategies), manifest 1.4, evidence 1.1 (`columns_agree`), the transitional code state (dual write, fallback read, aliases, deprecation), unique indexes carried concurrently, the identifier-mention risk, `parcels` extended with `reference → label` | 0 | L | on a throwaway cluster with 2 500 rows: expand applied; the baseline snapshot's own suite (V0) passes against the expanded schema; V0 and V1 writes interleaved; backfill and reopen leave `columns agree`; evidence `ok`; `in_place` on the in-memory example and on PostgreSQL with its claim recorded; the transitional project audits at the baseline |
| 2 | Renames II: retire and contract | `retire_fields`, the retired code state (V2), the destructive contract (`SET NOT NULL`, `DROP COLUMN`) with ADR-2.2-07's preconditions, reverse-copy downgrade, ADR-2.2-01/02/03/07 written | 1 | M | the drop refused without the flag, with evidence older than the retire evolution, before the retire age, with mismatching columns, with V1 declared; applied otherwise; downgrade restores every value; V1/V2 interleaving before the drop; the full rename slice end to end on `parcels` |
| 3 | Three-way merge on integration points | wiring of the merge into the file loop, `files.allow_merge`, `--allow-merge`, structural JSON merge for `.audit-fastapi.json`, double-inclusion proof, `managed_merged`, report and manifest records, ADR-2.2-05 | 0 | M | hand-edited `main.py`, `config.py` and `conftest.py` merged across two consecutive evolutions and verified; overlapping hunks block with both ranges; a lost side is refused; the generated suite still passes; a merged project evolves again |
| 4 | Adopted projects II | adoption 1.1 (mount points, refresh), `add_modules` at a mount point with generated modules recorded, handlers with parameters and delegation, `add_dependency` span rewrite, `add_columns` with a resolvable Alembic environment, discovery improvements, ADR-2.2-06 | 0, 3 (for the formatter check) | L | the adoption fixture gains a generated module mounted and visible to the auditor; delegation to an unresolvable callee refused; a dependency appended in `Annotated` and plain styles with formatting intact; a column added through the project's own Alembic environment on the cluster; refresh after a manual change |
| 5 | Recovery and concurrency hardening | the fault matrix over merges, transforms and sagas; the saga documentation in the guides; `recover` aware of `steps`; stale-lock handling; ADR-2.2-08 | 0–4 | M | every injected failure leaves a recoverable or untouched tree; the saga refuses to continue when a database step's evidence is missing; a stale lock is reported with its holder |
| 6 | Acceptance and release candidate | the v2.2 acceptance module (locked rows and contract already covered; add the rename slice with three code states, the merge scenario, the adopted scenario), compatibility, documentation, version bump | all | M | the pattern of `ACCEPTANCE-v2.1.0.md` |

Suggested order 0 → 1 → 2 → 3 → 4 → 5 → 6; 3 can run before 2 if the merge is the more urgent pain. A narrower
v2.2.0 that the review may prefer: 0, 1, 2 and 6 (renames complete, nothing else) or 0, 3, 4 and 6 (code evolution,
no renames).

## 9. Tests the increments need

- **Unit**: evolution 1.3 validation with JSON paths for every new key and every refusal (protected columns, unknown
  strategy, retire of an unknown operation, destructive without `declared_by`); rename planning in every state;
  golden files of the three code states for every field type; merge algorithm on crafted hunks (disjoint, adjacent,
  overlapping, deletions on one side, additions at the same anchor, CRLF); span primitives on the adversarial corpus;
  journal 1.1 and lock behaviour; adoption 1.1 validation.
- **Generated-project suites** (through the trust model, in the copy): V1 and V2 suites assert the API behaviour
  of the transition; the merged projects' suites; adopted fixtures' suites.
- **PostgreSQL opt-in**: the rename slice with V0 (the committed snapshot's venv and suite), V1 and V2 against one
  database; concurrent writers during the copy backfill; the drop refused and applied; the reverse-copy downgrade;
  `in_place` as a metadata rename; an adopted project's `add_columns` through its own Alembic environment; RLS table
  with a renamed non-tenant column as the owner, tenant column refused.
- **Adversarial**: textually clean merges that are semantically wrong (duplicate mount, shadowed import, both sides
  editing one function); transforms on every fixture of the corpus; identifier mentions in unmanaged files; two
  concurrent `apply` runs; fault injection matrix.
- **End to end** (acceptance module): rename of `reference` on `parcels` across three evolutions and three code
  states on live rows; a generated project with hand-edited integration points evolved twice with merges; an adopted
  fixture gaining a mounted generated module and a dependency.
- **Compatibility**: the compat artefacts extended with a 1.2 phased contract, a 1.3 manifest and a 1.0 evidence
  document; the precision harness against the v2.1.0 engine; the per-file oracle on every spec.

## 10. Recommendations

1. Treat renames as the centrepiece of v2.2.0 (increments 1 and 2): they are the most requested gap after v2.1 and
   every building block exists. Keep the three-evolution sequence; make the shortcut an explicit, documented
   declaration if it is wanted at all.
2. Build the transform engine on standard-library span primitives with hard refusals, and prove formatting with the
   project's own formatter instead of promising it. Do not take `libcst` in v2.2.
3. Ship the three-way merge opt-in, with the double-inclusion proof, and gather the adversarial corpus before
   discussing default-on.
4. Give adopted projects a real growth path (mount points, generated modules, delegation) before any in-body
   rewriting; keep "append or refuse" as the rule for hand-written code.
5. Add the lock and the multi-step journal early (increment 0) so every later increment is tested under the fault
   matrix from the start.
6. Keep every destructive step behind an explicit flag, a named person, fresh evidence and a reversible downgrade.

## 11. Decisions taken on review (2026-10-31)

The author approved the architectural direction with the adjustments below and authorised **increment 0 only**. They
override the recommendations above wherever the two differ.

1. **Scope**: the seven increments stay as the proposal; each one needs its own review and authorisation. The
   adopted-projects increment is conditional: if it threatens stability or widens the scope too much, it moves to
   v2.3.0.
2. **Renames**: `expand_contract` only in v2.2.0; `in_place` is out.
3. **Sequence**: three mandatory evolutions, `expand`, `retire` and `contract`; no destructive phase ever runs
   automatically.
4. **Destructive preconditions**: explicit human authorisation, the approver's identity, fresh evidence that
   postdates the retire evolution, equality of the data, a declared deployment compatibility and the PostgreSQL
   checks; the configurable minimum wait (24 hours by default) is kept as an **additional** condition, never as
   sufficient proof. The reversibility of the DDL and the effective recovery of the data are two different claims
   and are stated apart.
5. **Transforms**: standard library and conservative structural edits; whatever cannot be preserved with enough
   evidence is refused; no `libcst` by default.
6. **Merge**: opt-in only, per operation and per authorised file or region; semantic conflicts are never resolved
   automatically; manual regions outside the authorised scope are never modified.
7. **Adopted projects**: new modules only when the mount point is identified unambiguously and the operator
   authorised that region; unadopted files stay protected.
8. **Lock**: fail at once by default, naming the holding operation and the reason; an optional bounded wait with a
   timeout and recovery controls.
9. **Example**: `parcels` is extended; no sixth example for now.
10. **N-1 compatibility**: mandatory. The previous version's suites run against the expanded schema and the
    transition is demonstrated with the old, transitional and new versions where it applies.

Additional architectural conditions:

- a reversible schema migration is not a reversible data migration; the documents keep the two apart;
- dual write must define the source of truth, the read order, the resolution of discrepancies, the transactions and
  the behaviour under concurrent writes;
- the contract phase blocks without sufficient evidence that the incompatible earlier versions stopped using the
  old structures; an operator's declaration does not by itself demonstrate the state of every deployment;
- the three-way merge uses base, generated and modified versions and rejects conflicts without an unambiguous
  resolution;
- no historical contract, rule id, fingerprint, suppression or existing format changes without justification and
  explicit compatibility.

What increment 0 delivered under these decisions is recorded in `docs/releases/RELEASE-NOTES-v2.2.md`.

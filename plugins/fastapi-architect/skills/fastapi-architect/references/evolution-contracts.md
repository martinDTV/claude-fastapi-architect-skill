# Evolution contracts (v2.0, increments 3 and 4; v2.1 phased migrations): discovery, adoption, planned change, recoverable writer, verification

How FastAPI Architect extends a project without regenerating it from scratch and without touching what the
developer owns: **generated** projects grow by whole modules and by additive changes to existing modules;
**hand-written** projects are first *adopted* module by module and then receive anchored structural edits. The
engine reuses the design contract, the blueprint, the manifest, the audit engine and the v1.9 trust model.

```
python3 scripts/evolve_fastapi.py discover PROJECT                     # static model of any FastAPI project
python3 scripts/evolve_fastapi.py propose  PROJECT                     # candidate modules of a hand-written project
python3 scripts/evolve_fastapi.py adopt    ADOPTION.json               # record the adoption baseline (adoption/1.0)
python3 scripts/evolve_fastapi.py plan     CONTRACT.json               # change set + verification in a copy, writes nothing
python3 scripts/evolve_fastapi.py apply    CONTRACT.json               # verify in a copy, then write the project
       [--authorize-execution --trust fixture|trusted --test-command "python3 -m pytest -q"
        --env-passthrough DATABASE_URL,UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR] [--format text|json] [--output FILE] [--strict]
python3 scripts/evolve_fastapi.py recover  PROJECT [--forward]         # finish or undo an interrupted operation
```

Exit codes: `0` planned / noop / audit_clean / verified (and, for `apply`, written); `1` blocked, rejected,
inconclusive, interrupted or refused; `2` invalid contract or arguments.

## 1. Project discovery

`discover` builds a model by parsing the project (`ast`), reading `pyproject.toml`, `alembic.ini`,
`.audit-fastapi.json`, the generation manifest and the adoption baseline. It never imports or executes the
project.

| Aspect | Source | Notes |
|---|---|---|
| package, source root | manifest or a single importable package under `src/` or the root | several candidates: reported as an uncertainty, no choice is made |
| layout | `modules/<name>/` → `modular`; `api/`, `services/`, `repositories/` → `layered`; else `unknown` | never converted |
| routers, mounts, effective prefixes | `fastapi_audit.facts` (same pass as the auditor) | non-constant prefixes are `unresolved` |
| routes | `audit_project` contracts: method, path, effective paths, authentication, public | |
| pydantic models, settings | bases resolved to `pydantic.BaseModel` / `pydantic_settings.BaseSettings` | |
| SQLAlchemy models | bases resolved to `DeclarativeBase` subclasses found in the project (two passes) | the base itself is not a model |
| services, repositories, dependency aliases, lifespan | class names, `Annotated[..., Depends(...)]` aliases, `FastAPI(lifespan=...)` | heuristic; reported, not relied upon |
| Alembic | `alembic.ini` → `script_location/versions/*.py`: revisions, `down_revision`, heads, unknown parents, historical files changed since generation | |
| ownership | manifest file table vs sha256 on disk | `managed_unchanged`, `managed_modified` (manual change), `unmanaged`, `missing_managed`; with an adoption baseline `adopted_unchanged`, `adopted_modified`, `adopted_missing`, `unadopted`; `unknown` for every file when there is neither |
| baseline contract | `spec_document` embedded in a 1.1 manifest, digest-checked | a 1.0 manifest needs `--baseline-contract` |

Nothing is inferred from file names alone: a file called like a generated one is `unmanaged` unless the manifest
lists it, and a managed or adopted file with unexpected content is a manual change.

## 2. Adoption of hand-written projects (`fastapi-architect/adoption/1.0`)

Flow: `discover` → `propose` → the operator writes the contract → `adopt` → the baseline is recorded → controlled
evolution of the adopted modules only.

```json
{
  "schema": "fastapi-architect/adoption/1.0",
  "project": {"path": "/path/to/shop"},
  "modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}],
  "notes": "users stays unadopted on purpose"
}
```

- `propose` lists one candidate per router file (router, own and effective prefixes, routes, the project files
  it imports as related files), the python files nobody claims, and the uncertainties. **Nothing is adopted by a
  proposal.**
- `adopt` validates the contract against the project: a generated project is refused (use evolution), a project
  without a single application package is refused (unknown structure), every file must exist inside the project,
  not be (or sit under) a symlink, parse, belong to one module only; the module's router must be bound exactly once
  in its file and be an `APIRouter` the discovery recognises. The only file written is
  `.fastapi-architect/adoption.json` (through the recoverable writer): the adopted files with their sha256, the
  router and routes of each module, the ownership boundary ("only the listed files may be transformed; every
  other file is protected"), the notes and the history. Adopting again with the same files refreshes the baseline;
  previously adopted modules stay adopted.
- After adoption, a manual change on an adopted file (digest differs from the baseline) blocks any transform of
  that module until the baseline is refreshed by adopting again. Unadopted files are never written.

Adoption does not convert the project: no file is rewritten, no layout is imposed, no manifest claims the rest.

## 3. Evolution contract (`fastapi-architect/evolution/1.1`; 1.0 is still read; 1.2 adds the phased keys of section 9; 1.3 adds the renames of section 10)

```json
{
  "schema": "fastapi-architect/evolution/1.1",
  "project": {"path": "../generated/inventory-service", "package": "inventory_service", "expected_spec_digest": "7627b87b0d9b", "baseline_contract": null},
  "add_modules": [ { "name": "suppliers", "entities": [ ... same shape as spec.functional.modules ... ] } ],
  "modify_modules": [
    {"name": "catalog", "add_depends_on": ["suppliers"],
     "entities": [{"name": "Product", "add_fields": [{"name": "barcode", "type": "str", "max_length": 64, "required": false},
                                                      {"name": "active", "type": "bool", "required": true, "default": true}],
                   "add_operations": ["update"]}]},
    {"name": "items", "add_endpoints": [{"method": "GET", "path": "/stats", "name": "items_stats", "summary": "...", "response": {"module": "items"}}],
                      "add_fields": [{"class": "ItemIn", "name": "sku", "type": "str", "required": false}]}
  ],
  "invariants": ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"],
  "files": {"allow_modify": ["..."], "preserve": ["README.md"]}
}
```

| Key | Meaning |
|---|---|
| `project.path` / `package` / `expected_spec_digest` / `baseline_contract` | as in 1.0: the project (symlinks refused), the package check, the baseline the operator expects (`stale baseline` otherwise), the original contract for 1.0 manifests |
| `add_modules` | new modules of a generated project (regenerated from the merged contract) |
| `modify_modules[].add_depends_on` | generated: a declared dependency (cycles are rejected by the merged contract) |
| `modify_modules[].entities[].add_fields` | generated: fields appended to an existing entity; `required: false` (nullable) or `required: true` with `default` (server default = backfill); `references` only nullable |
| `modify_modules[].entities[].add_operations` | generated: new use cases, routes and tests for an existing entity |
| `modify_modules[].add_endpoints` | adopted: a handler appended to the module's router file (`method`, `path`, `name`, optional `summary`, a JSON `response` returned as is) |
| `modify_modules[].add_fields` | adopted: `class`, `name`, `type` (str, int, float, bool), optional `required` with `default` |
| `invariants`, `files.allow_modify`, `files.preserve` | as in 1.0 |

Classification of every requested change (reported under `plan.changes`):

| Classification | Examples | Outcome |
|---|---|---|
| `compatible` | nullable field; required field with a default; new operation; new dependency without a cycle; new endpoint; optional field on an adopted class | planned |
| `potentially_incompatible` | required field without default on an existing entity (existing callers would get 422; on PostgreSQL a NOT NULL column without backfill); required foreign key on an existing table | **blocked** until a safe form is declared (a `default`, or on PostgreSQL a `backfill`, section 9) |
| `unsupported` | `remove_fields`, `change_fields`, `remove_operations`, `remove_entities`, `rename_entities`, `remove_endpoints`, `remove_depends_on`, new entities inside an existing module, changes of public contracts | contract error (`rename_fields` is a phased change since 1.3, section 10) |

Applying the same contract twice is a `noop` for generated projects (the merged contract equals the baseline);
for adopted projects the same endpoint again is a duplicate and blocks (nothing is silently skipped).

## 4. The plan

`plan_evolution` computes, without writing:

1. **Preconditions**: no interrupted operation pending (`.fastapi-architect/ops`), not a symlink, manifest or
   adoption baseline present, baseline contract available and matching the manifest digest, expected digest,
   package and layout matching the manifest, no missing managed file, merged contract valid, unique routes across
   modules, linear Alembic chain with the recorded head and no historical migration modified (PostgreSQL); for
   adopted projects: every referenced module adopted, adopted files unchanged since adoption.
2. **Style**: the project keeps its layout; the advisor's new opinion is a warning and the evolved contract pins
   `architecture.style_preference`.
3. **Files** (generated): the baseline blueprint (what every managed file should contain now) against the evolved
   blueprint (what it should become) and against the disk: `create`, `modify` (integration points and, for
   `modify_modules`, the module's own files; only when managed, unchanged, allowed and not preserved), `unchanged`
   (never written, manual changes among them listed as `preserved_manual_changes`), `kept` (historical migrations),
   conflicts (`file_in_the_way`, `manual_change_on_integration_point`, `unmanaged_file_in_the_way`,
   `not_allowed_to_modify`, `preserved_file_would_change`, `missing_managed_file`, `unsafe_path`); the manifest is
   always rewritten against its current digest.
   **Files** (adopted): the transformed router or schema files (`transforms` carry the inserted symbols, the AST
   evidence and the digests before and after) and the refreshed adoption baseline.
4. **Public contracts**: routes added, removed (an invariant violation), declared contracts changed.
5. **Migration** (PostgreSQL): one additive revision with `down_revision` = current head: `create_table` for new
   tables, `add_column` (nullable, or NOT NULL with a server default) for new columns of existing tables,
   `create_foreign_key` + index for nullable references; `downgrade` drops them (the data of a dropped column goes
   with it, stated under risks). No `alter_column`, no drop of existing objects, no data migration, no merge of
   heads. `tests/test_migrations.py` is regenerated with the new head. With contract 1.2 phased keys the revision
   becomes an expand revision, an index revision and, in a later authorised evolution, a contract revision
   (section 9); `tests/test_backfill.py` is generated with them.
6. **Risks**, **tests required** and unified diffs of every modified file.

## 5. Structural transforms (adopted projects)

`transforms.py` analyses with `ast` and edits by **anchored insertion** into the original text: the new handler
is appended at the end of the router module (two blank lines), the new field after the last statement of the
class body at its indentation, a missing import after the last head import. Everything else stays byte-for-byte:
formatting, comments and hand-written code are never rewritten. For every transform: the target symbol is
identified (router bound exactly once, class defined exactly once), preconditions are checked (duplicate symbol,
duplicate route with the same method and path on the router, duplicate field), the result is parsed and its AST
compared with the original (every pre-existing top-level node unchanged and in order, exactly the inserted nodes
added), a unified diff is produced and the evidence (anchor, symbol, AST counts, digests) goes into the plan.
Ambiguity refuses: two bindings of the router, two classes of the same name, a class defined in several adopted
files. The standard library is enough for additive edits; a concrete-syntax-tree dependency would only be
justified to rewrite existing nodes, which this increment does not do.

## 6. Recoverable writer

`journal.py` replaces the plain two-phase writer in every path that writes a project (`apply`, `adopt`):

| Step | What happens | What it guarantees |
|---|---|---|
| plan identity | the operation id is the sha256 of (paths, expected digests, new digests, kind, description) | the same plan has the same id; a retry after completion is a different, no-op plan |
| `prepare` | preconditions (paths inside the project, no symlink on the way, every expected digest or absence) re-checked; new content and backups of existing files staged under `.fastapi-architect/ops/<id>/`; journal written with state `prepared` | nothing in the project changes; a failure here leaves no staging |
| `publish` | journal → `publishing`; preconditions re-checked once more (a concurrent change aborts with nothing published); each file written to a temporary sibling and renamed into place (`os.replace`, atomic per file on POSIX, same filesystem); the journal records every landed path; → `published`; staging removed; journal deleted | an interruption leaves a journal that says exactly which files landed |
| `recover` | files classified by current digest: `published`, `pending`, `conflict`; `rollback` restores the backups of published files and removes files the operation created; `forward` publishes the pending files from the staging copies; conflicts (edited after the crash) are never touched and keep the journal (`needs_attention`); progress is journaled, so an interrupted recovery resumes and a repeated recovery changes nothing | a crash after any number of files is recoverable without losing anyone else's changes |

An operation directory whose journal is missing, truncated, not JSON or of another schema is never skipped
(v2.0 acceptance): `plan` blocks with `no_unreadable_operation`, `recover` lists it under `unreadable` as
`needs_attention` with the number of staged files kept, exits 1 and touches nothing; a human decides from the staged
content and the backups. Limits, stated plainly: there is no global atomicity across files; a crash between two renames leaves a mixed
tree until `recover` runs (a new `plan` refuses while a journal is pending); the staging directory must be on
the same filesystem as the project for the renames to be atomic; nothing protects against a writer that ignores
the journal. Durability (v2.1): every write of the protocol is fsync'ed, the file before the rename and the directory
entry after it, and the operation directories when they are created, so once `apply` or `recover` returns its effects
survive a crash or a power loss on a filesystem that honours `fsync`. Not guaranteed: the drive's own cache on macOS
(`F_FULLFSYNC` is not issued), directory entries on Windows, and filesystems that refuse to sync a directory (the file
is synced, the entry is not, nothing fails). A failed sync of a file is an error, never a silent downgrade: `apply`
reports `write_failed` (nothing landed) or `interrupted` (the journal is kept for `recover`). The verification copy is
written without fsync. The tests inject failures before publication, after the n-th file, after the last file, before
cleanup and during recovery. Since v2.2 (increment 0) one writer at a time touches a project: `apply`, `adopt` and
`recover` take `.fastapi-architect/ops/LOCK` (holder pid, host, start time, operation), fail at once when it is held
(`applied.state: locked`, exit 3 for `recover`; `recover --wait SECONDS` bounds an optional wait), never break a stale
lock silently (`evolve_fastapi.py ops unlock` removes one whose holder is gone, after naming it); the journal is
`fastapi-architect/operation/1.1` with `steps` (what produced each file: `regenerate`, `transform`, `manifest`, later
`merge`) and `parent_op`, 1.0 journals are still read and recovered; `ops list` and `ops show` print pending
operations, their steps and the lock.

## 7. Verification and states

`apply` (and `plan` unless `--no-verify`) applies the plan to a **copy** of the project and audits it against
the project by fingerprint (new high/critical findings reject), checks that every existing route keeps its path
and authentication, checks that nothing outside the plan changed, and runs the tests only under the trust model
(the operator's scope covers the project and is moved to the copy; with the local executor `untrusted` fails
closed and the executor is process hygiene, not a sandbox; with `--executor container` (v2.1) the tests run in an
OCI container with restrictions verified per run, and `verification.tests.run.environment` records them; the
report schema is `evolution_report/1.3`, additive over 1.1 and 1.2: `contract_authorization` and the phased keys of
`plan.migration`, section 9). Then, and only after `verified` or `audit_clean`,
the project is written through the recoverable writer.

| State | Meaning |
|---|---|
| `planned` | preview only (`--no-verify`) |
| `noop` | already applied (generated) |
| `blocked` | a precondition failed (the `contract_*` ones of section 9 among them) or a conflict exists; nothing was tried |
| `verified` | copy audited, invariants hold, protected files untouched, tests executed and passed |
| `audit_clean` | same without executed tests: not `verified` |
| `inconclusive` | tests could not run (denied, refused command, unverifiable runner); never success |
| `rejected` | new blocking findings, changed routes, files touched outside the plan, failing tests, or a write that failed before anything landed |
| `interrupted` | the publication to the project stopped after some files landed; the journal is pending, `recover` finishes or undoes it |

## 8. Limits of this increment

- Generated projects: new modules, new nullable or defaulted fields, new operations and dependencies on existing
  modules; no removals, renames, type changes, new entities inside an existing module, or layout migrations.
- Adopted projects: handlers that return a literal and optional fields on pydantic classes, inside adopted files
  only; no changes to existing handlers, no generated modules, no persistence.
- Migrations: one additive revision per evolution (tables, nullable or defaulted columns, nullable foreign keys);
  since v2.1 the phased form of section 9 (expand, concurrent index, declarative backfill, authorised contract);
  renames, drops, column type changes, arbitrary data migrations and merges of heads are not supported.
- `verified` is as strong as the executed suite, through the local executor's process hygiene or the container
  executor's verified restrictions.

## 9. Phased migrations (contract `fastapi-architect/evolution/1.2`, v2.1; PostgreSQL projects)

A required column that existing rows cannot satisfy, or an index on a table with live rows, is not one additive
revision. Contract 1.2 declares it as phases; the plan appends the revisions and the programs, and three states stay
apart: **declared** (the contract and the manifest), **applied** (a concrete database) and **verified** (evidence
written against that database). The plugin never connects to a database.

```json
{
  "schema": "fastapi-architect/evolution/1.2",
  "project": {"path": "../generated/parcels"},
  "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
     "add_fields": [{"name": "carrier", "type": "str", "max_length": 30, "required": true,
                     "backfill": {"strategy": "constant", "value": "unknown"}},
                    {"name": "tracking_token", "type": "uuid", "required": true, "backfill": {"strategy": "uuid"}}],
     "add_indexes": [{"columns": ["carrier"], "unique": false}]}]}]
}
```

```json
{
  "schema": "fastapi-architect/evolution/1.2",
  "project": {"path": "../generated/parcels"},
  "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
     "contract_fields": [{"name": "carrier", "operation": "66636e4277641692"}]}]}],
  "authorization": {"contract": {"declared_by": "release manager", "evidence": "evidence.json", "max_evidence_age_hours": 24}}
}
```

| Key | Meaning |
|---|---|
| `add_fields[].backfill` | `strategy`: `constant` (with `value`, a literal of the field's type, bound as a parameter), `copy_column` (with `source`, an existing column of the same entity and type), `uuid` (uuid fields), `now` (datetime fields). Incompatible with `default` and `references`. The field is classified `compatible` and planned as a phased change |
| `add_indexes[]` | `columns` (exactly one existing or added column), optional `unique`; built `CONCURRENTLY` in a non-transactional revision |
| `contract_fields[]` | `name` and the `operation` id (16 hex characters) the expand plan printed; declares the NOT NULL transition of a column whose manifest state is `expand` |
| `authorization.contract` | `declared_by` (required for a contract), `evidence` (path of the migration evidence; `--evidence` on the command line overrides it), `max_evidence_age_hours` (24 by default) |

Rejected with the path: these keys under 1.0 or 1.1; an unknown strategy; a value of the wrong type; a `source`
that does not exist or has another type; a `backfill` on a non-PostgreSQL project; an index with several columns
or an unknown column; a `contract_fields` entry whose operation is not pending in the manifest; any SQL (the contract
never carries SQL; the generated runner builds its statements from validated identifiers and bound parameters).

What the plan appends (`plan.migration`, keys additive over 1.1): `phases[]` (`phase`, `revision`, `transactional`,
`reversible`, `lock_profile`, `preconditions`, `evidence_required`, `state_after`), `backfills[]` (operation id,
table, column, strategy, parameters, the `application_version` that writes the column, the `run` and `verify`
commands), `contract` (the operations contracted), `head_after` and `revisions`. The phases:

| Phase | Revision | What it does | Lock profile |
|---|---|---|---|
| `expand` | transactional, reversible | `ADD COLUMN ... nullable`, no server default; the progress table `<pkg>_backfill_progress` the first time; the model nullable, the create schema requiring the value, the read schema optional | brief ACCESS EXCLUSIVE, no rewrite |
| `index` | non-transactional (`autocommit_block`), reversible | `CREATE INDEX CONCURRENTLY`; compares `pg_get_indexdef` with the expected definition (a different definition fails), drops and rebuilds an invalid leftover, keeps a valid identical one, fails rather than end with `indisvalid = false` | SHARE UPDATE EXCLUSIVE, two scans |
| backfill (not a revision) | `python -m <pkg>.ops.backfill run OP_ID` | keyset batches on the primary key, `FOR UPDATE SKIP LOCKED`, `UPDATE ... WHERE column IS NULL`, rows and checkpoint in one transaction, one worker per operation (advisory lock), checksum-guarded checkpoint, refuses RLS tables unless the role bypasses RLS, `complete` only after counting zero pending rows (up to three passes); a run on a complete operation re-counts and reopens it only for rows that appeared afterwards | row locks only |
| verify (not a revision) | `python -m <pkg>.ops.backfill verify --application-version DIGEST --output FILE` | writes `fastapi-architect/migration-evidence/1.0`: database, `alembic_version` against the expected head, every operation (column present, nullable, pending rows, progress record, complete), every index (present, valid, definition), the declared application version, `ok` and `problems` | reads |
| `contract` | non-transactional, reversible (`DROP NOT NULL`) | re-checks pending rows and the progress state on the live database (an empty table without a record counts as complete and records it), `ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID`, `VALIDATE CONSTRAINT`, `SET NOT NULL`, `DROP CONSTRAINT`; idempotent step by step; the model and the read schema become non-optional | brief ACCESS EXCLUSIVE at the metadata steps, SHARE UPDATE EXCLUSIVE during validation |

A contract plan (`plan` or `apply` with `--authorize-contract`) lists every precondition under `plan.preconditions` and
blocks when one fails: `contract_authorized`, `contract_operation_pending`, `contract_evidence_present`,
`contract_evidence_valid`, `contract_evidence_fresh`, `contract_database_at_head`, `contract_evidence_matches`,
`contract_column_present`, `contract_backfill_complete`, `contract_no_pending_rows`,
`contract_application_version_accredited`, `contract_indexes_valid`, `contract_reversible`. The report records
`contract_authorization` (flag, evidence path, `declared_by`). A contract is never the automatic continuation of a
backfill, and no contract phase drops a column, a table or an index.

Exit codes of the runner: 0 done, 1 usage, 2 connection, 3 another worker holds the operation, 4 checkpoint refused
(foreign definition or corrupt key), 5 row-level security without a bypassing role, 6 incomplete after the passes
(an application version that does not write the column is live), 7 unknown operation or table.

The manifest records the phased changes (`fastapi-architect/generation/1.3`, `references/design-contracts.md`),
`docs/guides/phased-migrations-and-backfills.md` walks the fifth example through the four phases, and
`docs/architecture/adr-2.1-01-phased-migrations.md` records the decisions and what stays deferred (renames with
dual-write, drops, type changes, multi-column indexes, per-tenant backfills under RLS).

## 10. Renames in transition (contract `fastapi-architect/evolution/1.3`, v2.2 increment 1; PostgreSQL projects)

```json
{
  "schema": "fastapi-architect/evolution/1.3",
  "project": {"path": "../generated/parcels"},
  "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
     "rename_fields": [{"from": "reference", "to": "label", "strategy": "expand_contract"}]}]}]
}
```

| Key | Meaning |
|---|---|
| `rename_fields[].from` / `to` | the current and the new field name (lowercase identifiers, different) |
| `rename_fields[].strategy` | `expand_contract` (the only one): the new column is added next to the old one and the rename moves through expand, transition, retire and contract; `in_place` is refused by name (`ALTER TABLE … RENAME COLUMN` is not online across application versions) |
| `rename_fields[].sync` | `trigger` (the only one): the mechanism that mirrors the writes of an application version that knows only the old name |

Refused with the path or the precondition: an unknown key, a strategy or a sync other than the ones above, the same
name twice, two renames sharing a source or a target, a source that is not a field (`rename_source_exists`), a target
that exists (`rename_target_free`), the identity, ownership, tenant and timestamp columns and foreign keys
(`rename_protected_column`), a column already in a phased change (`rename_column_in_transition`), an in-memory
project or a new table (`rename_needs_existing_postgresql_table`), a source column that a concurrent index covers
(`rename_indexed_column_unsupported`, since increment 2: the index would vanish with the column at the contract), a
source field that declares a default (`rename_defaulted_column_unsupported`: a column default makes an INSERT through
one name indistinguishable from an INSERT through both, so the trigger could take the default for a value). A
repeated rename is a no-op. `contract_fields` naming a rename that was not retired is refused
(`contract_rename_retire_first`); `remove_fields` and `change_fields` stay refused. Retire and contract are the two
further evolutions of section 11; nothing is renamed in place.

What the plan produces (one `expand` revision plus a `transition` entry under `plan.migration.phases`, and
`plan.migration.renames[]`):

| Piece | Content |
|---|---|
| the expand revision (transactional, reversible) | `ADD COLUMN <to>` nullable next to `<from>`, the progress table once, and one **sync trigger** per rename: `BEFORE INSERT OR UPDATE FOR EACH ROW`, whichever column the writer touched the other follows inside the same statement, the old column winning when one statement sets both; it only assigns to `NEW` (no recursion), runs as the writer (`SECURITY INVOKER`) and leaves row-level security to the policies; the downgrade drops trigger, function and column (the old column holds every value) |
| the phased operation | kind `rename`, strategy `mirror_column` (new := old wherever `new IS DISTINCT FROM old`), `source_of_truth: from_column`, `read_policy: from_column`, `write_policy: both`, the sync names; recorded in the manifest 1.4 and in `ops/backfills.py` |
| the transitional code (version B) | model with both columns (`<to>` nullable, `<from>` as before, the source of truth); request schemas accepting both names (`validation_alias=AliasChoices(to, from)`); read schema filling `<to>` from the old column and exposing `<from>` deprecated; services writing both on create and update; module tests for both names |
| the runner | `python -m <pkg>.ops.backfill run <op>` reconciles the rows written before the trigger (pre-existing rows, or drift injected behind a disabled trigger) with the v2.1 semantics: resumable, one worker, locked rows skipped and revisited, `complete` after a zero count, reopened for rows that differ later; `verify` (evidence 1.1) adds `mismatch_rows`, `columns_agree`, `source_of_truth` and the trigger state, and reports a missing or disabled trigger as a problem |
| the policies, stated | **source of truth**: the old column until retire. **Read**: B reads the old column. **Write**: B writes both; A writes the old one; the trigger mirrors either. **Coexistence**: A (old name only) and B (both) run together on the expanded schema; C (new name only) needs the retire phase. **Verified equivalence**: `mismatch_rows == 0` with the trigger enabled |

The auditor's migration rules report nothing for the sync trigger (0 findings before and after on the fifth example);
its visibility comes from the plan, the manifest, the runner's definitions, the evidence and the generated tests.
`docs/guides/phased-migrations-and-backfills.md` walks the fifth example through it and
`docs/architecture/adr-2.2-01-safe-renames.md` records the decision.

## 11. Retire and destructive contract of a rename (contract `fastapi-architect/evolution/1.4`, v2.2 increment 2; PostgreSQL projects)

A rename is three evolutions. After the expand of section 10, the backfill and a fresh `verify`:

```json
{
  "schema": "fastapi-architect/evolution/1.4",
  "project": {"path": "../generated/parcels"},
  "authorization": {"contract": {"declared_by": "release owner", "evidence": "evidence-b.json", "max_evidence_age_hours": 24}},
  "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
     "retire_fields": [{"name": "label", "operation": "550095ef4c391dd0"}]}]}]
}
```

and, once the retired generation is deployed and verified again:

```json
{
  "schema": "fastapi-architect/evolution/1.4",
  "project": {"path": "../generated/parcels"},
  "authorization": {
    "contract": {"declared_by": "release owner", "evidence": "evidence-c.json", "max_evidence_age_hours": 24},
    "destructive": {"approved_by": "release owner", "old_versions_stopped": true, "min_retire_age_hours": 24,
                    "ignore_sessions": [], "reviewed_functions": []}
  },
  "modify_modules": [{"name": "shipments", "entities": [{"name": "Shipment",
     "contract_fields": [{"name": "label", "operation": "550095ef4c391dd0"}]}]}],
  "invariants": ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "protected_files_unchanged"]
}
```

| Key | Meaning |
|---|---|
| `retire_fields[] {name, operation}` | the new field name and the operation id of a rename declared `expand`: its truth moves to the new column. Needs `authorization.contract.declared_by` and the evidence; no command-line flag (the step is reversible and online) |
| `contract_fields[] {name, operation}` on a rename declared `retire` | the **destructive contract**: the old column, its sync trigger and function are dropped (`RESTRICT`). Needs everything the NOT NULL contract needs plus `authorization.destructive`, `--authorize-contract` **and** `--confirm-irreversible "<phrase>"` |
| `authorization.destructive.approved_by` | required: the person who approves the drop (recorded in the manifest and in the revision's docstring) |
| `authorization.destructive.old_versions_stopped` | the operator's explicit declaration that no version mapping the old column is running; never sufficient on its own: the sessions of the evidence are checked too |
| `authorization.destructive.min_retire_age_hours` | the minimum age of the retire, measured from the `retired_at` the retire revision stamped in the database (default 24, `0` allowed); a wait is never proof that older instances are gone |
| `authorization.destructive.ignore_sessions` | `application_name` values of external clients (monitoring, consoles) the session check may ignore, by name |
| `authorization.destructive.reviewed_functions` | `schema.function` names whose source mentions the table and the old column and that a human reviewed; catalog dependents (indexes, constraints, views, policies, foreign keys) can never be reviewed away |
| the invariant `no_destructive_schema_change` | must be **left out** of a destructive contract, on purpose; with it the plan blocks (`no_destructive_schema_change`) |

The `--confirm-irreversible` phrase is computed by the plan and printed in the blocker when missing or wrong: `drop
<table>.<old column>[, <table>.<old column>]`. It is distinct from `--authorize-contract` and never implied by it.

**Retire** (`retire_*` preconditions, all from the evidence of B's runner): operation known, `declared_by` present,
evidence present, fresh, at the manifest head and about this rename, sync trigger present and enabled, the function's
truth marker on the old column, zero rows where the columns differ, backfill complete, the transitional application
version declared, indexes valid. What it produces: the code of state **C** (the new name only: no alias, no dual
write, no deprecated field; the fresh code of the contract under the new name) and a `retire` revision (autocommit,
reversible) that re-checks the trigger and the equality on the live database, replaces the sync function under the same
trigger, carries the old column's NOT NULL (online: `NOT VALID`, `VALIDATE`, `SET NOT NULL`) and default to the new one
and stamps the function comment `fastapi-architect sync op=<id> truth=<new> retired_at=<now>`. Nothing is dropped; the
old column stays, equal by the trigger, for B. The manifest entry becomes `state_declared: retire` with
`retire_revision`, `source_of_truth`/`read_policy`/`write_policy: to_column` and `compatible_tags` (the application
tags of every generation that does not map the old column). Downgrade: `DROP NOT NULL`, `DROP DEFAULT`, the comment back
to the old column; no data moves.

**Destructive contract** (the sixteen `contract_*` preconditions the plan evaluates; the revision re-checks the
database-side ones under `ACCESS EXCLUSIVE` with `lock_timeout = 5s`):

| # | Precondition | Satisfied when |
|---|---|---|
| 1 | `contract_operation_known` | the operation is a rename the manifest records, with its stable identity |
| 2 | `contract_rename_retired`, `contract_retire_applied` | declared `retire` in the manifest; the function comment records `truth=<new>`; the evidence was written by the retired generation's runner |
| 3 | `contract_evidence_present`, `_fresh`, `_after_retire` | evidence present, within `max_evidence_age_hours`, generated after `retired_at` |
| 4 | `contract_columns_agree` | zero rows where the columns differ |
| 5 | `contract_backfill_complete` | progress `complete` |
| 6 | `contract_application_version_accredited` | the operator declared the retired generation's contract digest (`verify --application-version`) |
| 7 | `contract_deployments_identified`, `contract_old_versions_declared_stopped` | every session of the database (a snapshot of `pg_stat_activity` taken by `verify`) carries a tag in `compatible_tags`, is the runner's own (`<pkg>.ops.backfill`) or is named in `ignore_sessions`; the operator declared `old_versions_stopped` |
| 8 | `contract_retire_age` | `now - retired_at >= min_retire_age_hours` |
| 9 | `contract_constraints_carried` | the new column has the old one's NOT NULL, type and default |
| 10 | `contract_dependencies_reviewed` | `pg_depend` lists nothing on the old column beyond its own default and NOT NULL; no other trigger function of the table and no user function (outside `reviewed_functions`) names the table and the column |
| 11 | `contract_single_head` | one head on disk, one row in `alembic_version` |
| 12 | `contract_baseline_clean` | no managed file edited by hand |
| 13 | `contract_authorized` | `--authorize-contract` and `authorization.contract.declared_by` |
| 14 | `contract_approver_identified` | `authorization.destructive.approved_by` |
| 15 | `contract_irreversible_confirmed` | `--confirm-irreversible` equals the printed phrase |
| 16 | `contract_no_pending_operations` | no interrupted operation in the journal (every plan refuses otherwise), no project lock held by another process |

The revision evaluates the time-dependent ones again on the database clock when it runs, because any amount of time
may pass between the plan and `alembic upgrade head`: the connected sessions (`COMPATIBLE_TAGS`, `IGNORED_SESSIONS` and
the runner's own name are generated into the file), the age of the evidence the plan read (`EVIDENCE_GENERATED_AT`
against `max_evidence_age_hours`) and the age of the retire (`retired_at` from the function comment against
`min_retire_age_hours`). The downgrade sets the same `lock_timeout` and fails rather than queue.

Plus the informational `contract_database_at_head`, `contract_evidence_matches`, `contract_sync_enabled`,
`contract_indexes_valid`, `contract_restrict_only` and `contract_data_recovery_declared`.

What it produces: the `drop` revision (**one transaction**: `SET LOCAL lock_timeout`, `LOCK TABLE … ACCESS EXCLUSIVE`,
every check above against the live catalog, `DROP TRIGGER`, `DROP FUNCTION`, `ALTER TABLE … DROP COLUMN … RESTRICT`;
never `CASCADE`; a failure anywhere rolls the whole revision back and `alembic_version` does not move), the code of
C regenerated (unchanged in its modules), the manifest entry `state_declared: contract` with `contract_revision`,
`dropped {column, trigger, function}` and `destructive {approved_by, declared_by, confirmation, evidence_generated_at}`,
and `history[].phased.dropped`. The approver and the phrase are also written in the revision's docstring.

Reversibility, kept apart: **DDL reversibility is conditional** (the downgrade recreates the column nullable, copies the
new column's *current* values back, restores NOT NULL and default, recreates the sync function and trigger with the
truth on the new column and a fresh `retired_at`; a full-table `UPDATE` in one transaction: offline by nature);
**data recovery is not provided** (the content the old column had at the time of the drop is not recoverable from the
database; after the checks it equalled the new column, which stays; a backup taken before the upgrade is the only way
back to the historical content); **compatibility restoration** means stop, downgrade, redeploy B; there is **no online
rollback** after a destructive contract.

What is known about deployments, kept apart in `plan.migration.phases[contract].assurance` (and summarised in
`plan.risks`): **human declaration** (`declared_by`, `approved_by`, `old_versions_stopped`, the two command-line
flags: who decided, never evidence that older instances are gone); **observed sessions** (a snapshot at verify time,
taken again by the revision under the lock); **schema state** (the truth marker, `retired_at`, NOT NULL and type
carried, zero differing rows, the dependents, the trigger, one head: measured by `verify` and again by the revision);
**automated verification** (freshness, evidence newer than the retire, the minimum age, the declared digest);
**residual uncertainty** (instances idle at both observations, clients behind a pooler, code that names the column
without connecting under its own name, functions found by text only, the minimum age being a policy). The evidence
document carries the same limits in `limits[]`.

Application tags: every generated application announces `application_name = <package>@<12 hex>` to PostgreSQL
(`core/db.py`), a hash of the package and the declared state of every phased change, so A (no tag, generated before
v2.2), B and C carry different names; `verify` lists the sessions grouped by name; the plan refuses the drop while a
session is unidentified or belongs to a generation that still maps the old column. The snapshot sees connected
sessions only: instances that connect intermittently are not seen, which is why the declaration, the age and the
sessions are all required and none is sufficient. A contract with an explicit `files.allow_modify` list must include
`src/<pkg>/core/db.py` for evolutions that change a phased state.

Evidence `fastapi-architect/migration-evidence/1.2` (1.0 and 1.1 still read; a drop needs 1.2): rename operations add
`source_present`, `columns {<old>, <new>: present, nullable, type, default}`, `sync.function`, `sync.truth`,
`sync.retired_at`, `dependents[] {kind, deptype, description, managed}`, `unmanaged_dependents[]`, `other_triggers[]
{name, function, mentions_column}`, `functions_mentioning[]`; the document adds `sessions[] {application_name, user,
count, oldest_backend_start, identified}` and `tool_session`. Report `evolution_report/1.5`: `plan.migration.retires[]`,
destructive entries in `plan.migration.contract[]` (`destructive: true`, `reversible: "conditional"`, `drops`,
`confirmation`, `approved_by`), the `retire` phase and a destructive `contract` phase (`ddl_reversibility`,
`data_recovery`, `compatibility_restoration`), `plan.phased.retiring/dropping`, `history[].phased.retired/dropped`,
`contract_authorization.confirm_irreversible` and `approved_by`, and the change classification `destructive`.
Examples: `examples/evolutions/parcels-retire-label.json`, `parcels-contract-label.json`, with
`parcels-rename-evidence.sample.json` and `parcels-retire-evidence.sample.json`.

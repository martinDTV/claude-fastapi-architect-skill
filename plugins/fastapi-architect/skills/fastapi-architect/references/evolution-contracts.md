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

## 3. Evolution contract (`fastapi-architect/evolution/1.1`; 1.0 is still read; 1.2 adds the phased keys of section 9)

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
| `unsupported` | `remove_fields`, `rename_fields`, `change_fields`, `remove_operations`, `remove_entities`, `rename_entities`, `remove_endpoints`, `remove_depends_on`, new entities inside an existing module, changes of public contracts | contract error |

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

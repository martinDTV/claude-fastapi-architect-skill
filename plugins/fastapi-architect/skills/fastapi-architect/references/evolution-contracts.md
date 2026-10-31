# Evolution contracts (v2.0, increments 3 and 4): discovery, adoption, planned change, recoverable writer, verification

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

## 3. Evolution contract (`fastapi-architect/evolution/1.1`; 1.0 is still read)

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
| `potentially_incompatible` | required field without default on an existing entity (existing callers would get 422; on PostgreSQL a NOT NULL column without backfill); required foreign key on an existing table | **blocked** until a safe form is declared |
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
   heads. `tests/test_migrations.py` is regenerated with the new head.
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

Limits, stated plainly: there is no global atomicity across files; a crash between two renames leaves a mixed
tree until `recover` runs (a new `plan` refuses while a journal is pending); the staging directory must be on
the same filesystem as the project for the renames to be atomic; nothing protects against a writer that ignores
the journal. The tests inject failures before publication, after the n-th file, after the last file, before
cleanup and during recovery.

## 7. Verification and states

`apply` (and `plan` unless `--no-verify`) applies the plan to a **copy** of the project and audits it against
the project by fingerprint (new high/critical findings reject), checks that every existing route keeps its path
and authentication, checks that nothing outside the plan changed, and runs the tests only under the trust model
(the operator's scope covers the project and is moved to the copy; `untrusted` fails closed; the local executor
is process hygiene, not a sandbox). Then, and only after `verified` or `audit_clean`, the project is written
through the recoverable writer.

| State | Meaning |
|---|---|
| `planned` | preview only (`--no-verify`) |
| `noop` | already applied (generated) |
| `blocked` | a precondition failed or a conflict exists; nothing was tried |
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
  expand/contract, column changes, data migrations and merges of heads are not supported.
- `verified` is as strong as the executed suite, through the local executor's process hygiene.

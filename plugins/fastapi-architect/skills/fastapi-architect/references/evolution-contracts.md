# Evolution contracts (v2.0, increment 3): discovery, planned change, safe writer, verification

How FastAPI Architect extends a **generated** project with new business modules without regenerating it from
scratch and without touching what the developer owns. The engine reuses the design contract, the blueprint, the
manifest, the audit engine and the v1.9 trust model; nothing here uses text replacement: every written file is
regenerated from the merged contract.

```
python3 scripts/evolve_fastapi.py discover PROJECT                     # static model of any FastAPI project
python3 scripts/evolve_fastapi.py plan  CONTRACT.json                  # change set + verification in a copy, writes nothing
python3 scripts/evolve_fastapi.py apply CONTRACT.json                  # verify in a copy, then write the project
       [--authorize-execution --trust fixture|trusted --test-command "python3 -m pytest -q"
        --env-passthrough DATABASE_URL,UV_CACHE_DIR,UV_PYTHON_INSTALL_DIR] [--format text|json] [--output FILE] [--strict]
```

Exit codes: `0` planned / noop / audit_clean / verified (and, for `apply`, written); `1` blocked, rejected,
inconclusive or refused; `2` invalid contract or arguments.

## 1. Project discovery

`discover` builds a model by parsing the project (`ast`), reading `pyproject.toml`, `alembic.ini`,
`.audit-fastapi.json` and the generation manifest. It never imports or executes the project. It reports:

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
| ownership | manifest file table vs sha256 on disk | `managed_unchanged`, `managed_modified` (manual change), `unmanaged`, `missing_managed`; `unknown` for every file when there is no manifest |
| baseline contract | `spec_document` embedded in a 1.1 manifest, digest-checked | a 1.0 manifest needs `--baseline-contract` |

Nothing is inferred from file names alone: a file called like a generated one is `unmanaged` unless the manifest
lists it, and a managed file with unexpected content is a manual change.

## 2. Evolution contract (`fastapi-architect/evolution/1.0`)

```json
{
  "schema": "fastapi-architect/evolution/1.0",
  "project": {"path": "../generated/inventory-service", "package": "inventory_service", "expected_spec_digest": "7627b87b0d9b", "baseline_contract": null},
  "add_modules": [ { "name": "suppliers", "description": "...", "depends_on": ["catalog"], "entities": [ ... same shape as spec.functional.modules ... ] } ],
  "invariants": ["existing_routes_unchanged", "historical_migrations_unchanged", "declared_contracts_unchanged", "no_destructive_schema_change", "protected_files_unchanged"],
  "files": {"allow_modify": ["src/inventory_service/api/router.py", "..."], "preserve": ["README.md"]}
}
```

| Key | Meaning |
|---|---|
| `project.path` | the project (relative to the contract file or absolute); symlinks are refused |
| `project.package` | checked against the manifest and the package on disk |
| `project.expected_spec_digest` | prefix of the baseline contract digest the operator expects; a different baseline is `stale baseline` |
| `project.baseline_contract` | the original contract, for manifests that predate 1.1; its digest must match the manifest |
| `add_modules` | new modules, validated as part of the merged contract (identifiers, reserved names, cycles, references along the dependency direction, duplicates); errors are reported with `evolution.add_modules[i]...` paths |
| `invariants` | checked by the plan (routes removed, historical migrations modified) and by the verification in the copy (routes changed, files touched outside the plan) |
| `files.allow_modify` | globs of the files the evolution may regenerate; default: the integration points (`pyproject.toml`, `README.md`, `.env.example`, `.audit-fastapi.json`, `main.py`, `api/router.py`, `core/config.py`, `migrations/env.py`, `tests/conftest.py`, `tests/test_migrations.py`) plus the manifest |
| `files.preserve` | globs that must never be written; a preserved file the evolution would need to change is a conflict |

Entities, operations, persistence, security, integrations and style are **inherited** from the baseline contract:
the evolution adds modules to it, it does not reinterpret it. Applying the same contract twice is a `noop`;
adding a module that exists with a different definition is refused (changing modules is not supported yet).

## 3. The plan

`plan_evolution` computes, without writing:

1. **Preconditions**: not a symlink, manifest present and known, baseline contract available and matching the
   manifest digest, expected digest, package and layout matching the manifest, no missing managed file, merged
   contract valid, unique routes across modules, Alembic chain linear with the head recorded in the manifest and
   no historical migration modified (PostgreSQL).
2. **Style**: the project keeps its layout. When the advisor would now recommend another style (three modules in a
   layered project, for instance) the plan records a warning and pins `architecture.style_preference` in the
   evolved contract; migrating the layout is a separate, explicit operation that this increment does not offer.
3. **Files**, by comparing the baseline blueprint (what every managed file should contain now) with the evolved
   blueprint (what it should become) and with the disk:
   - `create`: files of the new modules (a file already there is `file_in_the_way`);
   - `modify`: integration points whose generated content changes, only when managed, unchanged, allowed and not
     preserved; otherwise `manual_change_on_integration_point`, `unmanaged_file_in_the_way`, `not_allowed_to_modify`,
     `preserved_file_would_change` or `missing_managed_file`;
   - `unchanged`: files whose generated content does not change are never written, whatever their state on disk;
     managed files with manual changes among them are listed as `preserved_manual_changes`;
   - `kept`: historical migration files, byte-identical;
   - the manifest is always rewritten against its current digest (history, files, migrations, contract).
4. **Public contracts**: routes added, removed (an invariant violation) and whether `.audit-fastapi.json` changes.
5. **Migration** (PostgreSQL): one additive revision (`create_table`, foreign keys to existing tables, indexes;
   no drop, no alter) with `down_revision` = current head and an id derived from the head and the evolved contract;
   `tests/test_migrations.py` is regenerated with the new head and the full table list.
6. **Risks** and **tests required**; unified diffs of every modified file for the preview.

A plan with any conflict or failed precondition is `blocked` and lists why.

## 4. Safe writer

`generator.write_files(target, files, expected)` is shared by generation and evolution:

| Guarantee | How |
|---|---|
| preview | `plan` (and `apply` before writing) report the full change set and diffs |
| identity and digest | sha256 per file in the manifest; ownership derived from it |
| baseline check | `expected_spec_digest`, manifest digest of the baseline contract, head revision |
| atomic writes | temporary sibling files, then `os.replace`; the first failure removes the temporaries |
| conflicts | every precondition digest re-checked immediately before writing: a concurrent change aborts before anything is written |
| manual files | never regenerated: integration points with manual changes block, other managed files with manual changes are preserved |
| idempotency | same contract twice → `noop`; no file written |
| unsafe paths | absolute paths, `..`, backslashes, surrounding spaces refused; the resolved path must stay inside the project |
| traversal / symlinks | a symlinked project is refused; writing through a symlinked directory or file inside the project is refused |
| no silent overwrite | a file this evolution does not need to change is never written; a differing file is a conflict, not a replacement |

## 5. Verification and states

`apply` (and `plan` unless `--no-verify`) applies the plan to a **copy** of the project (`Workspace.create`) and:

- audits the copy with the static engine and compares it by fingerprint with the audit of the project
  (`compare_reports`): new high/critical findings reject the plan;
- checks the route invariants: every route of the baseline keeps its method, path, effective paths and
  authentication; added routes are listed;
- checks that no file outside the plan changed in the copy;
- runs the tests only with `--authorize-execution` and a trust profile the executor accepts; the operator's scope
  covers the project and is moved to the copy for the run (`untrusted` fails closed; the local executor is process
  hygiene, not a sandbox); the report is nonce-bound through the runner adapter.

| State | Meaning |
|---|---|
| `planned` | preview only (`--no-verify`) |
| `noop` | already applied |
| `blocked` | a precondition failed or a conflict exists; nothing was tried |
| `verified` | copy audited, invariants hold, protected files untouched, tests executed and passed |
| `audit_clean` | same without executed tests (no authorization): not `verified` |
| `inconclusive` | tests could not run (denied, refused command, unverifiable runner); never treated as success |
| `rejected` | new blocking findings, changed routes, files touched outside the plan, or failing tests |

`apply` writes the project only after `verified` or `audit_clean`; `--strict` demands `verified`. The report
(`evolution_report/1.0`) carries the discovery model, the plan, the verification and what was written.

## 6. Limits of this increment

- Only generated projects (with a manifest) can be evolved; hand-written projects are discovered and refused.
- Only new modules are added: entities, fields, operations, persistence, security and style of existing modules
  are not changed (a different definition of an existing module is refused).
- Integration points are regenerated, not merged: a manual change on one of them must be moved into the contract
  (or the file restored) before the evolution can proceed.
- Migrations: one additive revision per evolution; no column changes, no data migrations, no merges of heads.
- `verified` is as strong as the executed suite: the generated tests of the new module and the existing suite,
  through the local executor's process hygiene.

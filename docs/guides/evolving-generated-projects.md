# Evolving a project: discover, (adopt,) plan, approve, apply, recover

A walkthrough of `scripts/evolve_fastapi.py` on the committed examples and on the hand-written fixture. The contract
reference is `plugins/fastapi-architect/skills/fastapi-architect/references/evolution-contracts.md`.

## 1. Look before touching

```
python3 scripts/evolve_fastapi.py discover plugins/fastapi-architect/examples/generated/inventory-service
```

The model says what the engine knows and what it does not: package and layout, the modules it recognises, every
route with its effective path and authentication, the ownership of every file against the manifest, the Alembic
chain, and a list of uncertainties. On a hand-written project the ownership of every file is `unknown` and the
layout is `unknown`; `plan` will refuse it, `discover` still describes it.

## 2. Write the contract

```json
{
  "schema": "fastapi-architect/evolution/1.0",
  "project": {"path": "../generated/inventory-service", "package": "inventory_service"},
  "add_modules": [{"name": "suppliers", "entities": [{"name": "Supplier", "fields": [{"name": "name", "type": "str", "max_length": 120}]}]}]
}
```

`plugins/fastapi-architect/examples/evolutions/inventory-add-suppliers.json` and `orders-add-shipments.json`
are complete examples. Pin `project.expected_spec_digest` when the contract is reviewed asynchronously, so a
project evolved in the meantime is detected as a stale baseline.

## 3. Preview

```
python3 scripts/evolve_fastapi.py plan evolution.json
```

```
files: create 7, modify 4, unchanged 24, kept 0, preserved manual changes 0
  + src/inventory_service/api/suppliers.py
  ...
  ~ src/inventory_service/api/router.py
  ~ src/inventory_service/main.py
public contracts: +5 routes, -0, declared contracts changed: False
verification in copy: audit_clean; audit before 0 after 0, new blocking 0; routes before 6 after 11 missing 0; ...
applied: not_applied
```

`plan` writes nothing in the project. It applies the change set to a temporary copy, audits it, checks that every
existing route is intact and that nothing outside the plan moved, and reports. `--format json` carries the
unified diffs of the modified files. A `blocked` plan lists its conflicts; the usual ones are a hand-edited
integration point (`manual_change_on_integration_point`), a file already sitting where a new one would go
(`file_in_the_way`), a preserved file that would change, a stale baseline, a modified historical migration or two
Alembic heads.

## 4. Approve and apply

```
python3 scripts/evolve_fastapi.py apply evolution.json \
    --authorize-execution --trust fixture --test-command "python3 -m pytest -q"
```

`apply` repeats the verification in a copy, this time with the tests (only with `--authorize-execution`; the
local executor is process hygiene, not a sandbox, and `untrusted` fails closed), and writes the project only if
the verification did not reject the plan. Every write re-checks the digest recorded at plan time: a concurrent
change aborts before anything is written. The manifest records the operation (`history`), the new file digests
and the merged contract, so the next evolution starts from it. Running the same contract again is a `noop`.

Without `--authorize-execution` the best state is `audit_clean`; `--strict` refuses anything but `verified`.

## 5. What the engine protects

- A managed file you edited by hand is never regenerated. If the evolution does not need to change it, it is
  preserved and reported (`preserved manual changes`); if it is an integration point the evolution must
  regenerate, the plan is blocked until you move the change into the contract or restore the file.
- Files the manifest does not list are never written over.
- Historical migrations are kept byte-identical; a modified one blocks the plan; a chain with two heads blocks it.
- The layout never changes: a layered project that grows to three modules stays layered, with a warning that the
  advisor would now recommend a modular monolith.
- Routes of existing modules keep their paths and authentication (checked statically on the copy).

## 6. PostgreSQL projects

For `orders-platform` the plan adds `migrations/versions/<rev>_add_shipments.py` chained after the initial
revision (`create_table` with the composite tenant foreign key to `orders`; no drop, no alter), regenerates
`migrations/env.py` (one more model registered) and `tests/test_migrations.py` (new head, full table list), and
creates the module and its tests. Verified on a throwaway cluster by the opt-in test:

```
FA_DESIGN_PG=1 python3 -m unittest tests.test_design_v20_postgresql -v
```

The evidence of the run that accompanies this increment is in
`plugins/fastapi-architect/examples/generated/VERIFICATION.md`.

## 7. Changing an existing module (contract 1.1)

```json
{
  "schema": "fastapi-architect/evolution/1.1",
  "project": {"path": "../generated/inventory-service"},
  "modify_modules": [{"name": "catalog", "entities": [{"name": "Product",
      "add_fields": [{"name": "barcode", "type": "str", "max_length": 64, "required": false},
                     {"name": "active", "type": "bool", "required": true, "default": true}],
      "add_operations": ["update"]}]}]
}
```

The plan lists every change with its classification: `compatible` (nullable field, defaulted field, new
operation, new dependency), `potentially_incompatible` (a required field without default: existing callers would
get 422, and on PostgreSQL a NOT NULL column with no backfill) which blocks until a safe form is declared, and
`unsupported` (removals, renames, type changes) which the contract rejects. The module's own files are regenerated
from the merged contract; a manual change on any of them blocks (move it into the contract or restore the file).
On PostgreSQL the plan adds one additive revision after the recorded head: `add_column(..., nullable=True)` or
`add_column(..., server_default=..., nullable=False)`, `create_foreign_key` for nullable references; the risks
state that a downgrade drops the column with its data.

## 8. Adopting a hand-written project

```
python3 scripts/evolve_fastapi.py propose tests/fixtures/adoption20/handmade_shop
```

The proposal lists one candidate per router (`items`, `users`), their routes and related files, and the files
nobody claims. Nothing is adopted. Write the contract naming the modules and the exact files:

```json
{"schema": "fastapi-architect/adoption/1.0", "project": {"path": "/path/to/handmade_shop"},
 "modules": [{"name": "items", "router": {"file": "app/routers/items.py", "name": "router"}, "files": ["app/routers/items.py", "app/schemas.py"]}],
 "notes": "users stays unadopted on purpose"}
```

`adopt` writes `.fastapi-architect/adoption.json` and nothing else. From then on an evolution contract may add a
handler to the items router (`add_endpoints`: method, path, name, a literal `response`) or an optional field to a
pydantic class in an adopted file (`add_fields`): the handler is appended at the end of the module, the field
after the last one of the class, imports untouched, comments and spacing preserved byte-for-byte. The plan carries
the diff and the AST evidence; the copy is audited and the project's own tests run under the trust model;
`users.py` and every other unadopted file are never written. A manual change on an adopted file after adoption
blocks any transform until you adopt again; a duplicate route or symbol blocks; an ambiguous class blocks.

## 9. If something interrupts the write

Every write goes through `.fastapi-architect/ops/<operation id>/`: the new content and a backup of each file are
staged, a journal records the operation, then each file is renamed into place and the journal records it. A crash
after some files landed leaves the journal; `plan` refuses until it is handled:

```
python3 scripts/evolve_fastapi.py recover /path/to/project            # roll back: restore the backups
python3 scripts/evolve_fastapi.py recover /path/to/project --forward  # finish: publish the pending files
```

A file edited by hand after the crash is a conflict: recovery leaves it alone, restores or publishes the others,
and keeps the journal (`needs_attention`) until you decide. There is no atomicity across files; the renames are
atomic per file; the journal is what makes the whole operation recoverable.

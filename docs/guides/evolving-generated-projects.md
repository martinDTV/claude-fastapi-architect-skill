# Evolving a generated project: discover, plan, approve, apply

A walkthrough of `scripts/evolve_fastapi.py` on the committed examples. The contract reference is
`plugins/fastapi-architect/skills/fastapi-architect/references/evolution-contracts.md`.

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

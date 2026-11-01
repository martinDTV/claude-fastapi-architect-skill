# ADR 2.2-06 — Adopted projects II: a declared boundary, generated modules at a mount point, bounded extensions

Status: accepted (2026-11-01, decisions of the v2.2.0 review: adopted projects extend only inside a boundary the
operator approves) and implemented in increment 4 (2026-11-01). The discovery (section 2.5 of
`discovery-v2.2.md`) proposed `mount_points[]`; the implementation narrows that to **one** mount point per adoption,
because two places to register a router is already an ambiguity the tool should not resolve.

## Context

Since v2.0 a hand-written project is adopted module by module (adoption 1.0): the operator names the router files
and their companions, the tool records their digests, and later evolutions append a handler that returns a literal
or an optional field to those files through anchored transforms. Nothing else was possible: no new module (nothing
could register a router), no persistence, no delegation to the project's own code. Increment 4 widens that without
changing its nature: the project is never converted, no layout is imposed, no file outside the declared boundary is
written, and nothing is inferred as a permission.

## Decision

1. **The boundary is declared, verified and recorded (adoption 1.1).** The contract adds, all optional but checked
   as a whole: `package` (must equal the discovered one), `unmanaged` (globs the tool never writes, even for its own
   generated files), `mount` (`file`, `function` or `null` for module level, `app`, `after_call:
   "<app>.include_router"`, `prefix`), `new_modules` (`layout: package`, `router_variable: router | {module}_router`,
   `import_style: absolute`, `test_app {module, attribute, factory?}`), `dependencies.allowed` (`module:name`
   symbols), `operations` (`add_module`, `mount_router`, `add_endpoint`, `add_field`, `add_persistence`) and
   `alembic` (`ini`, `script_location`, `metadata: module:Base`, `session_dependency: module:function`, `async`).
   `mount` and `new_modules` come together; `alembic` and `add_persistence` come together. `adopt` verifies every
   item against the project: the mount scope resolves to exactly one function or the module, the application
   variable is bound there, at least one direct `include_router` call exists and none hides inside a loop,
   conditional, `with` or `try` (dynamic registration is refused), a file with several application factories needs
   the function named, the mount file is not an adopted module file nor a symlink, every allowed dependency and the
   test application resolve to exactly one top-level definition, and the Alembic facts of decision 5 hold. The
   record keeps the mount's digest, anchor, existing registrations and factories, the verified Alembic facts, and
   later the generated modules with their file digests and their definition. `propose` lists the mount candidates
   and the Alembic facts and prints a skeleton whose permissions are deliberately empty: nothing in it grants
   anything until the operator writes it.
2. **A generated module is self-contained and lands at the mount point only.** `add_modules` on an adopted project
   needs the mount, the conventions and the operations `add_module` and `mount_router`. The module is a package
   `<pkg>/<name>/` (`__init__`, `schemas`, `router`, `repository`, with persistence `model` and one revision) plus
   `tests/test_<name>.py`, rendered from the same specification machinery as generated projects (the pydantic
   schemas are the blueprint's), importing nothing the project did not declare (allowed dependencies become
   router-level `Depends`). The mount file receives exactly one import after its last import and exactly one
   `<app>.include_router(<name>_router, prefix=...)` after the last existing registration of the declared scope,
   through the span primitives of increment 0, whose additive proof (every pre-existing node unchanged, exactly the
   inserted nodes added) is the evidence in the plan. Refused by name: a module whose name is adopted, already
   generated with another definition, or whose package, sibling module file or files exist (`file_in_the_way`), a
   prefix another router already serves effectively (`mount_prefix_conflict`), an alias already bound in the mount
   file (`mount_symbol_free`), a path under `unmanaged`, dependencies outside the allowed list, a mount file changed
   since the adoption (`mount_file_unchanged_since_adoption`: adopt again) or turned into a symlink. The
   verification copy checks that every expected route of the new module resolves at its effective path (the audit's
   static routes), that existing routes are unchanged, and runs the project's own suite plus the module's tests
   under the trust model. Repeating the contract is `noop`; a second module chains after the first registration.
3. **Extensions of adopted routers stay bounded.** `add_endpoints` (evolution 1.6) may declare typed `parameters`
   (`path` or `query`, `str|int|float|bool`, `required`, `default`), `dependencies` (from the allowed list, resolved
   again at plan time), a `delegate` (`callable: module:function`, `arguments ⊆ parameter names`; `await`ed when the
   function is async; a class or a missing symbol is refused) and a `response_model` (a pydantic class defined once
   in the adopted module's files). Either a JSON `response` or a `delegate`, never both; no Python travels from the
   contract to the file. A new handler that an earlier parametrised route of the same method would capture first
   (`GET /stats` after `GET /{item_id}`) is refused (`route_not_shadowed`): FastAPI resolves routes in declaration
   order, the new handler is appended, and the existing code is not reordered to make it reachable.
4. **A generated module evolves like a generated module.** `modify_modules` on a module the tool generated inside an
   adopted project accepts `entities[].add_fields` (optional or defaulted) and `add_operations`; the files are
   regenerated from the recorded definition, so they must be exactly as the tool left them
   (`generated_module_files_unchanged`); a manual edit there is the developer's signal that the module is theirs now.
   `add_endpoints`/`add_fields` with a class are for adopted modules; `entities[]` is for generated ones; the two
   vocabularies do not mix.
5. **Persistence only on the project's own, verified Alembic environment.** `persistence: postgresql` needs
   `add_persistence` in the operations and `alembic` in the adoption; the plan re-verifies: `alembic.ini` and the
   script directory exist and agree with the discovery, the chain has exactly one head and no unknown parent,
   `env.py` assigns `target_metadata` from the declared `module:Base` (parsed, not imported), the base is a class
   and the session dependency a function, and the new table names collide with no `__tablename__` or
   `create_table` of the project. The model subclasses the declared base; the revision is appended after the current
   head (`create_table`, downgrade `drop_table`); a later column lands in a second revision (`add_column` nullable or
   with a server default, downgrade `drop_column`). Historical revisions are never edited, no second Alembic
   environment is created, no existing table is adopted. Two heads, an unrecognised environment, an ambiguous
   metadata or a duplicate table are refusals with the project intact; the in-memory variant stays available.
6. **Journal and report.** The journal records `generate` (the module files), `mount` (the file, the scope, the
   statements and both digests; the writer refuses a mount edit whose planned digest changed) and `transform` steps;
   an interruption rolls back to the digests before the operation. Report `evolution_report/1.7` adds
   `plan.mount`, `plan.generated_modules`, `verification.mount`; the discovery reports `generated_*` and
   `mount_point_*` ownership and the adoption's mount, generated modules and operations.

## Alternatives rejected

- Inferring the mount point from the discovery and writing there: the discovery already finds it, but writing to a
  file the operator did not name is a permission the tool must not grant itself.
- Several mount points, or dynamic registration (a loop over routers): resolving which one a module belongs to is
  a design decision of the project, not of the tool.
- Rendering the module in the project's own style (its routers' layout, its storage module): the hand-written code is
  the reference for what not to touch, not a template to imitate; a self-contained package is honest about who
  wrote what.
- Letting a contract carry Python for a handler body, or adopting existing tables for the generated models:
  unvalidated code and unverified ownership of schema are outside the trust model.
- Creating an Alembic environment when the project has none: a second environment on the same database is a
  hazard; the in-memory module is what the tool can honestly offer there.

## Consequences

- Contracts: `fastapi-architect/adoption/1.1` (1.0 still read and written as before), `fastapi-architect/evolution/1.6`
  (`add_modules[].persistence|prefix|dependencies`, `add_endpoints[].parameters|dependencies|delegate|response_model`),
  report `evolution_report/1.7`, journal step kinds `generate` and `mount`.
- Fixtures: `tests/fixtures/adoption22/workshop` (application factory, services, dependencies; no database) and
  `tests/fixtures/adoption22/notes_pg` (SQLAlchemy async, Alembic, one revision); `tests/test_adopted_v22.py` and
  the opt-in `tests/test_adopted_v22_postgresql.py` (scenario B on a throwaway cluster).
- The shadow rule is an explicit compatibility change: the v2.0 scenario B (`GET /stats` appended after
  `GET /{item_id}`) was reported `verified` with an unreachable handler; it is now refused, and the v2.0 examples moved
  to `/reports/stats`. The historical blocker name for `add_modules` on a 1.0 adoption is kept.
- Limits, stated: one mount point per adoption; packages only, absolute imports only; generated modules are the
  tool's (a manual edit there blocks regeneration); extensions delegate to one callable and never rewrite an
  existing handler; persistence needs the project's own Alembic environment, PostgreSQL and a linear chain; a
  handler shadowed by an earlier route is refused rather than reordered; the project's own tests remain the
  acceptance of every evolution and run only under the trust model.

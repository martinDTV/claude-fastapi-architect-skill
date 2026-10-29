---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains that the session's type is only visible where it is created (the dependency's yield / the
   factory) and must be followed through calls: the route's parameter comes from get_db, the service and
   repository parameters inherit it from their callers; it mentions that the skill's auditor does this
   propagation (dependency and caller kinds, evidence marked as inferred) or describes an equivalent
   conservative tracing, and says what such tracing cannot see (dynamic dispatch, attributes, untyped
   injection).
2. It identifies the concrete defect class: a coroutine method of AsyncSession called without await never
   executes (commit silently skipped), and proposes verification (run the path and check the row, or a test).
3. It proposes the structural fix: annotate the dependency and parameters (SessionDep = Annotated[AsyncSession,
   Depends(get_session)], service/repository signatures typed), so type checkers and the auditor see the kind,
   plus a regression test; it does not propose a rewrite of the layering.
FAIL if it claims the misuse can be found without following the calls, if it misses the un-awaited commit, or
if it recommends replacing the architecture instead of typing the boundary.

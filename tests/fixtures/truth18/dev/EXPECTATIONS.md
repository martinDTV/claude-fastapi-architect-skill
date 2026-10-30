# truth18 / dev — expectations written before the v1.8 engine work (2026-10-30)

Marker legend as in truth16/truth17, plus `key=value` attributes on `truth:` markers that must match the
finding: `evidence=` (evidence class) and `loop_kind=` (`details.loop_kind` of a loop-commit finding).

| Area | Case | Expected |
|---|---|---|
| P1 classification | locals bound to awaited/plain results of external or stdlib calls, and to project functions whose return annotation is a builtin type | methods on them classified `external`/`builtin`, never `dynamic`, never a project edge |
| P1 classification | unpacked builtin results | `builtin` |
| P1 classification | closure capturing a typed session from the enclosing function | the session rule fires with `verified` evidence |
| P1 classification | loop targets and comprehension variables over lists of unknown element type | stay `dynamic` (honest) |
| P1 classification | project function with an annotated project return type | edge to the returned class's method; also through tuple unpacking |
| P2 callbacks | ASGI `receive`/`send`/`app` typed with starlette types, and the result of `await receive()` | `external` (framework-supplied), not dynamic |
| P2 callbacks | typed `Callable` parameter with one provider across callers, including a lambda that forwards to that provider, and a bound-method reference | project edge, `inferred`; the indirect-blocking rule follows it |
| P2 callbacks | typed `Callable` parameter with two different providers, or with no project caller | `ambiguous` / `dynamic`; no finding |
| P3 attributes | `self._uow.repo.get()` with both links typed | project edge |
| P3 mixins | mixin method using `self._session` where exactly one project class mixes it in with a typed session | session rule with `inferred` evidence |
| P3 mixins | two classes mix it in with conflicting kinds | no finding |
| P3 hooks | `self.step()` implemented by two subclasses | `ambiguous`; by exactly one subclass | project edge |
| P3 external bases | `self.model_dump_json()` on a pydantic model | `external` |
| P4 loops | commit per element, with or without a BATCH-named constant nearby | `TX_COMMIT_IN_LOOP` with `loop_kind=per_item` |
| P4 loops | commit under `index % N == 0` or `len(buffer) >= N` | classified `batch`: counted in `facts.commit_loops`, no finding |
| P4 loops | `while` loop fetching pages with a cursor that advances, commit per page | classified `paginated`: counted, no finding |
| P4 loops | `for _ in range(N)` that breaks when stable, commit per pass | classified `convergence`: counted, no finding |
| P4 loops | retry loop committing once on success | no finding |
| P4 loops | `commit()` inside `async with session.begin()` | `TX_COMMIT_INSIDE_BEGIN_BLOCK`; `flush()` there is fine |

## Change log
- 2026-10-30, before the first scoring of the v1.8 engine on this corpus and before any held-out scoring: the three
  deliberate loop kinds (`batch`, `paginated`, `convergence`) were first written as "reported with lowered
  confidence". They are now expected **absent**. Reason: the rule's own text treats deliberate batching as the
  acceptable outcome, the modulo form was already silenced in v1.6 after calibration review, and re-reporting it
  would surface new findings in every existing baseline (`--fail-on-new`). The v1.7 engine was inconsistent
  (modulo silent, `len(buffer) >= N` reported); v1.8 makes every structurally deliberate kind silent, keeps the
  classification visible in `facts.commit_loops`, and unit-tests the classifier directly
  (`tests/test_audit_v18.py`). The `loop_kind=` attribute on the absent markers is documentation only
  (the scorer ignores attributes on `truth-absent`).

# truth18 / holdout — written 2026-10-30, before the v1.8 review changes

Written after the v1.8.0 commits (`646faa1`) and **before** the review changes that followed (opt-in loop policy,
AgentForge reconciliation, SARIF properties). Same author as the engine, so it is not a blind test; what it
guarantees is ordering: every marker below was fixed before the engine ran on this corpus, the corpus is scored
once at the end of the review with the final engine and with the v1.7 engine (`--against e8224ce`), and its
results are not used to change any rule of this delivery. Open items, if any, are analysed in the release notes.

Domain: a warehouse (picking, gateway, layers, jobs). Markers as in `dev/EXPECTATIONS.md`; `follows=` on the
begin-block rule checks `details.follows`.

| Area | Case | Expected |
|---|---|---|
| P1 | awaited client result, `resp.json()`, `payload.get()` | `external` |
| P1 | builtin unpack from `str.split`, methods on the parts | `builtin` |
| P1 | comprehension over a dict parameter; loop target over an untyped list | `dynamic` (honest) |
| P1 | async closure inside a method using `self._session` | session rule, `verified` |
| P1 | project return annotation (`-> Location`) through an attribute session | edge to `Location.describe`; indirect blocking in the async caller |
| P1 | project function returning `str` | `builtin` |
| P2 | ASGI middleware with a `receive_wrapper` closure awaiting the outer `receive`; `self.app(...)` | `external` |
| P2 | typed `Callable` with one bound-method provider | edge to `Scanner.read`; indirect blocking in `scan` |
| P2 | typed `Callable` with two providers | `ambiguous`; no finding |
| P3 | `self.ctx.finder.find()` with both links typed | edge |
| P3 | mixin with one typed subclass | session rule, `inferred` |
| P3 | mixin with one typed and one untyped subclass | no finding |
| P3 | template method with one implementation | edge; with two | `ambiguous` |
| P3 | `TypedDict.get`, `Exception.add_note` | `external` |
| P4 | per-item commit | `TX_COMMIT_IN_LOOP`, `loop_kind=per_item` |
| P4 | counter-threshold batch, for-range pagination, while-True convergence, retry | classified, counted, not reported |
| P4 | per-item inner loop inside a paginated outer loop | the inner commit is reported (`per_item`) |
| P4 | paginated loop with an HTTP call inside `begin_nested()` | `TX_EXTERNAL_CALL_IN_TRANSACTION` still reported; the page commit is not |
| P4 | commit inside `begin()` followed by a statement / as the last statement / `flush()` | rule with `follows=True` / `follows=False` / nothing |

## Change log
- (none)

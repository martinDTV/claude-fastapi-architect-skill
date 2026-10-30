# truth18 / acceptance — held out (written 2026-10-30, before the v1.8 engine work)

Same rules as `dev/EXPECTATIONS.md`, different shapes: awaited external results through an attribute client,
iteration over `dict.items()` (unknown element type), a synchronous closure using an attribute session, project
functions returning project and builtin types, an ASGI middleware with a `send_wrapper` closure, a typed
callable with two providers and one with a bound-method reference, a two-level attribute chain through a
context object, a mixin audited by exactly one subclass, a pydantic base, a template method with one concrete
implementation, and the five loop-commit kinds plus a commit inside `begin()`.

Scored once with the final v1.8 engine and the v1.7 engine. Written by the same author as the engine, so it is
an acceptance corpus, not a blind holdout; it was displayed once during the v1.8 implementation (to confirm the
loop shapes it contains) and edited once, before any scoring, as recorded below.

## Change log
- 2026-10-30, before the first scoring: the `batch`, `paginated` and `convergence` loop-commit markers in
  `crm/jobs.py` were flipped from "reported with `loop_kind`" to **absent**, following the design decision
  recorded in `dev/EXPECTATIONS.md` (structurally deliberate commit loops are classified and counted in
  `facts.commit_loops`, not reported). No other marker was touched.

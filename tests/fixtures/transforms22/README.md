# Adversarial corpus for span edits and merges (v2.2, increment 0)

`app_factory.py` is a hand-written application factory in the shapes the consumer projects use (decorators with
keyword arguments, `Annotated` dependencies, a `TYPE_CHECKING` block, a call with a comment inside it, a multi-line
call with a trailing comma, keyword-only and `**kwargs` signatures, a nested class, an async handler). It is parsed,
never imported; `tests/test_spans_v22.py` edits it with every primitive and asserts, for every case, that the bytes
outside the edited span are identical, that the comment tokens are unchanged, that a second application is a no-op
and that the refusals name their reason. CRLF and tab variants are derived from it by the tests (git would otherwise
normalise line endings).

The merge cases of `tests/test_merge_v22.py` are built inline (base, ours, theirs triples): disjoint hunks, adjacent
insertions at one point, overlapping edits, a deletion on one side, the same change on both sides, both sides adding
one import, a router mounted by both sides at different places, a hand edit inside a function the generator
rewrites, a protected region, CRLF, and `.audit-fastapi.json` documents merged by key. Nothing here is a real
project of anyone.

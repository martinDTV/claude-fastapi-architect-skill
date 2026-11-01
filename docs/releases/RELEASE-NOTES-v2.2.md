# FastAPI Architect v2.2.0 — Safe Evolution II (in progress)

Status: in progress on top of the published v2.1.0 (`7aff2b1`, release at `219383d`). The scope was decided on
2026-10-31 after the discovery recorded in [`docs/architecture/discovery-v2.2.md`](../architecture/discovery-v2.2.md)
(section 11): seven increments, each authorised separately. This document grows one section per increment. The
version constant, the plugin manifests and the generated manifests stay at 2.1.0 until the release increment bumps
them.

## Increment 0: Foundations for Safe Evolution II

Nothing a user runs changes its outcome in this increment: the five committed snapshots regenerate byte-identically,
the per-file oracle against the v2.1.0 engine shows no difference on any specification, the evolution and
remediation behaviour is unchanged. What the increment adds is the ground the next increments stand on, each piece
pure, tested on an adversarial corpus and not yet wired into a user-facing operation except where noted.

### Span-exact edits (`fastapi_design/spans.py`)

Three primitives that edit an existing Python file without ever re-rendering it, next to the append-only ones of
`transforms.py`:

| Primitive | What it does | Proof it carries |
|---|---|---|
| `insert_statement` | one statement inside a function body (module level too), after the last call to a named callee or at the end of the body, at the anchor's indentation | the body grew by exactly one statement and every other node is unchanged (`ast.dump`), every byte outside the inserted lines is identical, the comment tokens are the same |
| `insert_keyword_argument` | `name=value` on one existing statement-level call, located by callee, enclosing function, index or first argument; follows the style it finds (inline, or one argument per line with a trailing comma) | the call gained exactly that keyword and nothing else changed; bytes outside the call's span identical; comments identical |
| `insert_parameter` | one parameter on one signature, where Python's ordering allows it (end of the positional list, end of the keyword-only section, before `**kwargs`) | the signature gained exactly that parameter, body and decorators unchanged, the rest of the module unchanged; bytes outside the signature identical |

Refusals, named: an ambiguous or missing target, a call or signature whose span contains a comment (refused rather
than re-rendered), a keyword already present with another value (never overwritten), `**kwargs` unpacking in the
call, a parameter without a default after parameters with defaults, a duplicate parameter name, a protected region.
No-ops with evidence: the same keyword with the same value, an identical parameter, an identical statement already
in the body. CRLF files stay CRLF; tab-indented bodies get tabs. `ProtectedRegion` is the explicit representation of
lines an operator froze; `detect_formatter` and `format_check_command` prepare the formatter-stability check the
verification copy will run in a later increment (nothing is executed here).

### The pure three-way merge (`fastapi_design/merge.py`)

`merge3(base, ours, theirs)` on lines (`difflib`): disjoint hunks both applied, two insertions at one point a
conflict unless identical, overlapping edits a conflict, an insertion at the edge of a replaced block ordered rather
than conflicting, identical changes applied once, CRLF kept; **no text is produced when any conflict or protected
violation exists**. Protected regions refuse hunks of the *generated* side only. `merge_python` adds the structural
proof over the top-level statements, matched by identity (a definition by its name, an assignment by its targets,
anything else by its text): added statements present, a statement changed by one side holds that side's version, one
changed by both is at least not the base version (reported under `both_modified`), untouched ones unchanged; a merge
that fails it is a conflict even when textually clean, and a statement both sides added that appears twice is
reported as `duplicate_additions`. `merge_json` merges `.audit-fastapi.json`-like documents by key (added on one
side: taken; changed on one side: taken; both to the same value: once; both differently, or removed versus changed: a
conflict named by its JSON path). Not wired into any operation yet: the planner of increment 3 will call it under
`files.allow_merge`.

### The project lock and journal 1.1 (`fastapi_design/locking.py`, `journal.py`)

One writer at a time per project: `.fastapi-architect/ops/LOCK`, created with `O_EXCL`, holds pid, host, start time,
operation kind and description and a token. **Fail fast by default**: `apply`, `adopt` and `recover` raise
`LockHeld` naming the holder and the reason (`held`, `stale`); `apply` reports `applied.state: locked`, `recover`
exits 3. Bounded wait on request (`recover --wait SECONDS`; `wait_s` in the API). A stale lock (holder pid gone on
this host, or older than six hours from another host) is reported and **never broken silently**: `evolve_fastapi.py
ops unlock` removes it after naming it and refuses a live one. The journal is `fastapi-architect/operation/1.1`,
additive: `steps` (what produced each file: `regenerate`, `transform`, `manifest`; `merge` later, with evidence
digests) and `parent_op`; the file protocol and the operation identity are unchanged, so 1.0 journals are still read
and recovered. `evolve_fastapi.py ops list | show | unlock` print pending operations, their steps, the files'
current classification and the lock.

### Adversarial corpus and compatibility fixtures

`tests/fixtures/transforms22/app_factory.py` (decorators with keyword arguments, `Annotated` dependencies, a
`TYPE_CHECKING` block, a call with a comment inside it, multi-line calls and signatures with trailing commas,
keyword-only and `**kwargs` signatures, a nested class; CRLF and tab variants derived in the tests) and inline merge
cases (`tests/test_merge_v22.py`). `tests/fixtures/compat/` gains what v2.1.0 wrote: the `parcels` project with its
manifest 1.3, the two phased evolution contracts 1.2, a migration evidence 1.0 and an evolution report 1.3; the
compatibility tests prove each is read and still means the same.

### Defects the corpus found before the increment closed

- `acquire` created the lock directory once before its retry loop; a holder releasing at that moment removes the
  empty directory, so a waiting acquirer hit `FileNotFoundError` instead of taking the lock. The directory is now
  recreated on every attempt and the vanished-directory case retries.
- The first structural proof of `merge_python` treated a function modified by both sides as two "added" nodes and
  declared the clean merge lost; the proof now matches top-level statements by identity.
- `merge_json` mistook a key added by one side for a key removed by one side and changed by the other.

### Deliberate differences from v2.1.0

- Journals are written as `operation/1.1` (1.0 readers of this tool keep working on 1.0 journals; a v2.1 reader
  would report a 1.1 journal as needing attention, which is the safe direction).
- `apply` may end in `applied.state: locked`; `recover` and `adopt` exit 3 on a held lock; `ops` is a new subcommand.
- Nothing else: contracts, manifests, rule ids, fingerprints, suppressions, formats, the five snapshots and the
  generated code are unchanged.

### Validation

Environment: macOS (Darwin 27.0.0), CPython 3.13.5 and 3.9.6, PostgreSQL 16.10 as throwaway clusters, Ruff 0.17.3.

| Check | Result |
|---|---|
| New modules on the adversarial corpus | `tests/test_spans_v22.py` 16, `tests/test_merge_v22.py` 19, `tests/test_journal_v22.py` 12 (lock lifecycle, fail-fast, bounded wait, stale and unreadable locks, another host's age rule, a write refused under a held lock, two processes racing, two threads serialising on disjoint files, steps surviving an interruption and a forward recovery, a 1.0 journal, identity independent of steps, the `ops` subcommands); all OK on 3.13 and 3.9 |
| Compatibility of the v2.0.0 and v2.1.0 artefacts | `tests/test_compat_artifacts.py` 13 OK: manifests 1.0, 1.1 and 1.3, evolution 1.0, 1.1 and 1.2, journal 1.0, adoption 1.0, audit report 1.7 fingerprints, migration evidence 1.0, evolution report 1.3 (same revisions and operation ids reproduced) |
| Generation equivalence | per-file oracle against the v2.1.0 engine (`7aff2b1`) on every specification of `examples/specs` and `tests/fixtures/design20/specs` and on every example evolution plan: 259 files, 0 differing; the five committed snapshots regenerate with 0 files written |
| Existing behaviour of evolution, adoption, recovery and remediation | `test_evolve_v20.py` 26, `test_evolve_v20_adoption.py` 22, `test_evolve_v20_recovery.py` 18, the remediation suites inside the full run: OK with the lock in place |
| Full suite, Python 3.13.5 | 680 tests OK, 26 skipped (the 51 new tests included); 137.4 s, 140 MB |
| Full suite, Python 3.9.6 | 680 tests OK, 41 skipped; 142.9 s, 184 MB |
| Real PostgreSQL (the modules whose `apply` writes through the journal) | `test_migrations_v21_postgresql.py` 4 OK (60.0 s), `test_acceptance_v21.py` 4 OK (67.6 s), `test_acceptance_v20.py` with `FA_DESIGN_PG=1` 3 OK (51.0 s) |
| Auditor | no file under `fastapi_audit` changed: 59 rule ids, fingerprints, suppressions, SARIF and precision identical by construction |
| `scripts/validate_structure.py` (3.9 and 3.13), `claude plugin validate` (marketplace and plugin), Ruff, `tests/test_docs_links.py`, `git diff --check`, secrets scan on the diff and every new file | pass; no cache or temporary file added |
| Not run | the real-Docker suite (the executor is untouched by this increment; its fake-runtime tests ran inside the full suite) |


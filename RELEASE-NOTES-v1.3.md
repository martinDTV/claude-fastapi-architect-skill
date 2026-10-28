# FastAPI Architect v1.3 — Architectural Auditor

Additive release on top of v1.2. No tracked file from v1.0, v1.1 or v1.2 was removed or renamed; the plugin keeps the installation identity `fastapi-architect` and the `/fastapi-architect` invocation.

## Added
- `plugins/fastapi-architect/skills/fastapi-architect/scripts/audit_fastapi.py`: read-only static auditor for FastAPI projects, standard library only (`ast`). Ships inside the skill so the plugin carries it. Rules: `ASYNC_BLOCKING_CALL`, `ASYNCIO_RUN_NESTED`, `ROUTE_NO_RETURN_ANNOTATION`, `PY_SYNTAX`, `FILE_SKIPPED`. Deterministic JSON report (`schema_version` 1.0) with `project_root`, `files_scanned`, `files_skipped`, `summary`, `findings` and a `disclaimer`. Text summary by default, `--json`, `--output FILE`, `--exclude`, `--include-hidden`, `--max-file-bytes`, `--fail-on`, `--list-rules`.
- `scripts/audit_fastapi.py`: repository-root entry point that forwards to the skill copy, so `python3 scripts/audit_fastapi.py <project>` works from a clone without duplicating logic.
- `tests/test_audit_fastapi.py`: 39 `unittest` tests (also runnable with pytest) covering rule detection, import aliases, shadowing, nested scopes, `response_model` suppression, non-FastAPI decorators, syntax errors, unreadable and oversized files, excluded and hidden directories, symlink deduplication and escape, determinism, read-only behaviour, JSON schema and the CLI.
- `references/architecture-audit.md`: audit workflow for existing repositories, layer and dependency mapping, antipattern catalogue with required evidence, current-versus-recommended comparison, finding classification with four evidence states (observed and reproduced; suspected, backed by evidence; not verifiable with the available information; fixed and verified), reproduction recipes, incremental refactor, regression tests, report template, acceptance criteria, authorization and multi-tenancy audit checklist, and limits of static tools.
- `references/reference-implementation-strategy.md`: how executable reference implementations must be built, labelled (`verified`, `draft`, `illustrative`) and verified, with a catalogue of twelve planned examples and a mandatory checklist. It states explicitly that no validated end-to-end example ships in this version.
- `evals/scenarios-v1.3.json`: six scorer scenarios for audits and reference-implementation requests. Not recorded runs.

## Updated
- `SKILL.md`: the review section now starts with the auditor as triage, and a v1.3 routing table loads the two new references only when the task is an audit, diagnosis, refactor, or a request for runnable examples. Progressive disclosure preserved.
- Version synchronized at `1.3.0` in `.claude-plugin/marketplace.json` (`metadata.version` and `plugins[0].version`) and `plugins/fastapi-architect/.claude-plugin/plugin.json`.
- `scripts/validate_structure.py` additionally checks the auditor files, the SKILL link to the auditor, the test file, and the release notes for the manifest version.
- README documents v1.3.

## Preserved
- All 21 previous references, 5 assets, `evals/evals.json`, `evals/scenarios-v2.json`, `evals/scenarios-v1.2.json`, `MERGE-NOTES.md`, `RELEASE-NOTES-v1.2.md`, LICENSE and the plugin structure.

## Versioning criterion
The target was `1.3.0` because the repository was at `1.2.0`. If a checkout already carries a higher version, keep the higher one and apply these changes as an additive patch; never lower a published version.

## Auditor design notes
- Scope-aware resolution: imports and bound names are tracked per function, class and lambda; the innermost function decides whether a call runs in an async context, so a sync helper nested in an `async def` is not flagged and an `async def` nested in a sync factory is.
- Import aliases are resolved (`import time as t`, `from time import sleep as pause`, `from urllib import request`). Names rebound by assignment, parameters, local `def`, relative imports or ambiguous imports are never matched.
- Callables passed as references (`anyio.to_thread.run_sync(time.sleep, 1)`) are not flagged.
- `response_model` or `response_class` on the decorator suppresses `ROUTE_NO_RETURN_ANNOTATION`. Route detection requires a `fastapi` import in the module or a receiver named like a router, which keeps Flask and aiohttp decorators out.
- Symlinked directories are never followed; symlinked files are analysed only when they resolve inside the project root, and a real file reachable through several paths is analysed once.
- Dot-directories and the usual build, cache and dependency folders are skipped by default.
- Unreadable, oversized or unparsable files are recorded and the scan continues.
- The auditor never imports or executes inspected code; its only write is the optional `--output` file.

## Corrections made during final validation
Three gaps in the CLI contract were found while validating against real repositories and fixed before publication; no rule logic changed.
- `--output` pointing to an unwritable location returned a raw traceback with exit 1. It now prints `cannot write report to ...` on stderr and exits 2. Nothing is created.
- A directory that could not be listed was dropped silently by the directory walk. It is now recorded in `files_skipped` with reason `unreadable_directory` and as a `FILE_SKIPPED` finding.
- The report now carries `tool.python` (interpreter whose grammar parsed the files) and `summary.partial_analysis` (`true` when any file or directory was skipped or failed to parse). `PY_SYNTAX` messages name the Python version, because a project written for 3.12+ audited with an older interpreter produces syntax findings that disappear with the right interpreter.
Tests: 42.

## Known limitations (open)
- No inter-procedural analysis; blocking helpers called from `async def` are invisible.
- Calls through instances or attributes (`self.http.get`, sync ORM sessions, SDK clients) are not resolved.
- Built-in `open()` and file I/O are intentionally not reported.
- Routes registered via `add_api_route` or custom decorators are not detected.
- Parsing uses the running interpreter's grammar.
- No Claude model evaluation run is recorded for the new scenarios.

## Validation performed on this release
See the final report of the integration session and `scripts/validate_structure.py`. Validation commands: `python3 scripts/validate_structure.py`, `python3 -m unittest discover -s tests -v`, `python3 scripts/audit_fastapi.py --list-rules`, `claude plugin validate .` when the CLI is available.

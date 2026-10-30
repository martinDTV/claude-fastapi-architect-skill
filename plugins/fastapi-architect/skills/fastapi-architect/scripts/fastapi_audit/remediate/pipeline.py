"""The remediation pipeline (v1.9, hardened): one isolated copy per finding, evidence per case.

For every selected finding, in its own copy of the project:

1. the copy is audited: the finding must be reproduced there (same fingerprint) and the file must match the
   audited baseline (``base_sha256``), otherwise the case is ``rejected`` (``not_reproduced`` / ``stale_baseline``);
2. the candidate patch is generated from the copy's source and checked against the case (same file, same base);
   a failed precondition leaves the case ``blocked``;
3. when an executor was granted (authorization, scope and trust profile all satisfied), the case's **linked
   reproducer** runs before the patch: it must fail (``defect_reproduced``), otherwise the case is
   ``inconclusive`` (``defect_not_reproduced``); a case without a linked reproducer is ``inconclusive``
   (``no_linked_tests``) when execution was granted;
4. the patch is applied; test files must be untouched (``tests_modified`` rejects);
5. the copy is audited again: the finding must be gone, not merely suppressed (``finding_persists``), and no
   new finding may appear anywhere (``regression_introduced``);
6. the linked reproducer runs after the patch: ``passed`` verifies, ``failed`` rejects, anything else
   (skipped, none collected, timeout, runner error) is ``inconclusive``;
7. without an executor the case ends ``audit_clean`` with the reason the tests were not executed.

Then a combined copy applies every candidate that reached ``verified`` or ``audit_clean``, audits it and, when a
suite command was given and an executor granted, runs the suite before and after: the run reports whether the
candidates compose (``combined``); individual verdicts keep their own evidence. The project itself is digested
before and after the run; a difference aborts with ``RemediationError``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..discovery import classify_environment
from ..engine import AuditOptions, audit_project
from ..model import ENV_TEST, TOOL_NAME, TOOL_VERSION
from ..report import compare_reports
from .executors import Authorization, Executor, LocalProcessExecutor, select_executor
from .isolation import ExecutionPolicy, Workspace, digest_tree
from .model import (
    OUTCOME_FAILED,
    OUTCOME_INFRASTRUCTURE_ERROR,
    OUTCOME_NO_TESTS,
    OUTCOME_PASSED,
    OUTCOME_REFUSED,
    OUTCOME_SKIPPED_ONLY,
    OUTCOME_TIMEOUT,
    RC_CANDIDATE_MISMATCH,
    RC_COMMAND_REFUSED,
    RC_DEFECT_NOT_REPRODUCED,
    RC_FINDING_PERSISTS,
    RC_INFRASTRUCTURE_ERROR,
    RC_NO_LINKED_TESTS,
    RC_NO_TESTS,
    RC_NOT_REPRODUCED,
    RC_PATCH_FAILED,
    RC_PRECONDITION_FAILED,
    RC_REGRESSION_INTRODUCED,
    RC_STALE_BASELINE,
    RC_TESTS_FAILED_AFTER,
    RC_TESTS_MODIFIED,
    RC_TESTS_SKIPPED,
    RC_TIMEOUT,
    STATE_AUDIT_CLEAN,
    STATE_BLOCKED,
    STATE_INCONCLUSIVE,
    STATE_PATCHED,
    STATE_REJECTED,
    STATE_VERIFIED,
    AuditCheck,
    Invariant,
    RemediationCase,
    RemediationError,
    TestRun,
)
from .patches import RECIPE_META, Blocked, apply_edits, propose, recipe_for

RUN_SCHEMA = "remediation_run/1.1"
OUTCOME_TO_INCONCLUSIVE = {
    OUTCOME_SKIPPED_ONLY: (RC_TESTS_SKIPPED, "the linked tests were skipped"),
    OUTCOME_NO_TESTS: (RC_NO_TESTS, "no linked test was collected"),
    OUTCOME_INFRASTRUCTURE_ERROR: (RC_INFRASTRUCTURE_ERROR, "the test runner failed before reaching a verdict (interrupted, internal or usage error)"),
    OUTCOME_TIMEOUT: (RC_TIMEOUT, "the linked tests timed out"),
    OUTCOME_REFUSED: (RC_COMMAND_REFUSED, "the linked command was refused by the executor"),
}


@dataclass
class RunOptions:
    select_rules: Optional[Set[str]] = None
    fingerprints: Optional[Set[str]] = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    authorization: Authorization = field(default_factory=Authorization)
    executor: str = LocalProcessExecutor.name
    links: Dict[str, str] = field(default_factory=dict)  # fingerprint | file:line | RULE -> reproducer command
    workspace_dir: Optional[str] = None
    keep_workspace: bool = False
    patch_dir: Optional[str] = None
    max_cases: int = 50
    config_path: Optional[str] = None
    include_tests: bool = False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _brief(f: Dict[str, object]) -> Dict[str, object]:
    return {"fingerprint": f.get("fingerprint"), "rule": f.get("rule"), "file": f.get("file"), "line": f.get("line"), "severity": f.get("severity")}


def _select(report: Dict[str, object], options: RunOptions) -> List[Dict[str, object]]:
    chosen: List[Dict[str, object]] = []
    for finding in report["findings"]:  # type: ignore[index]
        if not isinstance(finding, dict) or finding.get("suppressed"):
            continue
        if options.select_rules is not None and finding.get("rule") not in options.select_rules:
            continue
        if options.fingerprints is not None and finding.get("fingerprint") not in options.fingerprints:
            continue
        if finding.get("environment") == ENV_TEST and not options.include_tests:
            continue
        if recipe_for(finding) is None:
            continue
        chosen.append(finding)
    chosen.sort(key=lambda f: (str(f["file"]), -int(f["line"]), str(f["rule"])))
    return chosen[: options.max_cases]


def _link_for(case: RemediationCase, links: Dict[str, str]) -> Optional[str]:
    for key in (case.fingerprint, f"{case.file}:{case.line}", case.rule):
        if key in links:
            return links[key]
    return None


def _presence(report: Dict[str, object], fingerprint: str) -> AuditCheck:
    active = suppressed = False
    for f in report["findings"]:  # type: ignore[index]
        if isinstance(f, dict) and f.get("fingerprint") == fingerprint:
            if f.get("suppressed"):
                suppressed = True
            else:
                active = True
    return AuditCheck(target_present=active, target_suppressed=suppressed, findings_total=int(report["summary"]["findings_total"]))  # type: ignore[index]


def _test_files(digest: Dict[str, str]) -> Dict[str, str]:
    return {path: sha for path, sha in digest.items() if classify_environment(path) == ENV_TEST}


def _invariants(case: RemediationCase) -> List[Invariant]:
    meta = RECIPE_META.get(case.recipe or "", {})
    out: List[Invariant] = []
    for statement, checked_by in meta.get("invariants", []):  # type: ignore[union-attr]
        satisfied: Optional[bool]
        if checked_by == "audit":
            satisfied = (case.after_audit.target_present is False and not case.after_audit.target_suppressed and not case.after_audit.new_findings) if case.after_audit.target_present is not None else None
        elif checked_by == "parse":
            satisfied = case.patch is not None
        elif checked_by == "linked_tests":
            satisfied = True if (case.linked_after.ran and case.linked_after.outcome == OUTCOME_PASSED and case.defect_reproduced) else (False if case.linked_after.outcome == OUTCOME_FAILED else None)
        else:
            satisfied = None
        out.append(Invariant(statement=str(statement), checked_by=str(checked_by) if satisfied is not None else "not_checked", satisfied=satisfied))
    return out


def _run_case(case: RemediationCase, finding: Dict[str, object], root: str, options: RunOptions, audit_options: AuditOptions, executor: Optional[Executor], denial: Optional[str], denial_code: str) -> None:
    workspace = Workspace.create(root, None if not options.workspace_dir else str(Path(options.workspace_dir) / case.fingerprint))
    try:
        if options.keep_workspace:
            case.workspace_path = str(workspace.root)
        case.executor = executor.name if executor else None
        case.isolation_level = executor.isolation_level if executor else None
        if executor is not None:
            case.limitations.extend(f"executor '{executor.name}' ({executor.isolation_level}) does not provide: {item}" for item in executor.not_provided)
        baseline = audit_project(str(workspace.project), options=audit_options)
        case.before_audit = _presence(baseline, case.fingerprint)
        if not (case.before_audit.target_present or case.before_audit.target_suppressed):
            case.transition(STATE_REJECTED, "the finding was not reproduced by the audit of the isolated copy", RC_NOT_REPRODUCED)
            return
        path = workspace.project / case.file
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            case.transition(STATE_REJECTED, f"cannot read the file in the copy: {exc}", RC_STALE_BASELINE)
            return
        if _sha(source) != case.base_sha256:
            case.transition(STATE_REJECTED, "the file in the copy differs from the audited baseline", RC_STALE_BASELINE)
            return
        result = propose(finding, source)
        if isinstance(result, Blocked):
            case.transition(STATE_BLOCKED, result.reason, RC_PRECONDITION_FAILED)
            return
        if result.file != case.file or result.original_sha256 != case.base_sha256:
            case.transition(STATE_REJECTED, "the candidate targets another file or another baseline than the case", RC_CANDIDATE_MISMATCH)
            return
        digest_copy = digest_tree(str(workspace.project))
        tests_before = _test_files(digest_copy)
        case.reproducer = _link_for(case, options.links)
        if executor is not None and case.reproducer:
            case.linked_before = executor.run(case.reproducer, workspace, options.policy)
        try:
            path.write_text(apply_edits(source, result.edits), encoding="utf-8")
        except (OSError, ValueError) as exc:
            case.transition(STATE_REJECTED, f"cannot apply the candidate in the copy: {exc}", RC_PATCH_FAILED)
            return
        case.patch = result
        case.transition(STATE_PATCHED, "candidate applied in the case's isolated copy")
        tests_after = _test_files(digest_tree(str(workspace.project)))
        changed_tests = sorted(p for p in set(tests_before) | set(tests_after) if tests_before.get(p) != tests_after.get(p) and p != case.file)
        case.tests_untouched = not changed_tests
        if changed_tests:
            case.transition(STATE_REJECTED, "the candidate modified test files: " + ", ".join(changed_tests), RC_TESTS_MODIFIED)
            return
        if classify_environment(case.file) == ENV_TEST:
            case.limitations.append("the patched file is test code; its own tests cannot verify it")
        after = audit_project(str(workspace.project), options=audit_options)
        case.after_audit = _presence(after, case.fingerprint)
        comparison = compare_reports(after, baseline, "isolated-copy-before-patch")
        case.after_audit.new_findings = list(comparison["new"])
        case.after_audit.no_longer_detected = sorted(i["fingerprint"] for i in comparison["no_longer_detected"] if i["fingerprint"] == case.fingerprint)
        case.regressions = list(comparison["new"])
        if case.after_audit.target_present or case.after_audit.target_suppressed:
            how = "suppressed rather than fixed" if case.after_audit.target_suppressed and not case.after_audit.target_present else "still detected"
            case.transition(STATE_REJECTED, f"the finding is {how} after the patch", RC_FINDING_PERSISTS)
            return
        if case.regressions:
            case.transition(STATE_REJECTED, f"the candidate introduces {len(case.regressions)} new finding(s): " + ", ".join(f"{i['rule']} {i['file']}:{i['line']}" for i in case.regressions), RC_REGRESSION_INTRODUCED)
            return
        if executor is None:
            case.limitations.append("no test executed: " + str(denial))
            case.invariants = _invariants(case)
            case.transition(STATE_AUDIT_CLEAN, f"finding no longer detected and no new finding; tests not executed: {denial}", denial_code)
            return
        if not case.reproducer:
            case.invariants = _invariants(case)
            case.transition(STATE_INCONCLUSIVE, "no linked reproducer for this finding (use --link); the suite alone cannot attribute a verdict to one case", RC_NO_LINKED_TESTS)
            return
        if case.linked_before.outcome in OUTCOME_TO_INCONCLUSIVE:
            code, text = OUTCOME_TO_INCONCLUSIVE[case.linked_before.outcome]
            case.invariants = _invariants(case)
            case.transition(STATE_INCONCLUSIVE, f"before the patch: {text}", code)
            return
        if case.linked_before.outcome == OUTCOME_PASSED:
            case.invariants = _invariants(case)
            case.transition(STATE_INCONCLUSIVE, "the linked reproducer already passed before the patch: it does not observe the defect", RC_DEFECT_NOT_REPRODUCED)
            return
        case.linked_after = executor.run(case.reproducer, workspace, options.policy)
        case.invariants = _invariants(case)
        if case.linked_after.outcome == OUTCOME_PASSED:
            case.transition(STATE_VERIFIED, "finding no longer detected, no new finding, the linked reproducer failed before and passed after the patch")
        elif case.linked_after.outcome == OUTCOME_FAILED:
            case.transition(STATE_REJECTED, "the linked reproducer still fails after the patch", RC_TESTS_FAILED_AFTER)
        else:
            code, text = OUTCOME_TO_INCONCLUSIVE.get(case.linked_after.outcome, (RC_INFRASTRUCTURE_ERROR, "the after-run produced no verdict"))
            case.transition(STATE_INCONCLUSIVE, f"after the patch: {text}", code)
    finally:
        if not options.keep_workspace:
            workspace.cleanup()


def _combined(cases: List[RemediationCase], root: str, options: RunOptions, audit_options: AuditOptions, executor: Optional[Executor]) -> Dict[str, object]:
    candidates = [c for c in cases if c.patch is not None and c.state in (STATE_VERIFIED, STATE_AUDIT_CLEAN)]
    result: Dict[str, object] = {"cases": [c.fingerprint for c in candidates], "state": "not_run", "audit": None, "suite_before": TestRun().to_dict(), "suite_after": TestRun().to_dict()}
    if not candidates:
        return result
    workspace = Workspace.create(root, None if not options.workspace_dir else str(Path(options.workspace_dir) / "combined"))
    try:
        baseline = audit_project(str(workspace.project), options=audit_options)
        if executor is not None and options.policy.command:
            result["suite_before"] = executor.run(options.policy.command, workspace, options.policy).to_dict()
        for case in sorted(candidates, key=lambda c: (c.file, -c.line)):
            path = workspace.project / case.file
            path.write_text(apply_edits(path.read_text(encoding="utf-8"), case.patch.edits), encoding="utf-8")  # type: ignore[union-attr]
        after = audit_project(str(workspace.project), options=audit_options)
        comparison = compare_reports(after, baseline, "combined-before")
        remaining = [c.fingerprint for c in candidates if _presence(after, c.fingerprint).target_present or _presence(after, c.fingerprint).target_suppressed]
        audit_ok = not remaining and not comparison["new"]
        result["audit"] = {"targets_remaining": remaining, "new_findings": list(comparison["new"]), "ok": audit_ok}
        suite_ok: Optional[bool] = None
        if executor is not None and options.policy.command:
            run = executor.run(options.policy.command, workspace, options.policy)
            result["suite_after"] = run.to_dict()
            suite_ok = run.outcome == OUTCOME_PASSED
        result["state"] = "ok" if audit_ok and suite_ok is not False else "failed"
        if result["state"] == "failed":
            for case in candidates:
                case.limitations.append("the combined copy (all candidates together) " + ("still shows findings or new ones" if not audit_ok else "fails the suite command"))
        if options.keep_workspace:
            result["workspace"] = str(workspace.root)
    finally:
        if not options.keep_workspace:
            workspace.cleanup()
    return result


def run_remediation(project_root: str, options: Optional[RunOptions] = None) -> Dict[str, object]:
    options = options or RunOptions()
    root = str(Path(project_root).resolve())
    audit_options = AuditOptions(config_path=options.config_path)
    original = audit_project(root, options=audit_options)
    digest_before = digest_tree(root)
    selected = _select(original, options)
    cases = [
        RemediationCase(
            fingerprint=str(f["fingerprint"]), rule=str(f["rule"]), file=str(f["file"]), line=int(f["line"]), function=f.get("function"),  # type: ignore[arg-type]
            recipe=recipe_for(f), base_sha256=digest_before.get(str(f["file"])),
        )
        for f in selected
    ]
    executor, denial, denial_code = select_executor(options.authorization, root, options.executor)
    if options.authorization.execution and executor is None:
        denial = f"execution denied: {denial}"
    combined: Dict[str, object] = {"cases": [], "state": "not_run"}
    try:
        for case, finding in zip(cases, selected):
            _run_case(case, finding, root, options, audit_options, executor, denial, denial_code or "execution_unauthorized")
        combined = _combined(cases, root, options, audit_options, executor)
        if options.patch_dir:
            _write_patches(cases, options.patch_dir)
    finally:
        digest_after = digest_tree(root)
        untouched = digest_after == digest_before
    if not untouched:
        raise RemediationError("the audited project changed during the run; the remediation tool never writes to it, check for concurrent edits")
    by_state: Dict[str, int] = {}
    for case in cases:
        by_state[case.state] = by_state.get(case.state, 0) + 1
    return {
        "schema": RUN_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "project_root": root,
        "workspace": {"mode": "one copy per case plus a combined copy", "kept": bool(options.keep_workspace), "base_dir": options.workspace_dir, "ignored": list(Workspace.ignored)},
        "authorization": options.authorization.to_dict(),
        "execution": {
            "granted": executor is not None,
            "denied_reason": denial if executor is None else None,
            "denied_code": denial_code if executor is None else None,
            "executor": executor.to_dict() if executor is not None else None,
            "requested_executor": options.executor,
            "policy": options.policy.to_dict(),
            "links": dict(sorted(options.links.items())),
        },
        "original_untouched": untouched,
        "combined": combined,
        "cases": [c.to_dict() for c in cases],
        "summary": {
            "cases": len(cases),
            "by_state": dict(sorted(by_state.items())),
            "by_reason_code": dict(sorted({k: v for k, v in _count(c.reason_code for c in cases).items()}.items())),
            "selected_rules": sorted({c.rule for c in cases}),
            "composable": combined.get("state") == "ok" if combined.get("state") != "not_run" else None,
        },
        "note": (
            "States are claims about isolated copies, one per finding. 'verified' means: finding gone (not suppressed), no new "
            "finding, test files untouched, the linked reproducer failed before and passed after, run by the named executor. "
            "'audit_clean' means no test executed. 'inconclusive' means the evidence does not support a verdict; it is never a "
            "defect of the patch. Applying a diff to the project is a human decision."
        ),
    }


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        key = str(v)
        out[key] = out.get(key, 0) + 1
    return out


def _write_patches(cases: List[RemediationCase], patch_dir: str) -> None:
    out = Path(patch_dir)
    out.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if case.patch is not None:
            (out / f"{case.rule}-{case.fingerprint}-{case.patch.candidate_id}.diff").write_text(case.patch.diff, encoding="utf-8")

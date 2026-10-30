"""The remediation pipeline (v1.9): audit, copy, reproduce, patch, re-audit, re-test, decide.

Order of operations for one run:

1. audit the project read-only and select the findings that have an automatic recipe;
2. digest the project's source files;
3. copy the project into an isolated workspace and audit the copy: every selected finding must be present
   there with the same fingerprint (otherwise the case is rejected as not reproduced);
4. when execution is authorized, run the test command in the copy (**before**): a failing run documents that
   the defect is observable; a passing run means the suite does not cover it;
5. per file, from the highest line to the lowest, generate and apply each patch to the copy; preconditions
   that fail block the case, a file that changed since the audit rejects it;
6. audit the copy again and compare with the pre-patch audit: a patched case is rejected when its finding
   is still detected or when new findings appeared in its file;
7. when execution is authorized, run the test command again (**after**);
8. decide: ``verified`` needs a clean audit and passing tests; without executed tests the best is
   ``audit_clean``; failing tests after the patch reject the case;
9. digest the project again and record ``original_untouched``; write the patches as ``.diff`` files when asked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..engine import AuditOptions, audit_project
from ..model import TOOL_NAME, TOOL_VERSION
from ..report import compare_reports
from .isolation import ExecutionPolicy, Workspace, digest_tree, run_command
from .model import (
    STATE_AUDIT_CLEAN,
    STATE_BLOCKED,
    STATE_PATCHED,
    STATE_REJECTED,
    STATE_VERIFIED,
    AuditCheck,
    RemediationCase,
    RemediationError,
    TestRun,
)
from .patches import Blocked, propose, recipe_for

RUN_SCHEMA = "remediation_run/1.0"


@dataclass
class RunOptions:
    select_rules: Optional[Set[str]] = None
    fingerprints: Optional[Set[str]] = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    workspace_dir: Optional[str] = None
    keep_workspace: bool = False
    patch_dir: Optional[str] = None
    max_cases: int = 50
    config_path: Optional[str] = None
    include_tests: bool = False  # findings in test code are not patched unless asked


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
        if finding.get("environment") == "test" and not options.include_tests:
            continue
        if recipe_for(finding) is None:
            continue
        chosen.append(finding)
    chosen.sort(key=lambda f: (str(f["file"]), -int(f["line"]), str(f["rule"])))  # per file, highest line first
    return chosen[: options.max_cases]


def run_remediation(project_root: str, options: Optional[RunOptions] = None) -> Dict[str, object]:
    options = options or RunOptions()
    root = str(Path(project_root).resolve())
    audit_options = AuditOptions(config_path=options.config_path)
    original = audit_project(root, options=audit_options)
    selected = _select(original, options)
    cases = [
        RemediationCase(fingerprint=str(f["fingerprint"]), rule=str(f["rule"]), file=str(f["file"]), line=int(f["line"]), function=f.get("function"), recipe=recipe_for(f))  # type: ignore[arg-type]
        for f in selected
    ]
    by_fp = {c.fingerprint: c for c in cases}
    digest_before = digest_tree(root)
    workspace = Workspace.create(root, options.workspace_dir)
    before_tests = TestRun()
    after_tests = TestRun()
    try:
        baseline = audit_project(str(workspace.project), options=audit_options)
        baseline_fps = {f["fingerprint"]: f for f in baseline["findings"] if isinstance(f, dict)}  # type: ignore[index]
        for case in cases:
            present = case.fingerprint in baseline_fps
            case.before_audit = AuditCheck(target_present=present, findings_total=int(baseline["summary"]["findings_total"]))  # type: ignore[index]
            if not present:
                case.transition(STATE_REJECTED, "the finding was not reproduced by the audit of the isolated copy")
        if options.policy.authorized:
            before_tests = run_command(options.policy, workspace)
        # patches, per file from the highest line down (earlier edits never shift a pending case's lines)
        digest_copy = digest_tree(str(workspace.project))
        for finding in selected:
            case = by_fp[str(finding["fingerprint"])]
            if case.terminal:
                continue
            path = workspace.project / str(finding["file"])
            if digest_copy.get(str(finding["file"])) != digest_before.get(str(finding["file"])):
                case.transition(STATE_REJECTED, "the file in the copy differs from the audited project")
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                case.transition(STATE_REJECTED, f"cannot read the file in the copy: {exc}")
                continue
            result = propose(finding, source)
            if isinstance(result, Blocked):
                case.transition(STATE_BLOCKED, result.reason)
                continue
            try:
                path.write_text(_apply(source, result), encoding="utf-8")
            except OSError as exc:
                case.transition(STATE_REJECTED, f"cannot write the patched file in the copy: {exc}")
                continue
            case.patch = result
            case.transition(STATE_PATCHED, "patch applied in the isolated copy")
        patched = [c for c in cases if c.state == STATE_PATCHED]
        if patched:
            after = audit_project(str(workspace.project), options=audit_options)
            after_fps = {f["fingerprint"] for f in after["findings"] if isinstance(f, dict) and not f.get("suppressed")}  # type: ignore[index]
            comparison = compare_reports(after, baseline, "isolated-copy-before-patch")
            new_by_file: Dict[str, List[Dict[str, object]]] = {}
            for item in comparison["new"]:
                new_by_file.setdefault(str(item["file"]), []).append(item)
            gone = {item["fingerprint"] for item in comparison["no_longer_detected"]}
            for case in patched:
                case.after_audit = AuditCheck(
                    target_present=case.fingerprint in after_fps,
                    findings_total=int(after["summary"]["findings_total"]),  # type: ignore[index]
                    new_findings=list(new_by_file.get(case.file, [])),
                    no_longer_detected=sorted(fp for fp in gone if fp == case.fingerprint),
                )
                if case.after_audit.target_present:
                    case.transition(STATE_REJECTED, "the finding is still detected after the patch")
                elif case.after_audit.new_findings:
                    case.transition(STATE_REJECTED, f"the patch introduces {len(case.after_audit.new_findings)} new finding(s) in the file: " + ", ".join(f"{i['rule']}:{i['line']}" for i in case.after_audit.new_findings))
            still = [c for c in patched if c.state == STATE_PATCHED]
            if options.policy.authorized and still:
                after_tests = run_command(options.policy, workspace)
            for case in still:
                if options.policy.authorized and after_tests.ran:
                    if after_tests.passed:
                        case.transition(STATE_VERIFIED, "finding no longer detected, no new finding, tests passed after the patch")
                    else:
                        case.transition(STATE_REJECTED, "tests failed (or timed out) after the patch" + (f"; before the patch they {'failed' if before_tests.passed is False else 'passed'}" if before_tests.ran else ""))
                else:
                    reason = "finding no longer detected and no new finding; tests not executed"
                    if options.policy.authorized and after_tests.refused:
                        reason += f" ({after_tests.refused})"
                    case.transition(STATE_AUDIT_CLEAN, reason)
        if options.patch_dir:
            _write_patches(cases, options.patch_dir)
    finally:
        digest_after = digest_tree(root)
        untouched = digest_after == digest_before
        if not options.keep_workspace:
            workspace.cleanup()
    if not untouched:
        raise RemediationError("the audited project changed during the run; the remediation tool never writes to it, check for concurrent edits")
    by_state: Dict[str, int] = {}
    for case in cases:
        by_state[case.state] = by_state.get(case.state, 0) + 1
    return {
        "schema": RUN_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "project_root": root,
        "workspace": {"mode": "copy", "path": str(workspace.root) if options.keep_workspace else None, "kept": bool(options.keep_workspace), "ignored": list(workspace.ignored)},
        "authorization": options.policy.to_dict(),
        "isolation": {"home": "fresh directory inside the workspace", "stdin": "closed", "shell": False, "network": "not isolated (run inside a container when needed)", "limits": "cpu time and file size on POSIX"},
        "original_untouched": untouched,
        "defect_observed_before_patch": (before_tests.passed is False) if before_tests.ran else None,
        "tests": {"before": before_tests.to_dict(), "after": after_tests.to_dict()},
        "cases": [c.to_dict() for c in cases],
        "summary": {"cases": len(cases), "by_state": dict(sorted(by_state.items())), "selected_rules": sorted({c.rule for c in cases})},
        "note": "States are claims about the isolated copy only. 'verified' means the finding disappeared, nothing new appeared and the authorized tests passed there; applying the diff to the project is a human decision.",
    }


def _apply(source: str, proposal) -> str:
    from .patches import apply_edits

    return apply_edits(source, proposal.edits)


def _write_patches(cases: List[RemediationCase], patch_dir: str) -> None:
    out = Path(patch_dir)
    out.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if case.patch is not None:
            (out / f"{case.rule}-{case.fingerprint}.diff").write_text(case.patch.diff, encoding="utf-8")

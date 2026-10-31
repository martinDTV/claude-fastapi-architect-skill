"""Verification of a generated project (v2.0): audit with the existing engine, tests through the v1.9 trust model.

* the audit never executes the generated code (``fastapi_audit.engine.audit_project``);
* the generated tests run only with an explicit ``Authorization`` accepted by an executor (the local process
  executor accepts the ``fixture`` and ``trusted`` profiles and refuses ``untrusted``), through a runner adapter
  that binds the report to the run (``fastapi_audit.remediate``), inside a **copy** of the generated target
  (``Workspace.create``: no ``.git``, environments or caches), so the generated project is never modified by its own
  tests or by the tooling they pull in (virtual environments, lock files, caches); the copy's Python digest is
  compared before and after the run.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Optional

from fastapi_audit.engine import AuditOptions, audit_project
from fastapi_audit.remediate.executors import Authorization, select_executor
from fastapi_audit.remediate.isolation import ExecutionPolicy, Workspace, digest_tree


def audit_generated(target_dir: str) -> Dict[str, object]:
    report = audit_project(str(Path(target_dir).resolve()), options=AuditOptions())
    findings = [f for f in report["findings"] if isinstance(f, dict)]  # type: ignore[index]
    active = [f for f in findings if not f.get("suppressed")]
    by_severity: Dict[str, int] = {}
    for f in active:
        by_severity[str(f["severity"])] = by_severity.get(str(f["severity"]), 0) + 1
    return {
        "schema_version": report["schema_version"],
        "files_scanned": report["files_scanned"],
        "findings_total": len(active),
        "by_severity": dict(sorted(by_severity.items())),
        "findings": [
            {"rule": f["rule"], "file": f["file"], "line": f["line"], "severity": f["severity"], "confidence": f["confidence"], "evidence_state": f.get("evidence_state"), "message": str(f["message"])[:240]}
            for f in sorted(active, key=lambda f: (str(f["file"]), int(f["line"]), str(f["rule"])))
        ],
        "contracts": (report.get("contracts") or {}).get("summary"),
        "partial_analysis": bool(report["summary"].get("partial_analysis")),  # type: ignore[index]
    }


def run_generated_tests(target_dir: str, authorization: Authorization, policy: ExecutionPolicy, command: Optional[str], executor_name: str = "local") -> Dict[str, object]:
    """Run the generated project's tests under the trust model; never without an executor."""
    target = Path(target_dir).resolve()
    executor, denial, code = select_executor(authorization, str(target), executor_name)
    if executor is None:
        return {"ran": False, "denied_reason": denial, "denied_code": code, "run": None}
    if not command:
        return {"ran": False, "denied_reason": "no test command given", "denied_code": "no_command", "run": None, "executor": executor.to_dict()}
    workspace = Workspace.create(str(target))
    try:
        before = digest_tree(str(workspace.project))
        run = executor.run(command, workspace, policy)
        after = digest_tree(str(workspace.project))
        changed = sorted(set(k for k in set(before) | set(after) if before.get(k) != after.get(k)))
        return {
            "ran": run.ran,
            "executor": executor.to_dict(),
            "run": run.to_dict(),
            "workspace": {"copied": True, "python_files_digested": len(before), "python_files_changed_by_the_run": changed},
            "denied_reason": None,
            "denied_code": None,
        }
    finally:
        shutil.rmtree(workspace.root, ignore_errors=True)

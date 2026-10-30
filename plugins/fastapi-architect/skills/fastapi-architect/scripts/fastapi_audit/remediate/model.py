"""Remediation cases with verifiable states and per-finding evidence (v1.9, hardened).

States and what each one may claim about the isolated copy of one finding:

* ``proposed``: a recipe exists; nothing has been done.
* ``blocked``: a precondition of the recipe failed (``reason_code: precondition_failed``); the manual recipe applies.
* ``patched``: the candidate was applied in the case's own isolated copy; verification is incomplete.
* ``audit_clean``: after the patch the finding is no longer detected (not even as suppressed) and no new
  finding appeared; **no test was executed**, because execution was not authorized, the project's trust
  profile has no executor, or the project lies outside the authorized scope (``reason_code`` says which).
* ``verified``: ``audit_clean`` **and** the case's linked reproducer failed before the patch (the defect was
  observed) **and** passed after it, run by a named executor, with test files untouched. The model refuses
  the transition when any of these is missing.
* ``rejected``: the evidence shows the candidate is wrong or invalid: not reproduced in the copy, stale
  baseline, candidate mismatch, finding persists (or merely suppressed), test files modified, regression
  introduced, linked tests failed after the patch.
* ``inconclusive``: the evidence is insufficient or the failure is not attributable to the patch: no linked
  reproducer, the reproducer already passed before the patch, tests skipped or none collected,
  infrastructure error, timeout, command refused.

``audit_clean`` never equals ``verified``; a test that did not run never counts as passed; a skipped test
never satisfies an obligation; a baseline change invalidates the candidate; an infrastructure error is never
presented as a defect of the patch. Every transition is recorded in ``history`` with its reason code.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

STATE_PROPOSED = "proposed"
STATE_BLOCKED = "blocked"
STATE_PATCHED = "patched"
STATE_AUDIT_CLEAN = "audit_clean"
STATE_VERIFIED = "verified"
STATE_REJECTED = "rejected"
STATE_INCONCLUSIVE = "inconclusive"
STATES = (STATE_PROPOSED, STATE_BLOCKED, STATE_PATCHED, STATE_AUDIT_CLEAN, STATE_VERIFIED, STATE_REJECTED, STATE_INCONCLUSIVE)
TERMINAL_STATES = frozenset({STATE_BLOCKED, STATE_VERIFIED, STATE_REJECTED, STATE_INCONCLUSIVE})
TRANSITIONS: Dict[str, frozenset] = {
    STATE_PROPOSED: frozenset({STATE_BLOCKED, STATE_PATCHED, STATE_REJECTED, STATE_INCONCLUSIVE}),
    STATE_PATCHED: frozenset({STATE_AUDIT_CLEAN, STATE_VERIFIED, STATE_REJECTED, STATE_INCONCLUSIVE}),
    STATE_AUDIT_CLEAN: frozenset({STATE_VERIFIED, STATE_REJECTED, STATE_INCONCLUSIVE}),
    STATE_BLOCKED: frozenset(),
    STATE_VERIFIED: frozenset(),
    STATE_REJECTED: frozenset(),
    STATE_INCONCLUSIVE: frozenset(),
}

# reason codes (closed vocabulary; the human-readable reason adds detail)
RC_PRECONDITION_FAILED = "precondition_failed"
RC_NOT_REPRODUCED = "not_reproduced"  # the audit of the copy does not report the finding
RC_STALE_BASELINE = "stale_baseline"  # the file differs from the audited one
RC_CANDIDATE_MISMATCH = "candidate_mismatch"  # the candidate targets another file or another base
RC_PATCH_FAILED = "patch_failed"
RC_FINDING_PERSISTS = "finding_persists"  # still detected, active or suppressed
RC_TESTS_MODIFIED = "tests_modified"
RC_REGRESSION_INTRODUCED = "regression_introduced"
RC_TESTS_FAILED_AFTER = "tests_failed_after"
RC_EXECUTION_UNAUTHORIZED = "execution_unauthorized"
RC_EXECUTOR_UNAVAILABLE = "executor_unavailable"  # the trust profile has no executor that can run it safely
RC_SCOPE_MISMATCH = "scope_mismatch"
RC_NO_LINKED_TESTS = "no_linked_tests"
RC_DEFECT_NOT_REPRODUCED = "defect_not_reproduced"  # the reproducer already passed before the patch
RC_TESTS_SKIPPED = "tests_skipped"
RC_NO_TESTS = "no_tests"
RC_INFRASTRUCTURE_ERROR = "infrastructure_error"
RC_TIMEOUT = "timeout"
RC_COMMAND_REFUSED = "command_refused"
RC_VERIFIED = "verified"

# test outcomes (set by isolation.interpret_run)
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NO_TESTS = "no_tests"
OUTCOME_SKIPPED_ONLY = "skipped_only"
OUTCOME_INFRASTRUCTURE_ERROR = "infrastructure_error"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_REFUSED = "refused"
OUTCOME_NOT_RUN = "not_run"


class RemediationError(RuntimeError):
    """Raised when the pipeline cannot continue safely, or when a transition lacks its evidence."""


@dataclass
class Edit:
    """One line-level change: ``delete`` the line, ``replace`` it with ``text``, or ``insert_after`` it."""

    lineno: int
    kind: str
    text: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"line": self.lineno, "kind": self.kind, "text": self.text}


@dataclass
class PatchProposal:
    recipe: str
    file: str
    summary: str
    edits: List[Edit]
    diff: str
    original_sha256: str
    patched_sha256: str

    @property
    def candidate_id(self) -> str:
        return hashlib.sha256(f"{self.recipe}|{self.file}|{self.original_sha256}|{self.diff}".encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "recipe": self.recipe,
            "file": self.file,
            "summary": self.summary,
            "edits": [e.to_dict() for e in self.edits],
            "diff": self.diff,
            "original_sha256": self.original_sha256,
            "patched_sha256": self.patched_sha256,
        }


@dataclass
class TestRun:
    """One execution of a command in an isolated copy. ``outcome`` is derived from the runner's exit code and
    its summary line (``isolation.interpret_run``); ``ran`` is False when nothing executed."""

    ran: bool = False
    command: Optional[List[str]] = None
    returncode: Optional[int] = None
    passed: Optional[bool] = None
    timed_out: bool = False
    refused: Optional[str] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    outcome: str = OUTCOME_NOT_RUN
    runner: str = "generic"  # pytest | unittest | generic
    counts: Dict[str, int] = field(default_factory=dict)  # passed/failed/errors/skipped/ran when parseable
    executor: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "ran": self.ran,
            "command": list(self.command) if self.command else None,
            "executor": self.executor,
            "returncode": self.returncode,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "refused": self.refused,
            "outcome": self.outcome,
            "runner": self.runner,
            "counts": dict(self.counts),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass
class AuditCheck:
    target_present: Optional[bool] = None  # active finding with the case's fingerprint
    target_suppressed: Optional[bool] = None  # present but suppressed: still a defect, now hidden
    findings_total: Optional[int] = None
    new_findings: List[Dict[str, object]] = field(default_factory=list)  # anywhere in the copy, relative to the pre-patch audit
    no_longer_detected: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "target_present": self.target_present,
            "target_suppressed": self.target_suppressed,
            "findings_total": self.findings_total,
            "new_findings": list(self.new_findings),
            "no_longer_detected": list(self.no_longer_detected),
        }


@dataclass
class Invariant:
    statement: str
    checked_by: str  # audit | parse | linked_tests | not_checked
    satisfied: Optional[bool] = None

    def to_dict(self) -> Dict[str, object]:
        return {"statement": self.statement, "checked_by": self.checked_by, "satisfied": self.satisfied}


@dataclass
class RemediationCase:
    fingerprint: str
    rule: str
    file: str
    line: int
    function: Optional[str]
    recipe: Optional[str]
    base_sha256: Optional[str] = None  # sha256 of the audited file (the baseline the candidate was computed against)
    state: str = STATE_PROPOSED
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    patch: Optional[PatchProposal] = None
    reproducer: Optional[str] = None  # the linked command, as given
    linked_before: TestRun = field(default_factory=TestRun)
    linked_after: TestRun = field(default_factory=TestRun)
    before_audit: AuditCheck = field(default_factory=AuditCheck)
    after_audit: AuditCheck = field(default_factory=AuditCheck)
    invariants: List[Invariant] = field(default_factory=list)
    regressions: List[Dict[str, object]] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    tests_untouched: Optional[bool] = None
    executor: Optional[str] = None
    isolation_level: Optional[str] = None
    workspace_path: Optional[str] = None
    history: List[Dict[str, object]] = field(default_factory=list)

    # ------------------------------------------------------------------ evidence
    @property
    def defect_reproduced(self) -> Optional[bool]:
        if not self.linked_before.ran:
            return None
        if self.linked_before.outcome == OUTCOME_FAILED:
            return True
        if self.linked_before.outcome == OUTCOME_PASSED:
            return False
        return None

    def missing_evidence_for_verified(self) -> List[str]:
        """What still prevents ``verified``; empty when the evidence is sufficient."""
        missing: List[str] = []
        if self.patch is None:
            missing.append("no candidate patch")
        if self.after_audit.target_present is not False:
            missing.append("finding still detected after the patch (or not audited)")
        if self.after_audit.target_suppressed:
            missing.append("finding suppressed rather than fixed")
        if self.after_audit.new_findings:
            missing.append("new findings after the patch")
        if self.tests_untouched is not True:
            missing.append("test files were modified or not checked")
        if not self.reproducer:
            missing.append("no linked reproducer")
        if self.defect_reproduced is not True:
            missing.append("the reproducer did not fail before the patch")
        if not (self.linked_after.ran and self.linked_after.outcome == OUTCOME_PASSED):
            missing.append("the reproducer did not pass after the patch")
        if not self.executor:
            missing.append("no executor recorded")
        return missing

    def transition(self, new_state: str, reason: Optional[str] = None, reason_code: Optional[str] = None) -> None:
        if new_state not in STATES:
            raise RemediationError(f"unknown state {new_state!r}")
        if new_state not in TRANSITIONS[self.state]:
            raise RemediationError(f"{self.fingerprint}: transition {self.state} -> {new_state} is not allowed")
        if new_state == STATE_VERIFIED:
            missing = self.missing_evidence_for_verified()
            if missing:
                raise RemediationError(f"{self.fingerprint}: 'verified' refused, evidence missing: " + "; ".join(missing))
            reason_code = reason_code or RC_VERIFIED
        self.history.append({"from": self.state, "to": new_state, "reason_code": reason_code, "reason": reason})
        self.state, self.reason, self.reason_code = new_state, reason, reason_code

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> Dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "recipe": self.recipe,
            "base_sha256": self.base_sha256,
            "candidate_id": self.patch.candidate_id if self.patch else None,
            "state": self.state,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "applied_to_project": False,
            "patch": self.patch.to_dict() if self.patch else None,
            "reproducer": self.reproducer,
            "defect_reproduced": self.defect_reproduced,
            "linked_tests": {"before": self.linked_before.to_dict(), "after": self.linked_after.to_dict()},
            "verification": {"before_audit": self.before_audit.to_dict(), "after_audit": self.after_audit.to_dict()},
            "invariants": [i.to_dict() for i in self.invariants],
            "regressions": list(self.regressions),
            "tests_untouched": self.tests_untouched,
            "executor": self.executor,
            "isolation_level": self.isolation_level,
            "workspace": self.workspace_path,
            "limitations": list(self.limitations),
            "history": list(self.history),
        }

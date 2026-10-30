"""Remediation cases with verifiable states (v1.9).

States and what each one is allowed to claim:

* ``proposed``: a recipe exists for the finding; nothing has been done.
* ``blocked``: no safe automatic patch; a precondition of the recipe failed (the reason says which). The
  manual recipe in ``references/safe-refactoring.md`` still applies.
* ``patched``: a patch was generated and applied inside the isolated copy; verification is incomplete.
* ``audit_clean``: after the patch the target finding is no longer detected in the copy and no new finding
  appeared anywhere in it. Tests were **not** executed (no authorization), so nothing runtime is claimed.
* ``verified``: ``audit_clean`` **and** the authorized test command passed in the copy after the patch.
  When the same command failed before the patch, the defect was observed (``defect_observed_before_patch``).
* ``rejected``: the patch did not apply, broke parsing, left the finding, introduced findings, or the tests
  failed after it. The reason is recorded; the copy keeps the attempt for inspection when kept.

Transitions are enforced; every transition is appended to ``history`` with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

STATE_PROPOSED = "proposed"
STATE_BLOCKED = "blocked"
STATE_PATCHED = "patched"
STATE_AUDIT_CLEAN = "audit_clean"
STATE_VERIFIED = "verified"
STATE_REJECTED = "rejected"
STATES = (STATE_PROPOSED, STATE_BLOCKED, STATE_PATCHED, STATE_AUDIT_CLEAN, STATE_VERIFIED, STATE_REJECTED)
TERMINAL_STATES = frozenset({STATE_BLOCKED, STATE_VERIFIED, STATE_REJECTED})
TRANSITIONS: Dict[str, frozenset] = {
    STATE_PROPOSED: frozenset({STATE_BLOCKED, STATE_PATCHED, STATE_REJECTED}),
    STATE_PATCHED: frozenset({STATE_AUDIT_CLEAN, STATE_VERIFIED, STATE_REJECTED}),
    STATE_AUDIT_CLEAN: frozenset({STATE_VERIFIED, STATE_REJECTED}),
    STATE_BLOCKED: frozenset(),
    STATE_VERIFIED: frozenset(),
    STATE_REJECTED: frozenset(),
}


class RemediationError(RuntimeError):
    """Raised when the pipeline cannot continue safely (never because of a single case)."""


@dataclass
class Edit:
    """One line-level change: ``delete`` the line, ``replace`` it with ``text``, or ``insert_after`` it."""

    lineno: int  # 1-based, against the source the edit was computed on
    kind: str  # delete | replace | insert_after
    text: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"line": self.lineno, "kind": self.kind, "text": self.text}


@dataclass
class PatchProposal:
    recipe: str
    file: str
    summary: str
    edits: List[Edit]
    diff: str  # unified diff, a/<file> -> b/<file>
    original_sha256: str
    patched_sha256: str

    def to_dict(self) -> Dict[str, object]:
        return {
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
    """Result of the authorized command in the isolated copy. ``ran`` is False when nothing was executed."""

    ran: bool = False
    command: Optional[List[str]] = None
    returncode: Optional[int] = None
    passed: Optional[bool] = None
    timed_out: bool = False
    refused: Optional[str] = None  # why the command was not executed although execution was authorized
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "ran": self.ran,
            "command": list(self.command) if self.command else None,
            "returncode": self.returncode,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "refused": self.refused,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass
class AuditCheck:
    """What the auditor saw in the isolated copy at one point in time."""

    target_present: Optional[bool] = None
    findings_total: Optional[int] = None
    new_findings: List[Dict[str, object]] = field(default_factory=list)  # relative to the pre-patch audit of the copy
    no_longer_detected: List[str] = field(default_factory=list)  # fingerprints

    def to_dict(self) -> Dict[str, object]:
        return {
            "target_present": self.target_present,
            "findings_total": self.findings_total,
            "new_findings": list(self.new_findings),
            "no_longer_detected": list(self.no_longer_detected),
        }


@dataclass
class RemediationCase:
    fingerprint: str
    rule: str
    file: str
    line: int
    function: Optional[str]
    recipe: Optional[str]
    state: str = STATE_PROPOSED
    reason: Optional[str] = None
    patch: Optional[PatchProposal] = None
    before_audit: AuditCheck = field(default_factory=AuditCheck)
    after_audit: AuditCheck = field(default_factory=AuditCheck)
    history: List[Dict[str, object]] = field(default_factory=list)

    def transition(self, new_state: str, reason: Optional[str] = None) -> None:
        if new_state not in STATES:
            raise RemediationError(f"unknown state {new_state!r}")
        if new_state not in TRANSITIONS[self.state]:
            raise RemediationError(f"{self.fingerprint}: transition {self.state} -> {new_state} is not allowed")
        self.history.append({"from": self.state, "to": new_state, "reason": reason})
        self.state, self.reason = new_state, reason

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
            "state": self.state,
            "reason": self.reason,
            "applied_to_project": False,  # always: patches are applied in the isolated copy only
            "patch": self.patch.to_dict() if self.patch else None,
            "verification": {"before_audit": self.before_audit.to_dict(), "after_audit": self.after_audit.to_dict()},
            "history": list(self.history),
        }

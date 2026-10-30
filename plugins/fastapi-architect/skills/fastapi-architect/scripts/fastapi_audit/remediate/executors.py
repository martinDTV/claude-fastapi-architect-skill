"""Execution policy: trust profiles, authorization scope and interchangeable executors (v1.9, hardened).

The static steps (audit, candidate patches, re-audit of the copy) never execute project code. Executing
tests requires an explicit ``Authorization`` that names **what** is authorized (scope: a directory that
contains the project) and **under which trust profile**:

* ``fixture``: the plugin's own fixtures and examples;
* ``trusted``: a project the operator declares trustworthy (the declaration is recorded as such; it is a
  statement by the operator, not a technical guarantee, and the report says so);
* ``untrusted`` (default): no local execution. The run fails closed unless an executor whose
  ``isolation_level`` is ``isolated`` is registered. None ships in this increment, so untrusted projects get
  ``audit_clean`` at best with ``reason_code: executor_unavailable``.

``Executor`` is the seam for an isolated runner (container, microVM): it declares its isolation level, its
guarantees and what it does not provide, decides which trust profiles it accepts, and runs a command in a
workspace. ``LocalProcessExecutor`` is the only implementation: process hygiene (fresh HOME, reduced
environment, no shell, allow-listed programs, closed stdin, timeout, CPU/file-size limits), explicitly **not**
a sandbox: no network isolation, no filesystem confinement, no memory limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import time
import uuid

from .isolation import ExecutionPolicy, Workspace, command_allowed, parse_command, run_argv
from .model import TestRun
from .runners import Evidence, adapter_for, assess

TRUST_FIXTURE = "fixture"
TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted"
TRUST_PROFILES = (TRUST_FIXTURE, TRUST_TRUSTED, TRUST_UNTRUSTED)

ISOLATION_PROCESS_HYGIENE = "process_hygiene"
ISOLATION_ISOLATED = "isolated"


@dataclass
class Authorization:
    execution: bool = False
    trust: str = TRUST_UNTRUSTED
    scope: Optional[str] = None  # directory that must contain the project
    declared_by: Optional[str] = None  # e.g. "cli: --authorize-execution --trust trusted"
    note: Optional[str] = None

    def covers(self, project_root: str) -> Optional[str]:
        """None when the project is inside the authorized scope; otherwise the reason it is not."""
        if self.scope is None:
            return "no scope declared for the authorization"
        try:
            scope = Path(self.scope).resolve()
            project = Path(project_root).resolve()
        except OSError as exc:
            return f"cannot resolve the authorization scope: {exc}"
        if project != scope and scope not in project.parents:
            return f"project {project} is outside the authorized scope {scope}"
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "execution": bool(self.execution),
            "trust": self.trust,
            "scope": self.scope,
            "declared_by": self.declared_by,
            "note": self.note,
            "technical_guarantee": False,  # a declaration of trust is never a security property of the executor
        }


class Executor:
    """Interface of an executor. Subclasses override ``accepts`` and ``run``."""

    name = "abstract"
    isolation_level = "none"
    guarantees: Tuple[str, ...] = ()
    not_provided: Tuple[str, ...] = ()

    def accepts(self, trust: str) -> Optional[str]:
        return "abstract executor"

    def run(self, command: str, workspace: Workspace, policy: ExecutionPolicy) -> TestRun:  # pragma: no cover - interface
        raise NotImplementedError

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "isolation_level": self.isolation_level, "guarantees": list(self.guarantees), "not_provided": list(self.not_provided)}


class LocalProcessExecutor(Executor):
    name = "local"
    isolation_level = ISOLATION_PROCESS_HYGIENE
    guarantees = (
        "runs in a copy of the project (no .git, environments or caches)",
        "no shell; the program must be an interpreter, pytest or uv (extensible by the operator)",
        "fresh HOME inside the workspace; environment reduced to an explicit pass-through list",
        "stdin closed; wall-clock timeout; CPU-time and file-size limits on POSIX",
        "the project's digest is compared before and after the run",
    )
    not_provided = (
        "network isolation",
        "filesystem confinement beyond the fresh HOME (the tests can write anywhere the user can)",
        "memory limits",
        "protection against a malicious test suite: it runs with the operator's privileges",
    )

    def accepts(self, trust: str) -> Optional[str]:
        if trust in (TRUST_FIXTURE, TRUST_TRUSTED):
            return None
        return (
            "the local executor provides process hygiene, not isolation; an untrusted project needs an executor "
            "with isolation_level 'isolated' (none is registered in this version). Declare --trust trusted only "
            "for code you would run on this machine anyway."
        )

    def run(self, command: str, workspace: Workspace, policy: ExecutionPolicy) -> TestRun:
        """Instrument the command with its runner adapter, run it in the copy, read the report back and assess.
        A command without an adapter still runs (when allowed) but its outcome is ``unverifiable``."""
        argv = parse_command(command)
        refused = command_allowed(argv, policy.allowed_commands)
        if refused:
            result = TestRun(ran=False, command=argv, refused=refused)
            result.outcome, result.executor = assess(result, None), self.name
            return result
        adapter = adapter_for(argv)
        evidence_dir = workspace.root / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        nonce = uuid.uuid4().hex
        instrumented, report = adapter.instrument(argv, evidence_dir, nonce) if adapter else (list(argv), None)
        started = time.time()
        result = run_argv(policy, workspace, instrumented)
        result.command, result.instrumented_command = list(argv), list(instrumented)
        evidence = adapter.collect(report, nonce, started) if adapter and report is not None else Evidence(adapter=None)
        result.evidence = evidence.to_dict()
        result.counts = {k: v for k, v in evidence.counts.items() if v is not None}
        result.runner = adapter.name if adapter else "generic"
        result.outcome = assess(result, evidence)
        result.passed = result.outcome == "passed"
        result.executor = self.name
        return result


REGISTRY: Dict[str, Executor] = {LocalProcessExecutor.name: LocalProcessExecutor()}


def select_executor(authorization: Authorization, project_root: str, name: str = LocalProcessExecutor.name) -> Tuple[Optional[Executor], Optional[str], str]:
    """``(executor, denial_reason, reason_code)``. Fails closed: any doubt yields no executor."""
    from .model import RC_EXECUTION_UNAUTHORIZED, RC_EXECUTOR_UNAVAILABLE, RC_SCOPE_MISMATCH

    if not authorization.execution:
        return None, "execution not authorized (static steps only)", RC_EXECUTION_UNAUTHORIZED
    if authorization.trust not in TRUST_PROFILES:
        return None, f"unknown trust profile {authorization.trust!r}", RC_EXECUTOR_UNAVAILABLE
    executor = REGISTRY.get(name)
    if executor is None:
        return None, f"executor {name!r} is not registered (available: {', '.join(sorted(REGISTRY))})", RC_EXECUTOR_UNAVAILABLE
    refusal = executor.accepts(authorization.trust)  # the trust policy first: it explains why, whatever the scope says
    if refusal:
        return None, refusal, RC_EXECUTOR_UNAVAILABLE
    scope_problem = authorization.covers(project_root)
    if scope_problem:
        return None, scope_problem, RC_SCOPE_MISMATCH
    return executor, None, ""

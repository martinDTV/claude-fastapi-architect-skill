"""Design pipeline (v2.0): validated contract -> recommendation -> blueprint -> safe generation -> verification -> report.

``run_design`` is the single entry used by the CLI and the tests. Every step is deterministic for a given
specification; nothing in the generated project is executed unless an ``Authorization`` accepted by an executor
of the v1.9 trust model says so, and nothing in the target directory is overwritten unless asked explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi_audit.model import TOOL_VERSION
from fastapi_audit.remediate.executors import Authorization
from fastapi_audit.remediate.isolation import ExecutionPolicy

from .advisor import Recommendation, recommend
from .blueprint import Blueprint, build
from .generator import GenerationError, GenerationResult, generate
from .report import REPORT_SCHEMA
from .spec import Spec, SpecError, load_spec
from .verify import audit_generated, run_generated_tests

TOOL_NAME = "fastapi-architect design"
UNSUPPORTED_AUTHENTICATION = ("oauth2_jwt", "session")


class DesignError(RuntimeError):
    pass


@dataclass
class DesignOptions:
    target: Optional[str] = None
    allow_existing: bool = False
    overwrite: bool = False
    dry_run: bool = False
    validate_only: bool = False
    audit: bool = True
    authorization: Authorization = field(default_factory=Authorization)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    executor: str = "local"
    test_command: Optional[str] = None


def _spec_summary(spec: Spec) -> Dict[str, Any]:
    return {
        "name": spec.name,
        "schema": spec.schema,
        "digest": spec.digest,
        "source": spec.source,
        "package": spec.package,
        "modules": len(spec.modules),
        "entities": sum(len(m.entities) for m in spec.modules),
        "persistence": spec.persistence["kind"],
        "authentication": spec.security["authentication"],
        "multi_tenant": spec.multi_tenant,
        "explicit_paths": sum(1 for how in spec.provenance.values() if how == "explicit"),
        "assumed_paths": spec.assumed_paths(),
        "document": spec.to_dict(),
    }


def generation_refusal(spec: Spec) -> Optional[str]:
    """Why the generator will not emit code for this (valid) specification, if it will not."""
    auth = spec.security["authentication"]
    if auth in UNSUPPORTED_AUTHENTICATION:
        return f"authentication {auth!r} is accepted by the contract but this increment does not generate it; the generator refuses to emit a non-functional security stub (use api_key or none, and record the real scheme as a pending decision)"
    return None


def _next_steps(spec: Spec, rec: Recommendation, generation: Optional[GenerationResult], verification: Optional[Dict[str, Any]]) -> List[str]:
    steps: List[str] = []
    if generation and not generation.dry_run:
        steps.append(f"review the generated project in {generation.target} and put it under version control before changing it (the manifest in .fastapi-architect/generation.json tells later runs what was generated)")
        steps.append("create the environment and run the generated tests yourself: uv sync && uv run pytest -q (or pip install -e . --group dev)")
    if spec.persistence["kind"] == "postgresql":
        steps.append("add the Alembic environment and the first migration for the generated models (references/database-evolution.md); the generated API tests skip without DATABASE_URL")
    for item in rec.pending:
        steps.append(f"decide: {item}")
    if verification and verification.get("audit") and verification["audit"]["findings_total"]:
        steps.append("read the audit findings of the generated project: they are design questions the contract left open, not generator bugs to silence")
    steps.append("declare the next module in the specification and re-run the design: existing files are preserved, missing ones are added with --allow-existing")
    return steps


def run_design(spec_or_path: Union[str, Spec], options: Optional[DesignOptions] = None) -> Dict[str, Any]:
    options = options or DesignOptions()
    spec = spec_or_path if isinstance(spec_or_path, Spec) else load_spec(spec_or_path)  # raises SpecError
    rec = recommend(spec)
    refusal = generation_refusal(spec)
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "spec": _spec_summary(spec),
        "recommendation": rec.to_dict(),
        "layout": [],
        "files": [],
        "generation": None,
        "generation_refused": refusal,
        "verification": None,
        "authorization": options.authorization.to_dict(),
        "next_steps": [],
        "note": "the recommendation is derived from the specification's signals; assumptions and pending decisions are listed so the author can confirm or overrule them before building on the generated code",
    }
    if options.validate_only:
        report["next_steps"] = _next_steps(spec, rec, None, None)
        return report
    if refusal:
        raise DesignError(refusal)
    blueprint: Blueprint = build(spec, rec)
    report["layout"] = list(blueprint.layout)
    report["files"] = sorted(blueprint.files)
    report["blueprint_notes"] = list(blueprint.notes)
    if not options.target:
        report["next_steps"] = _next_steps(spec, rec, None, None)
        return report
    generation = generate(spec, blueprint, options.target, tool_version=TOOL_VERSION, allow_existing=options.allow_existing, overwrite=options.overwrite, dry_run=options.dry_run)
    report["generation"] = generation.to_dict()
    verification: Optional[Dict[str, Any]] = None
    if not options.dry_run:
        verification = {"audit": None, "tests": None}
        if options.audit:
            verification["audit"] = audit_generated(generation.target)
        if options.authorization.execution or options.test_command:
            verification["tests"] = run_generated_tests(generation.target, options.authorization, options.policy, options.test_command, executor_name=options.executor)
        report["verification"] = verification
    report["next_steps"] = _next_steps(spec, rec, generation, verification)
    return report


__all__ = ["DesignError", "DesignOptions", "GenerationError", "SpecError", "generation_refusal", "run_design"]

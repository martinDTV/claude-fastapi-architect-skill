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
IMPLEMENTED_TENANT_BINDINGS = ("api_key_registry",)


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
    if spec.multi_tenant and spec.security["multi_tenant"]["binding"] not in IMPLEMENTED_TENANT_BINDINGS:
        return (
            "multi-tenant contract without a principal-tenant binding (security.multi_tenant.binding is "
            f"{spec.security['multi_tenant']['binding']!r}): the generated routes would accept any tenant_id from any authenticated caller, "
            "which is not authorization; declare binding 'api_key_registry' (each API key is bound to one tenant) or disable multi-tenancy"
        )
    return None


def security_guarantees(spec: Spec) -> Dict[str, Any]:
    """What the generated project does and does not guarantee, stated so the report can never be read as
    'secure' when a property is declared but not implemented."""
    mt = spec.security["multi_tenant"]
    pg = spec.persistence["kind"] == "postgresql"
    auth = spec.security["authentication"]
    binding = mt["binding"] if spec.multi_tenant else None
    if auth == "api_key" and binding == "api_key_registry":
        authentication = "api_key_registry: X-API-Key compared in constant time against a registry of keys, each bound to one tenant"
    elif auth == "api_key":
        authentication = "api_key_shared: X-API-Key compared in constant time against one configured key (no per-caller identity)"
    elif auth == "none":
        authentication = "none: every route is public by design (declared in .audit-fastapi.json)"
    else:
        authentication = f"{auth}: accepted by the contract, not generated (generation refused)"
    storage = []
    if spec.multi_tenant:
        storage.append("tenant_id column on every entity and a tenant filter on every repository query")
        if pg:
            storage.append("composite foreign keys (tenant_id, id) between tenant tables: a row cannot reference another tenant's row")
    return {
        "multi_tenant": {
            "requested": spec.multi_tenant,
            "strategy_declared": mt["strategy"] if spec.multi_tenant else None,
            "strategy_implemented": "shared_schema_application_scoping" if spec.multi_tenant else None,
            "principal_tenant_binding": (
                "enforced: the tenant path parameter must equal the tenant bound to the presented API key, otherwise 403"
                if binding == "api_key_registry" else ("none" if spec.multi_tenant else None)
            ),
            "storage_isolation": storage,
            "row_level_security": False,
            "schema_or_database_per_tenant": False,
        },
        "authentication": authentication,
        "resource_authorization": (
            "not generated" + (f" (spec.security.authorization = {spec.security['authorization']!r} is a pending decision)" if spec.security["authorization"] != "none" else "")
        ),
        "persistence": {
            "kind": spec.persistence["kind"],
            "migrations": "alembic: generated environment and initial migration; tables are created only by `alembic upgrade head`" if pg else "none (in-memory)",
            "create_all_used": False,
            "constraint_violation_on_commit": "rolled back and answered 409 (problem+json)" if pg else "not applicable",
        },
        "execution": "generated tests run only through the v1.9 trust model, in a copy of the project; the local executor is process hygiene, not a sandbox",
        "not_claimed": [
            "row-level security",
            "schema or database per tenant",
            "resource-level authorization",
            "OAuth2/JWT or session authentication",
            "network or filesystem isolation of executed tests",
        ],
    }


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
        "guarantees": security_guarantees(spec),
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


__all__ = ["DesignError", "DesignOptions", "GenerationError", "SpecError", "generation_refusal", "run_design", "security_guarantees"]

"""Architecture advice from the contract (v2.0): a reasoned, deterministic recommendation, never a default dogma.

The advisor reads the signals of the specification (modules, scope, team, integrations, tenancy, consistency,
stated preferences and accepted decisions) and recommends one style with its reasons, the alternatives it
rejected and why, the assumptions it relied on (values the author did not write) and the decisions that remain
pending. Styles:

* ``layered``: one application package organised by layer (api, schemas, services, repositories, models). Right
  for a small scope with one or two modules and a small team: the cheapest structure that still separates
  transport, use cases and persistence.
* ``modular_monolith``: one deployable, modules by business capability with their own router, schemas, service,
  repository and models, dependencies between modules declared and checked (import-linter). The default once
  there are three or more modules, a medium scope, or a team that needs ownership boundaries.
* ``hexagonal``: a modular monolith whose modules with external integrations isolate them behind ports (Protocols)
  and adapters, so the use case does not depend on the HTTP client, broker or SMTP library. Recommended when
  there are two or more outbound integrations, when an integration is high-criticality, or when contract tests
  are required; never imposed on modules without integrations.
* ``microservices``: only when the author states independent deployment and the team and module count justify
  it (an accepted decision, a team of eight or more, four or more modules). Otherwise rejected with the reason:
  the cost (network, consistency, operations) is not paid for by the stated requirements.

An explicit ``architecture.style_preference`` is honoured, with a warning in the rationale when the signals point
elsewhere; the report shows both. DDD, CQRS and event sourcing are not recommended by this advisor: when the
specification shows signals for them (eventual consistency with a broker, audit trails) they appear as pending
decisions with a pointer to the references, not as structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .spec import Spec

STYLE_LAYERED = "layered"
STYLE_MODULAR = "modular_monolith"
STYLE_HEXAGONAL = "hexagonal"
STYLE_MICROSERVICES = "microservices"


@dataclass
class Recommendation:
    style: str
    rationale: List[str]
    rejected: List[Dict[str, str]]
    assumptions: List[str]
    pending: List[str]
    signals: Dict[str, object]
    ports_modules: List[str] = field(default_factory=list)  # modules that get ports and adapters (hexagonal)
    warnings: List[str] = field(default_factory=list)
    persistence_notes: List[str] = field(default_factory=list)
    security_notes: List[str] = field(default_factory=list)
    testing_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "style": self.style,
            "rationale": list(self.rationale),
            "rejected": list(self.rejected),
            "assumptions": list(self.assumptions),
            "pending": list(self.pending),
            "signals": dict(self.signals),
            "ports_modules": list(self.ports_modules),
            "warnings": list(self.warnings),
            "persistence_notes": list(self.persistence_notes),
            "security_notes": list(self.security_notes),
            "testing_notes": list(self.testing_notes),
        }


def recommend(spec: Spec) -> Recommendation:
    modules = len(spec.modules)
    entities = sum(len(m.entities) for m in spec.modules)
    outbound = [i for i in spec.integrations if i.direction == "outbound"]
    critical = [i for i in spec.integrations if i.criticality == "high"]
    team = int(spec.scope["team_size"])
    size = str(spec.scope["size"])
    independent = bool(spec.architecture["independent_deployment"])
    accepted_split = any(d.status == "accepted" and "deploy" in d.title.lower() for d in spec.decisions)
    contract_tests = "contract" in spec.testing["levels"]
    signals: Dict[str, object] = {
        "modules": modules, "entities": entities, "integrations": len(spec.integrations), "outbound_integrations": len(outbound),
        "high_criticality_integrations": len(critical), "team_size": team, "size": size, "expected_rps": spec.scope["expected_rps"],
        "consistency": spec.nonfunctional["consistency"], "multi_tenant": spec.multi_tenant, "independent_deployment": independent,
        "style_preference": spec.architecture["style_preference"], "contract_tests": contract_tests,
    }
    rationale: List[str] = []
    rejected: List[Dict[str, str]] = []
    warnings: List[str] = []

    # computed style
    wants_micro = independent and (accepted_split or team >= 8) and modules >= 4
    if wants_micro:
        computed = STYLE_MICROSERVICES
        rationale.append(f"independent deployment is stated, {modules} modules and a team of {team}: separate services are justified; the generator still produces one modular monolith per bounded context as the first step")
    elif len(outbound) >= 2 or critical or (contract_tests and outbound):
        computed = STYLE_HEXAGONAL
        why = []
        if len(outbound) >= 2:
            why.append(f"{len(outbound)} outbound integrations")
        if critical:
            why.append(f"{len(critical)} high-criticality integration(s)")
        if contract_tests and outbound:
            why.append("contract tests required")
        rationale.append("ports and adapters for the integrating modules: " + ", ".join(why) + "; use cases must not depend on the client libraries")
    elif modules >= 3 or size in ("medium", "large") or team >= 4:
        computed = STYLE_MODULAR
        why = []
        if modules >= 3:
            why.append(f"{modules} modules")
        if size in ("medium", "large"):
            why.append(f"{size} scope")
        if team >= 4:
            why.append(f"team of {team}")
        rationale.append("modules by business capability with declared dependencies: " + ", ".join(why) + "; one deployable keeps transactions and operations simple")
    else:
        computed = STYLE_LAYERED
        rationale.append(f"{modules} module(s), {entities} entit{'y' if entities == 1 else 'ies'}, team of {team}, {size} scope: one package by layer is the cheapest structure that still separates transport, use cases and persistence")

    # rejected alternatives, with reasons
    if computed != STYLE_MICROSERVICES:
        reason = "independent deployment is not stated" if not independent else ("the team or module count does not justify the operational cost" if not (accepted_split or team >= 8) or modules < 4 else "")
        rejected.append({"style": STYLE_MICROSERVICES, "reason": reason + "; network partitions, distributed consistency and per-service operations are paid for only when deployment independence is required"})
    if computed != STYLE_HEXAGONAL:
        rejected.append({"style": STYLE_HEXAGONAL, "reason": f"{len(outbound)} outbound integration(s) and no high-criticality dependency: ports and adapters would add indirection without a second implementation to protect"})
    if computed == STYLE_LAYERED:
        rejected.append({"style": STYLE_MODULAR, "reason": "fewer than three modules and a small team: module boundaries can be introduced when a second capability arrives (the layout keeps one package per layer that maps onto modules later)"})
    if computed in (STYLE_MODULAR, STYLE_HEXAGONAL, STYLE_MICROSERVICES):
        rejected.append({"style": STYLE_LAYERED, "reason": "a single package by layer blurs ownership once several capabilities share services and repositories"})

    # explicit preference
    preference = spec.architecture["style_preference"]
    style = computed
    if preference and preference != computed:
        style = preference
        warnings.append(f"the author prefers '{preference}' while the signals point to '{computed}'; the preference is honoured and the computed alternative is recorded")
        rationale.insert(0, f"style '{preference}' chosen by explicit preference (spec.architecture.style_preference)")
    elif preference:
        rationale.insert(0, f"style '{preference}' confirmed by explicit preference and by the signals")

    ports_modules: List[str] = []
    if style == STYLE_HEXAGONAL:
        with_integration = {i.module for i in spec.integrations if i.module}
        ports_modules = sorted(m.name for m in spec.modules if m.name in with_integration) or [spec.modules[0].name]
        if not with_integration:
            warnings.append("no integration names its module; ports and adapters are generated for the first module and should be moved to the integrating one")

    assumptions = [f"{path} was not stated and defaults to the generator's assumption" for path in spec.assumed_paths()]
    assumptions.extend(f"author's assumption: {a}" for a in spec.assumptions)
    pending = spec.pending_decisions()

    persistence_notes: List[str] = []
    if spec.persistence["kind"] == "memory":
        persistence_notes.append("in-memory repositories: suitable for a prototype or for the first vertical slice; data does not survive a restart")
    else:
        persistence_notes.append("PostgreSQL through SQLAlchemy 2 async sessions provided per request; one commit per use case in the service")
        if not spec.persistence["migrations"]:
            pending.append("persistence: migrations are not enabled; the generated models have no Alembic environment yet (references/database-evolution.md)")
    if spec.nonfunctional["consistency"] == "eventual" or spec.infrastructure["broker"]:
        pending.append("eventual consistency or a broker is stated: outbox/inbox and idempotency need a decision (references/events-and-workflows.md); not generated")
    if spec.nonfunctional["auditability"]:
        pending.append("auditability is required: an append-only audit trail or event log needs a decision; not generated")

    security_notes: List[str] = []
    auth = spec.security["authentication"]
    if auth == "none":
        security_notes.append("no authentication: every route is declared public in the generated .audit-fastapi.json so the auditor reports the exposure as a decision, not a finding")
    elif auth == "api_key":
        security_notes.append("API key in the X-API-Key header compared in constant time; applied as a router dependency to every business route")
    else:
        pending.append(f"authentication '{auth}' is accepted by the contract but not generated in this increment (references/security-and-config.md); the generator refuses to emit a non-functional stub")
    if spec.security["authorization"] != "none":
        pending.append(f"authorization '{spec.security['authorization']}' needs a policy decision (role model or resource ownership); only authentication is generated")
    if spec.multi_tenant:
        strategy = spec.security["multi_tenant"]["strategy"]
        security_notes.append(f"multi-tenant ({strategy}): the tenant id is a path parameter scoped into every repository call and declared as tenant_params for the auditor")
        pending.append("multi-tenant: binding the tenant to the authenticated principal (membership) is a decision the generator does not take (references/authz-multitenancy.md); the auditor will ask about it")
        if strategy != "shared_schema_rls":
            pending.append(f"tenant strategy '{strategy}' is recorded but the generated persistence uses one schema; schema or database per tenant needs its own provisioning design")

    testing_notes = [f"generated tests cover the {'unit' if 'unit' in spec.testing['levels'] else 'integration'} level of every operation through the HTTP boundary (create, validation, not found, list)"]
    if "end_to_end" in spec.testing["levels"]:
        pending.append("end-to-end tests are requested; the generator produces API-level tests only")

    return Recommendation(style=style, rationale=rationale, rejected=rejected, assumptions=assumptions, pending=sorted(set(pending)), signals=signals, ports_modules=ports_modules, warnings=warnings, persistence_notes=persistence_notes, security_notes=security_notes, testing_notes=testing_notes)

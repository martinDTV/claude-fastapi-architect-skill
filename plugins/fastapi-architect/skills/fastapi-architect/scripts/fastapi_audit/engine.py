"""Orchestration: discovery -> project facts -> per-file rules -> classification -> report dict."""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .discovery import DEFAULT_MAX_FILE_BYTES, classify_environment, iter_python_files
from .config_schema import ConfigError, validate_config
from .facts import ProjectFacts, collect_facts, load_config, module_name_for
from .kinds import module_level_factories
from .model import (
    CATEGORIES,
    CONFIDENCE_DOWNGRADE,
    DISCLAIMER,
    EVIDENCE_STATES,
    ENV_APPLICATION,
    ENV_MIGRATION,
    ENV_TEST,
    PYTHON_VERSION,
    RULES,
    SCHEMA_VERSION,
    SEVERITY_ORDER,
    TOOL_NAME,
    TOOL_VERSION,
    Finding,
    evidence_state_for,
    new_finding,
)
from .report import compare_with_baseline
from .resolver import awaited_call_ids, collect_functions, module_imports, resolve_in_scopes
from .remediation import REMEDIATIONS, build_remediation
from .rules import FileContext, architecture, async_rules, declared, interprocedural, lifecycle, migrations, reliability, security, sqlalchemy_rules, transactions
from .suppressions import line_comments, suppression_for

FILE_RULE_MODULES = (async_rules, sqlalchemy_rules, transactions, migrations, security, interprocedural, reliability, lifecycle, declared)
TEST_DOWNGRADE_PREFIXES = ("ASYNC_BLOCKING_CALL", "SA_", "TX_", "RETRY_", "BG_")
ASYNC_TEST_NOTE = (
    " Test code: the stalled event loop is the test's own; confirm the call does not run "
    "inside the system under test."
)
GENERIC_TEST_NOTE = " Test code: review only if the test mirrors production usage."


@dataclass
class AuditOptions:
    excluded_dirs: Sequence[str] = ()
    include_hidden: bool = False
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    exclude_tests: bool = False
    honor_suppressions: bool = True
    categories: Optional[Set[str]] = None  # None = all
    select_rules: Optional[Set[str]] = None  # None = all
    ignore_rules: Set[str] = field(default_factory=set)
    include_globs: Sequence[str] = ()
    exclude_globs: Sequence[str] = ()
    only_files: Optional[Set[str]] = None  # report findings only for these relpaths
    config_path: Optional[str] = None
    baseline: Optional[Dict[str, Any]] = None
    baseline_name: Optional[str] = None
    interprocedural: bool = True  # v1.5: summaries + call graph
    max_call_depth: int = 4
    time_budget_s: float = 5.0

    def effective(self) -> Dict[str, Any]:
        return {
            "interprocedural": self.interprocedural,
            "max_call_depth": self.max_call_depth,
            "time_budget_s": self.time_budget_s,  # schema 1.4
            "categories": sorted(self.categories) if self.categories is not None else sorted(CATEGORIES),
            "select_rules": sorted(self.select_rules) if self.select_rules is not None else None,
            "ignore_rules": sorted(self.ignore_rules),
            "exclude_tests": self.exclude_tests,
            "honor_suppressions": self.honor_suppressions,
            "include_hidden": self.include_hidden,
            "include_paths": list(self.include_globs),
            "exclude_paths": list(self.exclude_globs),
            "excluded_dirs": sorted(self.excluded_dirs),
            "max_file_bytes": self.max_file_bytes,
            "changed_files_only": self.only_files is not None,
            "baseline": self.baseline_name,
        }


# --------------------------------------------------------------------------- #
# per-file analysis
# --------------------------------------------------------------------------- #


def _parse_failure(relpath: str, environment: str, exc: BaseException) -> Finding:
    if isinstance(exc, (RecursionError, MemoryError)):
        return new_finding(
            "FILE_SKIPPED", relpath, 1, 0,
            "File exceeded parser limits (nesting depth or memory) and was skipped.",
            confidence="high", environment=environment,
        )
    line = getattr(exc, "lineno", None) or 1
    column = getattr(exc, "offset", None) or 0
    detail = getattr(exc, "msg", None) or exc.__class__.__name__
    return new_finding(
        "PY_SYNTAX", relpath, line, column,
        (
            f"File could not be parsed with Python {PYTHON_VERSION}: {detail}. "
            "Other rules did not run on it. If the project targets a newer "
            "Python, rerun the auditor with that interpreter."
        ),
        confidence="high", environment=environment,
    )


def build_context(relpath: str, source: bytes, tree: ast.Module, facts: Optional[ProjectFacts]) -> FileContext:
    environment = classify_environment(relpath)
    fastapi_imported = module_imports(tree, ("fastapi",))
    sqlalchemy_imported = module_imports(tree, ("sqlalchemy", "sqlmodel"))
    alembic_imported = module_imports(tree, ("alembic",))
    module = facts.file_module.get(relpath) if facts is not None else None
    if module is None:
        names = module_name_for(relpath, ["src"])
        module = names[0] if names else None
    module_scope, functions = collect_functions(tree, fastapi_imported, module=module, is_package=relpath.endswith("__init__.py"))
    if environment == ENV_APPLICATION and alembic_imported:
        is_revision = any(f.name == "upgrade" and f.parent is None for f in functions)
        is_env = os.path.basename(relpath) == "env.py"
        if is_revision or is_env:
            environment = ENV_MIGRATION
    returned: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
            returned.add(id(node.value))
    local_factories = module_level_factories(tree, lambda parts: resolve_in_scopes([module_scope], parts))
    return FileContext(
        relpath=relpath,
        tree=tree,
        source=source,
        environment=environment,
        module_scope=module_scope,
        functions=functions,
        fastapi_imported=fastapi_imported,
        sqlalchemy_imported=sqlalchemy_imported,
        alembic_imported=alembic_imported,
        facts=facts,
        local_factories=local_factories,
        awaited=awaited_call_ids(tree),
        returned=returned,
    )


def _contracts_block(facts: ProjectFacts) -> Dict[str, Any]:
    routes = sorted(facts.route_contracts, key=lambda c: (c["file"], c["line"], c["function"]))
    counts = {
        "routes": len(routes),
        "authenticated": sum(1 for c in routes if c["authentication"] == "present"),
        "public": sum(1 for c in routes if c["public"]),  # declared only (config glob or marker)
        "exposure_undetermined": sum(1 for c in routes if c.get("bucket") == "exposure_undetermined"),
        "unauthenticated_not_public": sum(1 for c in routes if c["authentication"] == "absent" and not c["public"] and c.get("bucket") != "exposure_undetermined"),
        "authorization_visible": sum(1 for c in routes if c["authorization"]),
        "authorization_unknown": sum(1 for c in routes if c["authentication"] == "present" and not c["authorization"] and (c["resource_params"] or c["tenant_params"])),
    }
    kinds: Dict[str, int] = {}
    partition = {"public_declared": 0, "exposure_undetermined": 0, "unauthenticated_not_public": 0, "optional_authentication": 0, "authenticated_authorization_visible": 0, "authenticated_only": 0, "authenticated_authorization_unknown": 0}
    for c in routes:
        for k in c["authorization"]:
            kinds[k] = kinds.get(k, 0) + 1
        partition[c.get("bucket", "authenticated_only")] += 1
    counts["optional_authentication"] = partition["optional_authentication"]
    return {"summary": counts, "partition": partition, "authorization_kinds": dict(sorted(kinds.items())), "routes": routes}


def _rule_enabled(rule: str, options: AuditOptions) -> bool:
    if rule in options.ignore_rules:
        return False
    if options.select_rules is not None:
        return rule in options.select_rules
    category = RULES[rule]["category"]
    if category == "files":
        return True  # file-level problems are always reported unless ignored explicitly
    return options.categories is None or category in options.categories


def _classify(finding: Finding, environment: str, comments: Dict[int, str], honor_suppressions: bool) -> Finding:
    confidence = finding.confidence
    message = finding.message
    if environment == ENV_TEST and finding.rule.startswith(TEST_DOWNGRADE_PREFIXES):
        confidence = CONFIDENCE_DOWNGRADE[confidence]
        message += ASYNC_TEST_NOTE if finding.rule == "ASYNC_BLOCKING_CALL" else GENERIC_TEST_NOTE
    suppression = suppression_for(finding.rule, comments.get(finding.line)) if honor_suppressions else None
    classified = replace(
        finding,
        confidence=confidence,
        message=message,
        environment=environment if finding.environment == ENV_APPLICATION else finding.environment,
        suppressed=suppression is not None,
        suppression=suppression,
    )
    return replace(classified, evidence_state=evidence_state_for(classified))


def analyse_source(
    source: bytes,
    relpath: str,
    *,
    facts: Optional[ProjectFacts] = None,
    options: Optional[AuditOptions] = None,
) -> List[Finding]:
    """Analyse one Python buffer. Never raises for malformed input."""
    options = options or AuditOptions()
    environment = classify_environment(relpath)
    try:
        tree = ast.parse(source, filename=relpath)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        finding = _parse_failure(relpath, environment, exc)
        return [finding] if _rule_enabled(finding.rule, options) else []
    try:
        ctx = build_context(relpath, source, tree, facts)
    except (RecursionError, MemoryError) as exc:
        finding = _parse_failure(relpath, environment, exc)
        return [finding] if _rule_enabled(finding.rule, options) else []
    raw: List[Finding] = []
    for module in FILE_RULE_MODULES:
        try:
            raw.extend(module.run(ctx))
        except (RecursionError, MemoryError) as exc:
            # pathological nesting inside one rule module: keep the other modules' results and say so
            raw.append(new_finding(
                "ANALYSIS_PARTIAL", relpath, 1, 0,
                f"Rule module '{module.__name__.rsplit('.', 1)[-1]}' hit a resource limit ({exc.__class__.__name__}) on this file "
                "and reported nothing for it; the other rule modules ran. The analysis of this file is partial.",
                confidence="high", environment=ctx.environment,
            ))
    raw = [f for f in raw if _rule_enabled(f.rule, options)]
    comments = line_comments(source) if (options.honor_suppressions and raw) else {}
    return [_classify(f, ctx.environment, comments, options.honor_suppressions) for f in raw]


def audit_source(source: bytes, relpath: str, *, honor_suppressions: bool = True, facts: Optional[ProjectFacts] = None) -> List[Finding]:
    """Backward-compatible helper (v1.3 signature)."""
    return analyse_source(source, relpath, facts=facts, options=AuditOptions(honor_suppressions=honor_suppressions))


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #


def audit_project(
    root: str,
    *,
    excluded_dirs: Iterable[str] = (),
    include_hidden: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    exclude_tests: bool = False,
    honor_suppressions: bool = True,
    options: Optional[AuditOptions] = None,
) -> Dict[str, object]:
    """Audit a directory (or single file) and return the JSON-ready report."""
    if options is None:
        options = AuditOptions(
            excluded_dirs=tuple(excluded_dirs),
            include_hidden=include_hidden,
            max_file_bytes=max_file_bytes,
            exclude_tests=exclude_tests,
            honor_suppressions=honor_suppressions,
        )
    root_abs = os.path.abspath(root)
    if not os.path.exists(root_abs):
        raise FileNotFoundError(root_abs)

    config_root = root_abs if os.path.isdir(root_abs) else os.path.dirname(root_abs)
    config, config_source = load_config(config_root, options.config_path)
    problems = validate_config(config, config_source)
    if problems:
        raise ConfigError(config_source, problems)  # fail closed: no report is produced
    excluded = list(options.excluded_dirs) + [str(d) for d in config.get("exclude", []) if isinstance(d, str)]
    include_globs = list(options.include_globs) + [str(g) for g in config.get("include_paths", []) if isinstance(g, str)]
    exclude_globs = list(options.exclude_globs) + [str(g) for g in config.get("exclude_paths", []) if isinstance(g, str)]
    if options.ignore_rules is not None:
        options.ignore_rules = set(options.ignore_rules) | {str(r) for r in config.get("ignore_rules", []) if isinstance(r, str)}

    tests_excluded: List[int] = []
    files, skipped = iter_python_files(
        root_abs,
        excluded_dirs=excluded,
        include_hidden=options.include_hidden,
        exclude_tests=options.exclude_tests,
        tests_excluded=tests_excluded,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
    )
    facts = collect_facts(
        config_root,
        files,
        config=config,
        config_source=config_source,
        max_file_bytes=options.max_file_bytes,
        interprocedural=options.interprocedural,
        max_call_depth=options.max_call_depth,
        time_budget_s=options.time_budget_s,
    )

    findings: List[Finding] = []
    for rel in sorted(facts.summary_failures):
        partial = new_finding(
            "ANALYSIS_PARTIAL", rel, 1, 0,
            "The interprocedural pass hit a resource limit (recursion or memory) while summarising this file; "
            "its functions are absent from the call graph. Per-file rules still ran on it.",
            confidence="high", environment=classify_environment(rel),
        )
        findings.append(replace(partial, evidence_state=evidence_state_for(partial)))
    for entry in skipped:
        if entry["reason"] == "unreadable_directory":
            findings.append(new_finding(
                "FILE_SKIPPED", entry["file"], 1, 0,
                "Directory could not be listed and was skipped; the analysis of this subtree is partial.",
                confidence="high", environment=classify_environment(entry["file"]),
            ))
    files_scanned = 0
    for rel, full in files:
        try:
            if os.stat(full).st_size > options.max_file_bytes:
                skipped.append({"file": rel, "reason": "exceeds_max_file_bytes"})
                findings.append(new_finding(
                    "FILE_SKIPPED", rel, 1, 0,
                    f"File is larger than {options.max_file_bytes} bytes and was skipped.",
                    confidence="high", environment=classify_environment(rel),
                ))
                continue
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            skipped.append({"file": rel, "reason": "unreadable"})
            findings.append(new_finding(
                "FILE_SKIPPED", rel, 1, 0,
                f"File could not be read ({exc.__class__.__name__}) and was skipped.",
                confidence="high", environment=classify_environment(rel),
            ))
            continue
        files_scanned += 1
        findings.extend(analyse_source(data, rel, facts=facts, options=options))

    if options.categories is None or "architecture" in options.categories or (options.select_rules and any(r.startswith("ARCH_") for r in options.select_rules)):
        project_findings = [replace(f, evidence_state=evidence_state_for(f)) for f in architecture.run_project(facts) if _rule_enabled(f.rule, options)]
        findings.extend(project_findings)

    if options.only_files is not None:
        wanted = {p.replace(os.sep, "/") for p in options.only_files}
        findings = [f for f in findings if f.file in wanted]
        files_reported = sum(1 for rel, _ in files if rel in wanted)
    else:
        files_reported = files_scanned

    findings.sort(key=Finding.sort_key)
    skipped.sort(key=lambda item: (item["file"], item["reason"]))
    # v1.8: classify, then attach the structured (never applied) remediation proposal of the selected rules
    findings = [replace(f, evidence_state=f.evidence_state or evidence_state_for(f)) for f in findings]
    findings = [replace(f, remediation=build_remediation(f)) if f.rule in REMEDIATIONS else f for f in findings]

    by_rule: Dict[str, int] = {}
    by_severity: Dict[str, int] = {level: 0 for level in SEVERITY_ORDER}
    by_environment: Dict[str, int] = {ENV_APPLICATION: 0, ENV_TEST: 0, ENV_MIGRATION: 0}
    by_category: Dict[str, int] = {c: 0 for c in CATEGORIES}
    by_verification: Dict[str, int] = {}
    by_evidence_state: Dict[str, int] = {state: 0 for state in EVIDENCE_STATES}
    suppressed = 0
    for finding in findings:
        if not finding.evidence_state:
            finding = replace(finding, evidence_state=evidence_state_for(finding))
        by_evidence_state[finding.evidence_state] += 1
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
        by_severity[finding.severity] += 1
        by_environment[finding.environment] = by_environment.get(finding.environment, 0) + 1
        by_category[finding.category] = by_category.get(finding.category, 0) + 1
        by_verification[finding.verification] = by_verification.get(finding.verification, 0) + 1
        suppressed += int(finding.suppressed)

    partial = bool(skipped) or "PY_SYNTAX" in by_rule or "ANALYSIS_PARTIAL" in by_rule or bool(facts.interprocedural.get("truncated"))
    report: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION, "python": PYTHON_VERSION},
        "project_root": root_abs,
        "files_scanned": files_scanned,
        "files_skipped": skipped,
        "tests_excluded": sum(tests_excluded),
        "files_reported": files_reported,
        "summary": {
            "findings_total": len(findings),
            "by_rule": dict(sorted(by_rule.items())),
            "by_severity": by_severity,
            "by_environment": by_environment,
            "by_category": by_category,
            "by_verification": dict(sorted(by_verification.items())),
            "by_evidence_state": by_evidence_state,  # schema 1.5
            "suppressed": suppressed,
            "partial_analysis": partial,
        },
        "findings": [finding.to_dict() for finding in findings],
        "config": options.effective(),
        "facts": facts.summary(),
        "interprocedural": facts.interprocedural,
        "contracts": _contracts_block(facts),  # schema 1.5
        "comparison": None,
        "disclaimer": DISCLAIMER,
    }
    if options.baseline is not None:
        report["comparison"] = compare_with_baseline(report, options.baseline, options.baseline_name)
    return report

"""fastapi_audit: read-only static auditor for FastAPI / SQLAlchemy / Alembic projects.

Standard library only. Public API re-exported here; ``audit_fastapi.py`` next to
this package is the command-line entry point.
"""

from __future__ import annotations

from .cli import build_parser, main
from .discovery import (
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_MAX_FILE_BYTES,
    TEST_DIR_NAMES,
    TEST_FILE_RE,
    classify_environment,
    iter_python_files,
)
from .engine import AuditOptions, analyse_source, audit_project, audit_source
from .facts import CONFIG_FILENAME, ProjectFacts, collect_facts
from .callgraph import DEFAULT_MAX_CALL_DEPTH, CallGraph, CallGraphStats
from .kinds import function_kinds, function_kinds_ex
from .model import (
    CATEGORIES,
    CONFIDENCE_DOWNGRADE,
    CONFIDENCE_ORDER,
    DISCLAIMER,
    ENV_APPLICATION,
    ENV_MIGRATION,
    ENV_TEST,
    EVIDENCE_INFERRED,
    EVIDENCE_UNKNOWN,
    EVIDENCE_VERIFIED,
    PYTHON_VERSION,
    RULE_CATEGORIES,
    RULES,
    SCHEMA_VERSION,
    SEVERITY_ORDER,
    TOOL_NAME,
    TOOL_VERSION,
    VERIFICATION_NOT_STATIC,
    VERIFICATION_RUNTIME,
    VERIFICATION_STATIC,
    Finding,
)
from .report import compare_with_baseline, fingerprint_of, list_rules_text, render_markdown, render_text, to_json
from .suppressions import MARKER_RE, NOQA_CODES_BY_RULE, NOQA_RE, line_comments, suppression_for

__all__ = [
    "AuditOptions",
    "CATEGORIES",
    "CONFIDENCE_DOWNGRADE",
    "CONFIDENCE_ORDER",
    "CONFIG_FILENAME",
    "DEFAULT_EXCLUDED_DIRS",
    "DEFAULT_MAX_FILE_BYTES",
    "DISCLAIMER",
    "ENV_APPLICATION",
    "ENV_MIGRATION",
    "ENV_TEST",
    "Finding",
    "MARKER_RE",
    "NOQA_CODES_BY_RULE",
    "NOQA_RE",
    "PYTHON_VERSION",
    "ProjectFacts",
    "RULES",
    "RULE_CATEGORIES",
    "SCHEMA_VERSION",
    "SEVERITY_ORDER",
    "TEST_DIR_NAMES",
    "TEST_FILE_RE",
    "TOOL_NAME",
    "TOOL_VERSION",
    "VERIFICATION_NOT_STATIC",
    "VERIFICATION_RUNTIME",
    "VERIFICATION_STATIC",
    "analyse_source",
    "audit_project",
    "audit_source",
    "build_parser",
    "classify_environment",
    "collect_facts",
    "compare_with_baseline",
    "fingerprint_of",
    "iter_python_files",
    "line_comments",
    "list_rules_text",
    "main",
    "render_markdown",
    "render_text",
    "suppression_for",
    "to_json",
]

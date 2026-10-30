"""FastAPI Architect design engine (v2.0): declarative architecture contract, reasoned recommendation, safe
project generation and verification that reuses the audit engine and the v1.9 trust model.

Public surface:

* ``spec``: ``SPEC_SCHEMA``, ``load_spec``, ``validate_spec``, ``SpecError``, ``Spec``;
* ``advisor``: ``recommend`` -> ``Recommendation``;
* ``blueprint``: ``build`` -> ``Blueprint`` (``{relative path: content}``, pure);
* ``generator``: ``generate`` (idempotent, never overwrites without ``overwrite``), ``verify_manifest``, ``GenerationError``;
* ``verify``: ``audit_generated`` (static), ``run_generated_tests`` (only through an executor of the trust model);
* ``pipeline``: ``run_design(spec_or_path, DesignOptions)`` -> ``architecture_report/1.0`` dict;
* ``report``: ``to_json``, ``render_text``, ``render_markdown``.
"""

from .advisor import Recommendation, recommend
from .blueprint import Blueprint, build
from .generator import MANIFEST, MANIFEST_SCHEMA, GenerationError, GenerationResult, generate, verify_manifest
from .pipeline import DesignError, DesignOptions, generation_refusal, run_design
from .report import REPORT_SCHEMA, render_markdown, render_text, to_json
from .spec import SPEC_SCHEMA, Spec, SpecError, load_spec, validate_spec
from .verify import audit_generated, run_generated_tests

__all__ = [
    "Blueprint", "DesignError", "DesignOptions", "GenerationError", "GenerationResult", "MANIFEST", "MANIFEST_SCHEMA", "REPORT_SCHEMA",
    "Recommendation", "SPEC_SCHEMA", "Spec", "SpecError", "audit_generated", "build", "generate", "generation_refusal", "load_spec",
    "recommend", "render_markdown", "render_text", "run_design", "run_generated_tests", "to_json", "validate_spec", "verify_manifest",
]

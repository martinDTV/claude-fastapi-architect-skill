"""FastAPI Architect design engine (v2.0): declarative architecture contract, reasoned recommendation, safe
project generation and verification that reuses the audit engine and the v1.9 trust model.

Public surface:

* ``spec``: ``SPEC_SCHEMA``, ``load_spec``, ``validate_spec``, ``SpecError``, ``Spec``;
* ``advisor``: ``recommend`` -> ``Recommendation``;
* ``blueprint``: ``build`` -> ``Blueprint`` (``{relative path: content}``, pure);
* ``generator``: ``generate`` (idempotent, never overwrites without ``overwrite``), ``verify_manifest``, ``GenerationError``;
* ``verify``: ``audit_generated`` (static), ``run_generated_tests`` (only through an executor of the trust model);
* ``pipeline``: ``run_design(spec_or_path, DesignOptions)`` -> ``architecture_report/1.0`` dict;
* ``report``: ``to_json``, ``render_text``, ``render_markdown``;
* ``discovery``: ``discover`` -> ``ProjectModel`` (static model of an existing project, increment 3);
* ``evolution``: ``load_evolution``, ``plan_evolution``, ``run_evolution`` -> ``evolution_report/1.0`` (increment 3).
"""

from .advisor import Recommendation, recommend
from .blueprint import Blueprint, build
from .generator import MANIFEST, MANIFEST_SCHEMA, GenerationError, GenerationResult, generate, verify_manifest
from .pipeline import DesignError, DesignOptions, generation_refusal, run_design, security_guarantees
from .report import REPORT_SCHEMA, render_markdown, render_text, to_json
from .spec import SPEC_SCHEMA, SPEC_SCHEMAS, Spec, SpecError, load_spec, validate_spec
from .discovery import ProjectModel, discover
from .evolution import EVOLUTION_SCHEMA, EvolutionContract, EvolutionError, EvolutionPlan, EvolveOptions, load_evolution, plan_evolution, run_evolution
from .verify import audit_generated, run_generated_tests

__all__ = [
    "EVOLUTION_SCHEMA", "EvolutionContract", "EvolutionError", "EvolutionPlan", "EvolveOptions", "ProjectModel", "discover", "load_evolution", "plan_evolution", "run_evolution",
    "Blueprint", "DesignError", "DesignOptions", "GenerationError", "GenerationResult", "MANIFEST", "MANIFEST_SCHEMA", "REPORT_SCHEMA",
    "Recommendation", "SPEC_SCHEMA", "SPEC_SCHEMAS", "Spec", "SpecError", "audit_generated", "build", "generate", "generation_refusal", "load_spec",
    "recommend", "render_markdown", "render_text", "run_design", "run_generated_tests", "security_guarantees", "to_json", "validate_spec", "verify_manifest",
]

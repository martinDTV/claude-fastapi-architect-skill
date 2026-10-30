"""Verified remediation (v1.9): states with evidence, structured patches, trust-scoped execution, per-case checks.

Nothing in this package writes to the audited project. Candidate patches are generated as unified diffs and
applied only inside isolated copies (one per finding); commands run there only through an ``Executor`` that
accepted an explicit ``Authorization`` (execution, trust profile, scope). The only executor shipped is the
local process executor (process hygiene, not a sandbox); untrusted projects fail closed. A case reaches
``verified`` only when the finding is gone (not suppressed), nothing new appeared, test files are untouched
and its linked reproducer failed before and passed after the patch.
"""

from .executors import Authorization, Executor, LocalProcessExecutor, select_executor
from .model import STATES, RemediationCase, RemediationError
from .patches import RECIPE_META, RECIPES, recipe_for
from .pipeline import RunOptions, run_remediation

__all__ = [
    "Authorization", "Executor", "LocalProcessExecutor", "RECIPE_META", "RECIPES", "RemediationCase", "RemediationError",
    "RunOptions", "STATES", "recipe_for", "run_remediation", "select_executor",
]

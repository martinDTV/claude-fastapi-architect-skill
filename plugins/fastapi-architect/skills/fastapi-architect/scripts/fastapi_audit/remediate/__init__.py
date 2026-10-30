"""Verified remediation (v1.9): states, structured patches, isolated execution, before/after checks.

Nothing in this package writes to the audited project. Patches are generated as unified diffs and applied
only inside an isolated copy (``isolation.Workspace``); commands run there only when the caller passes an
explicit authorization, with a sanitised environment, a fresh HOME, a timeout and resource limits. A case
reaches ``verified`` only when, after the patch, the finding is no longer detected, no new finding appeared
and the tests executed in the copy passed; without executed tests the best state is ``audit_clean``.
"""

from .model import STATES, RemediationCase, RemediationError
from .patches import RECIPES, recipe_for
from .pipeline import RunOptions, run_remediation

__all__ = ["STATES", "RECIPES", "RemediationCase", "RemediationError", "RunOptions", "recipe_for", "run_remediation"]

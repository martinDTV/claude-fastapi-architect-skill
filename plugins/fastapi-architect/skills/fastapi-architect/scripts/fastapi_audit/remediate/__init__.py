"""Verified remediation (v1.9): states with evidence, structured patches, trust-scoped execution, per-case checks.

Nothing in this package writes to the audited project. Candidate patches are generated as unified diffs and
applied only inside isolated copies (one per finding); commands run there only through an ``Executor`` that
accepted an explicit ``Authorization`` (execution, trust profile, scope). Two executors ship: the local process
executor (process hygiene, not a sandbox; fixture and trusted profiles) and, since v2.1, the container executor
(an OCI container with verified restrictions; the only one that accepts untrusted projects). There is no fallback
between them. A case reaches
``verified`` only when the finding is gone (not suppressed), nothing new appeared, test files are untouched
and its linked reproducer failed before and passed after the patch.
"""

from .containers import ContainerExecutor, DockerCliRuntime, ImagePolicy, SandboxPolicy, reap_leftovers
from .executors import REGISTRY, Authorization, Executor, LocalProcessExecutor, select_executor
from .model import STATES, RemediationCase, RemediationError
from .patches import RECIPE_META, RECIPES, recipe_for
from .pipeline import RunOptions, run_remediation

REGISTRY.setdefault(ContainerExecutor.name, ContainerExecutor())  # default policies; the CLIs replace it with a configured one

__all__ = [
    "Authorization", "ContainerExecutor", "DockerCliRuntime", "Executor", "ImagePolicy", "LocalProcessExecutor", "RECIPE_META", "RECIPES",
    "REGISTRY", "RemediationCase", "RemediationError", "RunOptions", "STATES", "SandboxPolicy", "reap_leftovers", "recipe_for",
    "run_remediation", "select_executor",
]

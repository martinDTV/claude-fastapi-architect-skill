"""Rule modules. Each exposes ``CATEGORY`` and ``run(ctx) -> List[Finding]``;
architecture additionally exposes ``run_project(facts)``."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..facts import ProjectFacts
from ..resolver import FunctionInfo, Scope


@dataclass
class FileContext:
    """Everything a file-level rule may look at. Built once per file by the engine."""

    relpath: str
    tree: ast.Module
    source: bytes
    environment: str
    module_scope: Scope
    functions: List[FunctionInfo]
    fastapi_imported: bool
    sqlalchemy_imported: bool
    alembic_imported: bool
    facts: Optional[ProjectFacts]
    local_factories: Dict[str, str] = field(default_factory=dict)  # module-level SessionLocal -> kind
    awaited: Set[int] = field(default_factory=set)  # ids of awaited Call nodes
    returned: Set[int] = field(default_factory=set)  # ids of Call nodes directly returned
    contracts: List[object] = field(default_factory=list)  # v1.7: RouteContract per route of this file (filled by rules.security)

    def resolve(self, parts) -> Optional[str]:
        from ..resolver import resolve_in_scopes

        return resolve_in_scopes([self.module_scope], parts)

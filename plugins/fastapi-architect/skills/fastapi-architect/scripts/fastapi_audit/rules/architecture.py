"""Architecture boundaries from the project import graph.

Layers and business modules are inferred from paths (or taken from
``.audit-fastapi.json``) and the inference is reported. When import-linter
contracts exist they are the source of truth and these findings are reported
with lower confidence.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from ..discovery import classify_environment
from ..facts import ProjectFacts
from ..model import CONFIDENCE_DOWNGRADE, ENV_APPLICATION, Finding, new_finding
from . import FileContext

CATEGORY = "architecture"

WEB_LIBS = frozenset({"fastapi", "starlette"})
INFRA_LIBS = frozenset(
    {
        "sqlalchemy", "sqlmodel", "alembic", "psycopg", "psycopg2", "asyncpg", "aiomysql", "pymysql",
        "httpx", "requests", "aiohttp", "boto3", "botocore", "redis", "aioredis", "celery", "kafka",
        "aiokafka", "pika", "aio_pika", "pymongo", "motor", "elasticsearch", "fastapi", "starlette",
    }
)


def run(ctx: FileContext) -> List[Finding]:  # architecture rules are project-level
    return []


def _layer_note(facts: ProjectFacts) -> str:
    return "layer from config" if facts.layer_source == "config" else "layer inferred from path"


def _base_confidence(facts: ProjectFacts) -> str:
    return "high" if facts.layer_source == "config" else "medium"


def _tarjan(graph: Dict[str, Set[str]]) -> List[List[str]]:
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    sccs: List[List[str]] = []
    counter = [0]

    def strongconnect(v: str) -> None:
        # iterative to survive large graphs
        work = [(v, iter(sorted(graph.get(v, ()))))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    sccs.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)
    return sorted(sccs)


def run_project(facts: Optional[ProjectFacts]) -> List[Finding]:
    out: List[Finding] = []
    if facts is None:
        return out
    linter = facts.import_linter
    downgrade = linter is not None
    suffix = ""
    if linter:
        names = ", ".join(linter.get("contracts") or []) or "no named contracts parsed"
        suffix = f" import-linter is configured in {linter['file']} ({names}); run lint-imports and treat its result as authoritative."
        out.append(
            new_finding(
                "ARCH_IMPORT_LINTER_PRESENT",
                linter["file"],
                linter.get("line", 1),
                0,
                f"import-linter configuration found ({names}). Boundary findings below are heuristic and reported with reduced confidence; the contracts are the source of truth.",
                confidence="high",
                symbol="import-linter",
            )
        )

    def conf(level: str) -> str:
        return CONFIDENCE_DOWNGRADE[level] if downgrade else level

    base = _base_confidence(facts)
    note = _layer_note(facts)
    graph: Dict[str, Set[str]] = {rel: set() for rel in facts.files}
    first_edge_line: Dict[Tuple[str, str], int] = {}
    reported: Set[Tuple[str, str, str]] = set()

    for rel in sorted(facts.files):
        if classify_environment(rel) != ENV_APPLICATION:
            continue  # tests and migrations may import anything; they are not architecture
        layer = facts.layer_of(rel)
        module = facts.module_of_file(rel)
        for dotted, line, module_level in facts.imports.get(rel, []):
            top = dotted.split(".")[0]
            if layer in {"service", "repository"} and top in WEB_LIBS and ("web", rel, top) not in reported:
                reported.add(("web", rel, top))
                out.append(new_finding("ARCH_SERVICE_IMPORTS_FASTAPI", rel, line, 0,
                    f"{layer} module imports '{dotted}' ({note}). Business and persistence code should not depend on the web framework; raise domain exceptions and map them to HTTP in the API layer.{suffix}",
                    confidence=conf(base if layer == "service" else "medium"), symbol=dotted))
            if layer == "domain" and top in INFRA_LIBS and ("infra", rel, top) not in reported:
                reported.add(("infra", rel, top))
                out.append(new_finding("ARCH_DOMAIN_IMPORTS_INFRA", rel, line, 0,
                    f"domain module imports infrastructure library '{dotted}' ({note}). Keep the domain pure and depend on a port (Protocol) implemented by an adapter.{suffix}",
                    confidence=conf(base), symbol=dotted))
            target = facts.resolve_import(dotted)
            if not target or target == rel:
                continue
            if module_level:
                graph[rel].add(target)
                first_edge_line.setdefault((rel, target), line)
            target_layer = facts.layer_of(target)
            target_module = facts.module_of_file(target)
            if layer == "repository" and target_layer == "service" and ("repo", rel, target) not in reported:
                reported.add(("repo", rel, target))
                out.append(new_finding("ARCH_REPOSITORY_IMPORTS_SERVICE", rel, line, 0,
                    f"repository module imports service module '{target}' ({note}); the dependency direction router -> service -> repository is inverted.{suffix}",
                    confidence=conf(base), symbol=target))
            if module and target_module and target_module != module:
                prefix = f"{facts.modules_root}/{target_module}/"
                rest = target[len(prefix):] if target.startswith(prefix) else target
                is_public = rest in {"public.py", "__init__.py"} or rest.startswith("public/")
                if not is_public and ("xmod", rel, target_module) not in reported:
                    reported.add(("xmod", rel, target_module))
                    root_note = "modules root from config" if facts.modules_root_source == "config" else f"modules root inferred: {facts.modules_root}"
                    out.append(new_finding("ARCH_CROSS_MODULE_INTERNAL_IMPORT", rel, line, 0,
                        f"module '{module}' imports '{target}' from module '{target_module}' ({root_note}). Cross-module access should go through '{target_module}/public.py'; importing models, repositories or services directly couples the modules and blocks extraction.{suffix}",
                        confidence=conf("high" if facts.modules_root_source == "config" else "medium"), symbol=target))
            if layer == "core" and target_module and module is None and ("core", rel, target_module) not in reported:
                reported.add(("core", rel, target_module))
                out.append(new_finding("ARCH_CORE_IMPORTS_MODULE", rel, line, 0,
                    f"core/shared module imports business module '{target_module}' ('{target}', {note}). Infrastructure must not know about business modules; move the logic into the module or invert the dependency.{suffix}",
                    confidence=conf(base), symbol=target))

    for cycle in _tarjan(graph):
        anchor = cycle[0]
        line = 1
        for member in cycle[1:]:
            if (anchor, member) in first_edge_line:
                line = first_edge_line[(anchor, member)]
                break
        shown = ", ".join(cycle[:8]) + (f" (+{len(cycle) - 8} more)" if len(cycle) > 8 else "")
        out.append(new_finding("ARCH_IMPORT_CYCLE", anchor, line, 0,
            f"Module-level import cycle between {len(cycle)} files: {shown}. Import order becomes fragile and the files cannot be tested or extracted independently; break the cycle with a public facade, a shared lower-level module or a function-local import documented as such.{suffix}",
            confidence=conf("high"), symbol=f"cycle:{len(cycle)}"))
    return out

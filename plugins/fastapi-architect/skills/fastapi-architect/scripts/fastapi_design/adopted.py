"""Advanced adoption of hand-written projects (v2.2, increment 4): new modules at a declared mount point, bounded
endpoint extensions and, when the project's Alembic environment is declared and verified, additive persistence.

Adoption 1.0 lets the tool transform the files of the modules an operator adopted. Adoption 1.1 adds, still by
declaration and never by inference, what the tool may *create*:

* ``package``: the target package (must be the one the discovery found);
* ``mount``: the single mount point for new routers (file, enclosing function or module level, the application
  variable, the call after whose last occurrence a new ``include_router`` goes, the base prefix); it is resolved
  against the AST and refused when it is not unique, when the application is not bound in that scope, when routers
  are registered inside loops or conditionals (dynamic registration) or when no existing registration anchors it;
* ``new_modules``: the layout and conventions of generated modules (one package per module under ``package``,
  the router variable's name, absolute imports, what the generated tests import);
* ``dependencies.allowed``: the only callables a generated router or an added handler may depend on, each
  resolved to one top-level function of a project file;
* ``operations``: what evolutions may do (``add_module``, ``mount_router``, ``add_endpoint``, ``add_field``,
  ``add_persistence``);
* ``unmanaged``: paths the tool must never write even when something else would allow it;
* ``alembic``: the project's own Alembic environment (ini, script location, metadata and session dependency), with
  which a generated module may get a SQLAlchemy model and one additive revision after the current head.

Nothing in an adopted project becomes "managed" by this: the files the tool creates are recorded in the adoption
baseline with their digests (``generated_modules``, ``migrations``), the mount file is recorded with its digest and
receives exactly one import and one statement per module through the span-exact primitives, and every other file
stays protected. The generated module is self-contained (its own schemas, repository, service-less router, tests);
it does not import anything the generator would have produced in a generated project.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .blueprint._base import SA_TYPES
from .blueprint._helpers import _annotation, _py_default, _sql_default
from .blueprint.modules import _schemas
from .discovery import ProjectModel
from .spans import find_function, insert_statement
from .spec import IDENT_RE, OPERATIONS, FieldSpec, ModuleSpec, Spec, validate_spec
from .transforms import StructuralEdit, TransformError, _diff, _last_import_end, _parse, _verify_additive, compose, module_names

ADOPTION_SCHEMA_11 = "fastapi-architect/adoption/1.1"
ADOPTION_OPERATIONS = ("add_module", "mount_router", "add_endpoint", "add_field", "add_persistence")
NEW_MODULE_LAYOUTS = ("package",)
ROUTER_VARIABLE_CONVENTIONS = ("router", "{module}_router")
IMPORT_STYLES = ("absolute",)
PERSISTENCE_KINDS = ("in_memory", "postgresql")
SYMBOL_RE = re.compile(r"^([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*)$")
SAMPLE_VALUES = {"str": "sample", "int": 1, "float": 1.5, "bool": True, "uuid": "11111111-1111-4111-8111-111111111111", "datetime": "2026-01-01T10:00:00Z", "date": "2026-01-01", "decimal": "9.99"}
SA_IMPORTS = {"str": "String", "int": "Integer", "float": "Float", "bool": "Boolean", "uuid": "Uuid", "datetime": "DateTime", "date": "Date", "decimal": "Numeric"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# the 1.1 extensions of the adoption contract
# --------------------------------------------------------------------------- #


def _symbol(value: Any, where: str, errors: List[str]) -> Optional[Tuple[str, str]]:
    if not isinstance(value, str) or not SYMBOL_RE.match(value):
        errors.append(f"{where}: expected 'dotted.module:name' (a project module and one top-level name)")
        return None
    module, name = value.split(":")
    return module, name


def validate_adoption_extensions(document: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The 1.1 keys, validated by shape; the project-side checks (paths exist, the mount resolves, the symbols
    resolve) happen in ``adopt`` and in the plan, against the files."""
    errors: List[str] = []
    out: Dict[str, Any] = {}
    package = document.get("package")
    if package is not None:
        if not isinstance(package, str) or not IDENT_RE.match(package):
            errors.append("adoption.package: expected the application package name (a lowercase identifier)")
        else:
            out["package"] = package
    unmanaged = document.get("unmanaged", [])
    if not isinstance(unmanaged, list) or any(not isinstance(g, str) or not g.strip() or g.startswith(("/", "\\")) or ".." in g.split("/") for g in unmanaged):
        errors.append("adoption.unmanaged: expected a list of relative path globs inside the project")
        unmanaged = []
    out["unmanaged"] = list(unmanaged)
    mount = document.get("mount")
    if mount is not None:
        if not isinstance(mount, dict):
            errors.append("adoption.mount: expected an object with file, app, function, after_call, prefix")
        else:
            for key in mount:
                if key not in ("file", "function", "app", "after_call", "prefix") and not str(key).startswith("x-"):
                    errors.append(f"adoption.mount.{key}: unknown property")
            file = mount.get("file")
            if not isinstance(file, str) or not file.endswith(".py") or file.startswith(("/", "\\")) or ".." in file.split("/"):
                errors.append("adoption.mount.file: expected the relative path of the python file that registers the routers")
            function = mount.get("function")
            if function is not None and (not isinstance(function, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$", function)):
                errors.append("adoption.mount.function: expected a function name (or Class.method), or null for module level")
            app = mount.get("app", "app")
            if not isinstance(app, str) or not IDENT_RE.match(app):
                errors.append("adoption.mount.app: expected the name of the FastAPI application variable in that scope")
            after_call = mount.get("after_call", f"{app}.include_router" if isinstance(app, str) else "app.include_router")
            if not isinstance(after_call, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\.include_router$", after_call):
                errors.append("adoption.mount.after_call: expected '<app>.include_router' (new routers go after its last call in the scope)")
            prefix = mount.get("prefix", "")
            if not isinstance(prefix, str) or (prefix and (not prefix.startswith("/") or prefix.endswith("/"))):
                errors.append("adoption.mount.prefix: expected '' or a path starting with '/' and not ending with one")
            if not errors or all("adoption.mount" not in e for e in errors):
                out["mount"] = {"file": file, "function": function, "app": app, "after_call": after_call, "prefix": prefix}
    new_modules = document.get("new_modules")
    if new_modules is not None:
        if not isinstance(new_modules, dict):
            errors.append("adoption.new_modules: expected an object with layout, router_variable, import_style, test_app")
        else:
            for key in new_modules:
                if key not in ("layout", "router_variable", "import_style", "test_app") and not str(key).startswith("x-"):
                    errors.append(f"adoption.new_modules.{key}: unknown property")
            layout = new_modules.get("layout", "package")
            if layout not in NEW_MODULE_LAYOUTS:
                errors.append(f"adoption.new_modules.layout: only {', '.join(NEW_MODULE_LAYOUTS)} is implemented (one package per module under the application package); got {layout!r}")
            router_variable = new_modules.get("router_variable", "router")
            if router_variable not in ROUTER_VARIABLE_CONVENTIONS:
                errors.append(f"adoption.new_modules.router_variable: expected one of {', '.join(ROUTER_VARIABLE_CONVENTIONS)}")
            import_style = new_modules.get("import_style", "absolute")
            if import_style not in IMPORT_STYLES:
                errors.append("adoption.new_modules.import_style: only 'absolute' imports are generated (relative imports are not inserted into a project the tool does not own)")
            test_app = new_modules.get("test_app")
            if not isinstance(test_app, dict) or not isinstance(test_app.get("module"), str) or not isinstance(test_app.get("attribute"), str) or not IDENT_RE.match(test_app.get("attribute", "")):
                errors.append("adoption.new_modules.test_app: expected {module: 'dotted.module', attribute: 'app'} (what the generated tests import; a FastAPI instance or a zero-argument factory)")
            if all("adoption.new_modules" not in e for e in errors):
                out["new_modules"] = {"layout": layout, "router_variable": router_variable, "import_style": import_style, "test_app": {"module": test_app["module"], "attribute": test_app["attribute"], "factory": bool(test_app.get("factory", False))}}
    dependencies = document.get("dependencies")
    if dependencies is not None:
        if not isinstance(dependencies, dict) or not isinstance(dependencies.get("allowed"), list):
            errors.append("adoption.dependencies: expected {allowed: ['dotted.module:name', ...]}")
        else:
            allowed = []
            for i, item in enumerate(dependencies["allowed"]):
                resolved = _symbol(item, f"adoption.dependencies.allowed[{i}]", errors)
                if resolved:
                    allowed.append(item)
            out["dependencies"] = {"allowed": allowed}
    operations = document.get("operations")
    if operations is not None:
        if not isinstance(operations, list) or any(o not in ADOPTION_OPERATIONS for o in operations):
            errors.append(f"adoption.operations: expected a list of {', '.join(ADOPTION_OPERATIONS)}")
        else:
            out["operations"] = list(dict.fromkeys(operations))
    alembic = document.get("alembic")
    if alembic is not None:
        if not isinstance(alembic, dict):
            errors.append("adoption.alembic: expected an object with ini, script_location, metadata, session_dependency")
        else:
            for key in alembic:
                if key not in ("ini", "script_location", "metadata", "session_dependency", "async") and not str(key).startswith("x-"):
                    errors.append(f"adoption.alembic.{key}: unknown property")
            ini = alembic.get("ini", "alembic.ini")
            script = alembic.get("script_location")
            if not isinstance(ini, str) or not ini.endswith(".ini"):
                errors.append("adoption.alembic.ini: expected the relative path of the project's alembic.ini")
            if not isinstance(script, str) or not script.strip() or script.startswith(("/", "\\")) or ".." in script.split("/"):
                errors.append("adoption.alembic.script_location: expected the relative path of the project's Alembic script directory (the one env.py lives in)")
            metadata = _symbol(alembic.get("metadata"), "adoption.alembic.metadata", errors)
            session = _symbol(alembic.get("session_dependency"), "adoption.alembic.session_dependency", errors)
            is_async = alembic.get("async", True)
            if not isinstance(is_async, bool):
                errors.append("adoption.alembic.async: expected true or false")
            if metadata and session and all("adoption.alembic" not in e for e in errors):
                out["alembic"] = {"ini": ini, "script_location": script.rstrip("/"), "metadata": alembic["metadata"], "session_dependency": alembic["session_dependency"], "async": is_async}
    if document.get("new_modules") is not None and document.get("mount") is None:
        errors.append("adoption.mount: required with new_modules (a generated module needs one declared place to be registered)")
    if document.get("mount") is not None and document.get("new_modules") is None:
        errors.append("adoption.new_modules: required with mount (the conventions the generated modules follow)")
    if "add_persistence" in (document.get("operations") or []) and document.get("alembic") is None:
        errors.append("adoption.alembic: required when operations allow add_persistence (the project's own Alembic environment, declared by the operator)")
    if document.get("alembic") is not None and "add_persistence" not in (document.get("operations") or []):
        errors.append("adoption.operations: add_persistence is required when alembic is declared (declaring the environment without allowing its use is contradictory)")
    return out, errors


# --------------------------------------------------------------------------- #
# resolution against the project
# --------------------------------------------------------------------------- #


@dataclass
class MountPoint:
    file: str
    function: Optional[str]
    app: str
    after_call: str
    prefix: str
    existing: List[str] = field(default_factory=list)  # first arguments of the existing include_router calls in the scope
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"file": self.file, "function": self.function, "app": self.app, "after_call": self.after_call, "prefix": self.prefix, "existing_registrations": list(self.existing), **self.evidence}


def _include_calls(statements: Sequence[ast.stmt], app: str) -> Tuple[List[ast.Call], List[int]]:
    """Statement-level ``<app>.include_router(...)`` calls of a body, and the lines of registrations hidden inside
    compound statements (loops, conditionals, with, try): those are dynamic for the tool's purposes."""
    direct: List[ast.Call] = []
    nested: List[int] = []
    for stmt in statements:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "include_router" and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == app:
            direct.append(stmt.value)
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try)):
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
                    nested.append(node.lineno)
    return direct, nested


def _binds(statements: Sequence[ast.stmt], name: str) -> int:
    count = 0
    for stmt in statements:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            count += 1
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == name:
            count += 1
    return count


def _app_factories(tree: ast.Module) -> List[str]:
    """Top-level functions that build a FastAPI application (a call to ``FastAPI(...)`` in their body)."""
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == "FastAPI") or (isinstance(n.func, ast.Attribute) and n.func.attr == "FastAPI")) for n in ast.walk(node)):
                out.append(node.name)
    return out


def mount_candidates(project: Path, model: ProjectModel) -> List[Dict[str, Any]]:
    """Where the project registers routers today: every module scope or top-level function with statement-level
    ``<app>.include_router(...)`` calls. A proposal lists them; the operator declares one (nothing is inferred)."""
    out: List[Dict[str, Any]] = []
    for rel in sorted(model.digests):
        if not rel.endswith(".py") or rel.startswith("tests/") or "/tests/" in rel:
            continue
        try:
            tree = ast.parse((project / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError, OSError):
            continue
        scopes: List[Tuple[Optional[str], Sequence[ast.stmt]]] = [(None, tree.body)]
        scopes += [(node.name, node.body) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        factories = _app_factories(tree)
        for function, body in scopes:
            apps = sorted({stmt.value.func.value.id for stmt in body if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "include_router" and isinstance(stmt.value.func.value, ast.Name)})
            for app in apps:
                direct, nested = _include_calls(body, app)
                out.append({"file": rel, "function": function, "app": app, "after_call": f"{app}.include_router", "registrations": [ast.unparse(c.args[0]) if c.args else "?" for c in direct], "prefixes": [ast.unparse(k.value) for c in direct for k in c.keywords if k.arg == "prefix"], "dynamic_registrations": nested, "app_bound_here": _binds(body, app) > 0, "app_factories": factories})
    return out


def resolve_mount(project: Path, mount: Dict[str, Any]) -> Tuple[Optional[MountPoint], List[str]]:
    """The declared mount point against the file: unique scope, application bound there, at least one direct
    registration to anchor after, none hidden in compound statements."""
    rel, function, app, after_call, prefix = mount["file"], mount.get("function"), mount["app"], mount["after_call"], mount.get("prefix", "")
    path = project / rel
    errors: List[str] = []
    if path.is_symlink() or not path.is_file():
        return None, [f"adoption.mount.file: {rel} is missing or is a symlink"]
    try:
        tree = ast.parse(path.read_bytes(), filename=rel)
    except (SyntaxError, ValueError) as exc:
        return None, [f"adoption.mount.file: {rel} does not parse ({exc})"]
    factories = _app_factories(tree)
    if function is None:
        body: Sequence[ast.stmt] = tree.body
        scope = "module"
        if factories and not _binds(tree.body, app):
            return None, [f"adoption.mount: {rel} binds no {app!r} at module level but defines application factories ({', '.join(factories)}); name the factory in adoption.mount.function"]
    else:
        try:
            owner = find_function(tree, function, path=rel)
        except TransformError as exc:
            return None, [f"adoption.mount.function: {exc}"]
        body = list(getattr(owner, "body", []))
        scope = function
    if after_call != f"{app}.include_router":
        errors.append(f"adoption.mount.after_call: {after_call!r} does not belong to the application variable {app!r}")
    bound = _binds(body, app)
    if function is not None and bound == 0:
        params = [a.arg for a in getattr(getattr(owner, "args", None), "args", [])]
        if app not in params:
            errors.append(f"adoption.mount: {app!r} is not bound in {scope} of {rel} (no assignment, not a parameter)")
    elif function is None and bound == 0:
        errors.append(f"adoption.mount: {app!r} is not bound at module level of {rel}")
    elif bound > 1:
        errors.append(f"adoption.mount: {app!r} is bound {bound} times in {scope} of {rel}; ambiguous")
    direct, nested = _include_calls(body, app)
    if nested:
        errors.append(f"adoption.mount: {rel} registers routers inside loops, conditionals or context managers (lines {nested}); dynamic registration cannot be anchored and is refused")
    if not direct:
        errors.append(f"adoption.mount: no statement-level {app}.include_router(...) in {scope} of {rel}; a new router is only ever inserted after an existing registration")
    other_scopes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != function:
            calls, _n = _include_calls(node.body, app)
            if calls:
                other_scopes.append(node.name)
    if function is not None:
        module_calls, _n = _include_calls(tree.body, app)
        if module_calls:
            other_scopes.append("module")
    if errors:
        return None, errors
    existing = []
    for call in direct:
        first = call.args[0] if call.args else None
        existing.append(ast.unparse(first) if first is not None and hasattr(ast, "unparse") else "<router>")
    point = MountPoint(file=rel, function=function, app=app, after_call=after_call, prefix=prefix, existing=existing, evidence={"scope": scope, "app_factories": factories, "other_scopes_with_registrations": other_scopes, "anchor": f"after the last {after_call}(...) in {scope} (line {direct[-1].lineno})"})
    return point, []


def resolve_symbol(project: Path, package: str, source_root: str, symbol: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``dotted.module:name`` to one top-level definition of a project file: ``{file, kind, line, is_async}``."""
    match = SYMBOL_RE.match(symbol)
    if not match:
        return None, f"{symbol!r} is not 'dotted.module:name'"
    module, name = match.group(1), match.group(2)
    if module.split(".")[0] != package:
        return None, f"{symbol!r} is outside the application package {package!r}"
    base = project / source_root if source_root else project
    candidates = [base / (module.replace(".", "/") + ".py"), base / module.replace(".", "/") / "__init__.py"]
    file = next((c for c in candidates if c.is_file() and not c.is_symlink()), None)
    if file is None:
        return None, f"{symbol!r}: no file for module {module!r} in the project"
    try:
        tree = ast.parse(file.read_bytes(), filename=str(file))
    except (SyntaxError, ValueError) as exc:
        return None, f"{symbol!r}: {file.name} does not parse ({exc})"
    found = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
    found += [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    if len(found) != 1:
        return None, f"{symbol!r}: {name!r} is defined {len(found)} time(s) at the top level of {file.name}; it must be exactly once"
    node = found[0]
    kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else ("function" if isinstance(node, ast.FunctionDef) else ("class" if isinstance(node, ast.ClassDef) else "assignment"))
    rel = file.relative_to(project).as_posix()
    return {"symbol": symbol, "module": module, "name": name, "file": rel, "kind": kind, "line": node.lineno, "is_async": kind == "async_function"}, None


# --------------------------------------------------------------------------- #
# the Alembic environment of the adopted project
# --------------------------------------------------------------------------- #


def _env_metadata(env_source: str) -> Tuple[Optional[str], Optional[str]]:
    """``(imported_from, name)`` of what env.py assigns to ``target_metadata`` (``Name.metadata`` or ``Name``)."""
    try:
        tree = ast.parse(env_source)
    except SyntaxError:
        return None, None
    imports: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}:{alias.name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "target_metadata" for t in node.targets):
            value = node.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.attr == "metadata":
                return imports.get(value.value.id), value.value.id
            if isinstance(value, ast.Name):
                return imports.get(value.id), value.id
    return None, None


def existing_tables(project: Path, model: ProjectModel, alembic: Dict[str, Any]) -> Dict[str, str]:
    """``table -> where`` for every table the project already declares (``__tablename__`` in models, ``create_table``
    in its revisions); a new module's table must not collide with any of them."""
    tables: Dict[str, str] = {}
    for rel in model.digests:
        if not rel.endswith(".py") or rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse((project / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in node.targets) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                tables.setdefault(node.value.value, f"model in {rel}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_table" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                tables.setdefault(node.args[0].value, f"revision {rel}")
    return tables


def alembic_facts(project: Path, model: ProjectModel, alembic: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """What the declared environment really is: ini and script directory present, the discovery's view of the chain
    (one head, no unknown parents), env.py assigning the declared metadata, the declared Base and session dependency
    resolving to one definition each. Any doubt is an error; the plan refuses persistence on it."""
    errors: List[str] = []
    facts: Dict[str, Any] = {"ini": alembic["ini"], "script_location": alembic["script_location"]}
    if not (project / alembic["ini"]).is_file():
        errors.append(f"alembic: {alembic['ini']} not found")
    script = project / alembic["script_location"]
    env = script / "env.py"
    versions = script / "versions"
    if not script.is_dir() or not env.is_file():
        errors.append(f"alembic: {alembic['script_location']}/env.py not found")
    if not versions.is_dir():
        errors.append(f"alembic: {alembic['script_location']}/versions not found")
    discovered = model.alembic or {}
    if discovered.get("script_location") and discovered.get("script_location") != alembic["script_location"]:
        errors.append(f"alembic: alembic.ini points at {discovered.get('script_location')!r}, the contract declares {alembic['script_location']!r}")
    if not discovered:
        errors.append("alembic: the discovery found no alembic.ini at the project root; the environment is not the project's")
    heads = list(discovered.get("heads") or [])
    facts["heads"] = heads
    facts["revisions"] = [r["revision"] for r in discovered.get("revisions") or []]
    if len(heads) != 1 or discovered.get("unknown_parents"):
        errors.append(f"alembic: the chain is not linear (heads {heads}, unknown parents {discovered.get('unknown_parents')}); merge it before extending it")
    else:
        facts["head"] = heads[0]
    if env.is_file():
        imported_from, name = _env_metadata(env.read_text(encoding="utf-8", errors="replace"))
        facts["env_target_metadata"] = {"imported_from": imported_from, "name": name}
        declared_module, declared_name = alembic["metadata"].split(":")
        if imported_from != f"{declared_module}:{declared_name}":
            errors.append(f"alembic: env.py assigns target_metadata from {imported_from!r}, the contract declares {alembic['metadata']!r}; the metadata is not unambiguously the declared one")
    base_info, error = resolve_symbol(project, model.package or "", model.source_root, alembic["metadata"])
    if error:
        errors.append(f"alembic.metadata: {error}")
    else:
        facts["metadata"] = base_info
        if base_info["kind"] not in ("class", "assignment"):
            errors.append(f"alembic.metadata: {alembic['metadata']} is a {base_info['kind']}, not a declarative base")
    session_info, error = resolve_symbol(project, model.package or "", model.source_root, alembic["session_dependency"])
    if error:
        errors.append(f"alembic.session_dependency: {error}")
    else:
        facts["session_dependency"] = session_info
        if session_info["kind"] not in ("function", "async_function"):
            errors.append(f"alembic.session_dependency: {alembic['session_dependency']} is a {session_info['kind']}, not a dependency function")
    facts["tables"] = existing_tables(project, model, alembic) if not errors else {}
    facts["async"] = bool(alembic.get("async", True))
    return facts, errors


# --------------------------------------------------------------------------- #
# the generated module of an adopted project
# --------------------------------------------------------------------------- #


def module_fragment(package: str, module_doc: Dict[str, Any], persistence: str) -> Tuple[Optional[Spec], Optional[ModuleSpec], List[str]]:
    """Validate one ``add_modules`` entry through the design contract's own rules (a minimal spec around it)."""
    doc = {"schema": "fastapi-architect/spec/1.2", "name": package, "package": package, "description": f"generated module of the adopted project {package}", "scope": {"size": "small", "team_size": 1, "expected_rps": 5, "data_volume": "small"}, "functional": {"modules": [json.loads(json.dumps({k: v for k, v in module_doc.items() if k not in ("persistence", "prefix", "dependencies")}))]}, "persistence": {"kind": "postgresql" if persistence == "postgresql" else "memory"}, "security": {"authentication": "none"}}
    spec, errors = validate_spec(doc)
    if spec is None:
        return None, None, [e.replace("spec.functional.modules[0]", f"add_modules[{module_doc.get('name')}]") for e in errors]
    return spec, spec.modules[0], []


def _router_name(module: str, convention: str) -> str:
    return "router" if convention == "router" else f"{module}_router"


def _sample(f: FieldSpec) -> Any:
    if f.type == "str":
        value = "sample"
        return value[: f.max_length] if f.max_length else value
    if f.type in ("int", "float", "decimal") and f.min_value is not None and f.min_value > 0:
        return f.min_value if f.type != "decimal" else str(f.min_value)
    return SAMPLE_VALUES[f.type]


def _payload(entity) -> Dict[str, Any]:
    return {f.name: _sample(f) for f in entity.fields if f.required and not f.has_default and not f.references}


def _dependency_import(symbol: str) -> Tuple[str, str]:
    module, name = symbol.split(":")
    return f"from {module} import {name}", name


def render_module(package: str, module: ModuleSpec, spec: Spec, *, conventions: Dict[str, Any], persistence: str, prefix: str, dependencies: Sequence[str], alembic: Optional[Dict[str, Any]] = None, revision: Optional[str] = None, head: Optional[str] = None, source_root: str = "") -> Dict[str, str]:
    """Every file of a self-contained module: schemas (the generator's own template), an in-memory or SQLAlchemy
    repository, a router with the declared operations, a test module, and, with persistence, the model and one
    additive revision in the project's own Alembic directory."""
    pkg_dir = f"{source_root + '/' if source_root else ''}{package}"
    base = f"{package}.{module.name}"
    rel_dir = f"{pkg_dir}/{module.name}"
    router_var = _router_name(module.name, conventions.get("router_variable", "router"))
    files: Dict[str, str] = {}
    files[f"{rel_dir}/__init__.py"] = f'"""{module.name}: generated by FastAPI Architect inside an adopted project; a self-contained module."""\n'
    files[f"{rel_dir}/schemas.py"] = _schemas(spec, module)
    dep_imports = [_dependency_import(d) for d in dependencies]
    any_list = any("list" in set(e.operations or OPERATIONS) for e in module.entities)
    needs_annotated = any_list or persistence == "postgresql"
    router_lines: List[str] = [f'"""{module.name} routes (generated by FastAPI Architect inside an adopted project).', "", "Mounted by the project's own application file at the declared mount point; this module imports nothing the project", "did not declare. Existing handlers of the project are not touched by it.", '"""', "", *(["from typing import Annotated"] if needs_annotated else []), "from uuid import UUID", ""]
    fastapi_names = ["APIRouter", "HTTPException", "status"] + (["Query"] if any_list else [])
    if persistence == "postgresql" or dep_imports:
        fastapi_names.insert(1, "Depends")
    router_lines.append(f"from fastapi import {', '.join(sorted(fastapi_names))}")
    if persistence == "postgresql":
        router_lines.append("from sqlalchemy.ext.asyncio import AsyncSession")
    router_lines.append("")
    project_imports = [line for line, _name in dep_imports]
    if persistence == "postgresql" and alembic:
        session_module, session_name = alembic["session_dependency"].split(":")
        project_imports.append(f"from {session_module} import {session_name}")
    router_lines += sorted(set(project_imports))
    repo_names = ", ".join(f"{e.name}Repository" for e in module.entities)
    schema_names = _schema_names(module)
    router_lines.append(f"from {base}.repository import {repo_names}")
    router_lines.append(f"from {base}.schemas import {', '.join(schema_names)}")
    router_lines.append("")
    deps = f", dependencies=[{', '.join(f'Depends({name})' for _l, name in sorted(dep_imports))}]" if dep_imports else ""
    router_lines.append(f'{router_var} = APIRouter(tags=[{json.dumps(module.name)}]{deps})')
    if persistence != "postgresql":
        for e in module.entities:
            router_lines.append(f"_{e.plural} = {e.name}Repository()  # one in-memory store per process")
    for e in module.entities:
        ops = set(e.operations or OPERATIONS)
        E, x, plural = e.name, e.snake, e.plural
        collection = "" if len(module.entities) == 1 else f"/{plural}"
        item = f"{collection}/{{{x}_id}}"
        if persistence == "postgresql":
            session_params = [f"session: Annotated[AsyncSession, Depends({alembic['session_dependency'].split(':')[1]})]"] if alembic else []
            repo = f"{E}Repository(session)"
        else:
            session_params = []
            repo = f"_{plural}"
        aw = "await " if persistence == "postgresql" else ""
        not_found = f'        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="{E} not found")'
        if "create" in ops:
            router_lines += ["", "", f'@{router_var}.post({json.dumps(collection)}, response_model={E}Read, status_code=status.HTTP_201_CREATED)', *_signature(f"create_{x}", [f"data: {E}Create", *session_params], f"{E}Read"), f"    return {aw}{repo}.add(data)"]
        if "get" in ops:
            router_lines += ["", "", f'@{router_var}.get({json.dumps(item)}, response_model={E}Read)', *_signature(f"get_{x}", [f"{x}_id: UUID", *session_params], f"{E}Read"), f"    found = {aw}{repo}.get({x}_id)", "    if found is None:", not_found, "    return found"]
        if "list" in ops:
            router_lines += ["", "", f'@{router_var}.get({json.dumps(collection)}, response_model=list[{E}Read])', *_signature(f"list_{plural}", [*session_params, "limit: Annotated[int, Query(ge=1, le=200)] = 50", "offset: Annotated[int, Query(ge=0)] = 0"], f"list[{E}Read]"), f"    return {aw}{repo}.list(limit, offset)"]
        if "update" in ops:
            router_lines += ["", "", f'@{router_var}.patch({json.dumps(item)}, response_model={E}Read)', *_signature(f"update_{x}", [f"{x}_id: UUID", f"data: {E}Update", *session_params], f"{E}Read"), f"    updated = {aw}{repo}.update({x}_id, data)", "    if updated is None:", not_found, "    return updated"]
        if "delete" in ops:
            router_lines += ["", "", f'@{router_var}.delete({json.dumps(item)}, status_code=status.HTTP_204_NO_CONTENT)', *_signature(f"delete_{x}", [f"{x}_id: UUID", *session_params], "None"), f"    if not {aw}{repo}.delete({x}_id):", not_found]
    files[f"{rel_dir}/router.py"] = "\n".join(router_lines).rstrip("\n") + "\n"
    files[f"{rel_dir}/repository.py"] = _repository(base, module, persistence, alembic)
    if persistence == "postgresql" and alembic:
        files[f"{rel_dir}/model.py"] = _model(base, module, alembic)
        if revision and head and alembic.get("script_location"):
            files[f"{alembic['script_location']}/versions/{revision}_{module.name}.py"] = _revision(module, revision, head)
    files[f"tests/test_{module.name}.py"] = _tests(module, conventions, persistence, prefix, dependencies)
    return files


def _schema_names(module: ModuleSpec) -> List[str]:
    names = set()
    for e in module.entities:
        ops = set(e.operations or OPERATIONS)
        names.add(f"{e.name}Read")
        if "create" in ops:
            names.add(f"{e.name}Create")
        if "update" in ops:
            names.add(f"{e.name}Update")
    return sorted(names)


def _signature(name: str, params: List[str], returns: str) -> List[str]:
    """``async def name(params) -> returns:`` on one line when it fits in 120 columns, one parameter per line otherwise."""
    one = f"async def {name}({', '.join(params)}) -> {returns}:"
    if len(one) <= 120:
        return [one]
    return [f"async def {name}(", *[f"    {p}," for p in params], f") -> {returns}:"]


def _repository(base: str, module: ModuleSpec, persistence: str, alembic: Optional[Dict[str, Any]]) -> str:
    schema_names = _schema_names(module)
    if persistence != "postgresql":
        lines = ['"""In-memory repositories of the module: one dict per entity, keyed by id; the process restarts empty."""', "", "from datetime import UTC, datetime", "from uuid import UUID, uuid4", "", f"from {base}.schemas import {', '.join(schema_names)}", ""]
        for e in module.entities:
            E = e.name
            ops = set(e.operations or OPERATIONS)
            lines += ["", f"class {E}Repository:", "    def __init__(self) -> None:", f"        self._rows: dict[UUID, {E}Read] = {{}}"]
            if "create" in ops:
                lines += ["", f"    def add(self, data: {E}Create) -> {E}Read:", f"        row = {E}Read(id=uuid4(), created_at=datetime.now(UTC), **data.model_dump())", "        self._rows[row.id] = row", "        return row"]
            if "get" in ops:
                lines += ["", f"    def get(self, row_id: UUID) -> {E}Read | None:", "        return self._rows.get(row_id)"]
            if "list" in ops:
                lines += ["", f"    def list(self, limit: int = 50, offset: int = 0) -> list[{E}Read]:", "        return list(self._rows.values())[offset : offset + limit]"]
            if "update" in ops:
                lines += ["", f"    def update(self, row_id: UUID, data: {E}Update) -> {E}Read | None:", "        current = self._rows.get(row_id)", "        if current is None:", "            return None", "        updated = current.model_copy(update=data.model_dump(exclude_unset=True))", "        self._rows[row_id] = updated", "        return updated"]
            if "delete" in ops:
                lines += ["", "    def delete(self, row_id: UUID) -> bool:", "        return self._rows.pop(row_id, None) is not None"]
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"
    model_names = ", ".join(e.name for e in module.entities)
    needs_select = any("list" in set(e.operations or OPERATIONS) for e in module.entities)
    pg_schema_names = [n for n in schema_names if not n.endswith("Read")]
    lines = ['"""SQLAlchemy repositories of the module, on the session the project provides; every write commits."""', "", "from uuid import UUID", "", *(["from sqlalchemy import select"] if needs_select else []), "from sqlalchemy.ext.asyncio import AsyncSession", "", f"from {base}.model import {model_names}", *([f"from {base}.schemas import {', '.join(pg_schema_names)}"] if pg_schema_names else []), ""]
    for e in module.entities:
        E = e.name
        ops = set(e.operations or OPERATIONS)
        lines += ["", f"class {E}Repository:", "    def __init__(self, session: AsyncSession) -> None:", "        self._session = session"]
        if "create" in ops:
            lines += ["", f"    async def add(self, data: {E}Create) -> {E}:", f"        row = {E}(**data.model_dump())", "        self._session.add(row)", "        await self._session.commit()", "        await self._session.refresh(row)", "        return row"]
        if "get" in ops:
            lines += ["", f"    async def get(self, row_id: UUID) -> {E} | None:", f"        return await self._session.get({E}, row_id)"]
        if "list" in ops:
            lines += ["", f"    async def list(self, limit: int = 50, offset: int = 0) -> list[{E}]:", f"        statement = select({E}).order_by({E}.created_at, {E}.id).limit(limit).offset(offset)", "        result = await self._session.execute(statement)", "        return list(result.scalars().all())"]
        if "update" in ops:
            lines += ["", f"    async def update(self, row_id: UUID, data: {E}Update) -> {E} | None:", f"        row = await self._session.get({E}, row_id)", "        if row is None:", "            return None", "        for name, value in data.model_dump(exclude_unset=True).items():", "            setattr(row, name, value)", "        await self._session.commit()", "        await self._session.refresh(row)", "        return row"]
        if "delete" in ops:
            lines += ["", "    async def delete(self, row_id: UUID) -> bool:", f"        row = await self._session.get({E}, row_id)", "        if row is None:", "            return False", "        await self._session.delete(row)", "        await self._session.commit()", "        return True"]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _sa_column(f: FieldSpec) -> str:
    kind = SA_IMPORTS[f.type]
    arg = f"String({f.max_length or 255})" if f.type == "str" else ("Numeric(18, 2)" if f.type == "decimal" else ("DateTime(timezone=True)" if f.type == "datetime" else kind))
    extras = "" if f.required else ", nullable=True"
    if f.has_default:
        extras += f", default={_py_default(f)}, server_default=text(\"{_sql_default(f)}\")"
    return f"    {f.name}: Mapped[{_annotation(f)}] = mapped_column({arg}{extras})"


def _model(base: str, module: ModuleSpec, alembic: Dict[str, Any]) -> str:
    metadata_module, base_name = alembic["metadata"].split(":")
    types = sorted({SA_IMPORTS[f.type] for e in module.entities for f in e.fields} | {"DateTime", "Uuid"})
    needs_text = any(f.has_default for e in module.entities for f in e.fields)
    stdlib = ["from datetime import date, datetime" if any(f.type == "date" for e in module.entities for f in e.fields) else "from datetime import datetime"]
    if any(f.type == "decimal" for e in module.entities for f in e.fields):
        stdlib.append("from decimal import Decimal")
    stdlib.append("from uuid import UUID, uuid4")
    lines = ['"""SQLAlchemy models of the module, on the project\'s own declarative base (the one Alembic\'s env.py targets)."""', "", *stdlib, "", f"from sqlalchemy import {', '.join(types + (['func', 'text'] if needs_text else ['func']))}", "from sqlalchemy.orm import Mapped, mapped_column", "", f"from {metadata_module} import {base_name}", ""]
    for e in module.entities:
        lines += ["", f"class {e.name}({base_name}):", f'    __tablename__ = "{e.plural}"', "", "    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)"]
        lines += [_sa_column(f) for f in e.fields]
        lines.append("    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _column_sql(f: FieldSpec) -> str:
    kind = SA_TYPES[f.type]
    sa_type = f"sa.String(length={f.max_length or 255})" if f.type == "str" else ("sa.Numeric(precision=18, scale=2)" if f.type == "decimal" else ("sa.DateTime(timezone=True)" if f.type == "datetime" else f"sa.{kind}()"))
    default = f', server_default=sa.text("{_sql_default(f)}")' if f.has_default else ""
    return f'sa.Column("{f.name}", {sa_type}{default}, nullable={not f.required})'


def _revision(module: ModuleSpec, revision: str, head: str) -> str:
    lines = [f'"""Create the tables of the {module.name} module (generated by FastAPI Architect inside an adopted project).', "", "Additive: new tables only, after the current head of the project's own chain. Downgrade drops them, with their data", "(stated in the plan's risks). No existing revision is touched.", "", f"Revision ID: {revision}", f"Revises: {head}", '"""', "", "import sqlalchemy as sa", "from alembic import op", "", f'revision = "{revision}"', f'down_revision = "{head}"', "branch_labels = None", "depends_on = None", "", "", "def upgrade() -> None:"]
    for e in module.entities:
        lines += ["    op.create_table(", f'        "{e.plural}",', '        sa.Column("id", sa.Uuid(), nullable=False),']
        lines += [f"        {_column_sql(f)}," for f in e.fields]
        lines += ['        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),', f'        sa.PrimaryKeyConstraint("id", name="pk_{e.plural}"),', "    )"]
    lines += ["", "", "def downgrade() -> None:"]
    for e in reversed(module.entities):
        lines.append(f'    op.drop_table("{e.plural}")')
    return "\n".join(lines) + "\n"


def _tests(module: ModuleSpec, conventions: Dict[str, Any], persistence: str, prefix: str, dependencies: Sequence[str] = ()) -> str:
    """The module's tests against the project's own application. The paths come from the OpenAPI document (stable
    across FastAPI versions; ``app.routes`` stopped listing included routers). With PostgreSQL, or with router-level
    dependencies the test cannot satisfy, only the mount is asserted; otherwise an in-memory round trip."""
    test_app = conventions.get("test_app") or {"module": "app.main", "attribute": "app", "factory": False}
    app_expr = f"{test_app['attribute']}()" if test_app.get("factory") else test_app["attribute"]
    mounted_only = persistence == "postgresql" or bool(dependencies) or any("create" not in set(e.operations or OPERATIONS) for e in module.entities)
    lines = [f'"""Tests of the generated {module.name} module against the project\'s own application (mounted at {prefix})."""', "", *([] if mounted_only else ["from fastapi.testclient import TestClient", ""]), f"from {test_app['module']} import {test_app['attribute']}", "", f'PREFIX = "{prefix}"', ""]
    lines += ["", f"def test_{module.name}_routes_are_mounted():", f"    paths = set({app_expr}.openapi()[\"paths\"])"]
    for e in module.entities:
        collection = "" if len(module.entities) == 1 else f"/{e.plural}"
        lines.append(f'    assert f"{{PREFIX}}{collection}" in paths')
    if mounted_only:
        why = "the database-backed round trip belongs to the project's own test setup (its migrations and its database URL)" if persistence == "postgresql" else ("the router depends on " + ", ".join(dependencies) + "; satisfying them belongs to the project's own test setup" if dependencies else "not every entity exposes create; the round trip is not defined")
        lines += ["", "", f"# {why};", "# this module checks that the mount is in place without assuming either."]
        return "\n".join(lines) + "\n"
    for e in module.entities:
        ops = set(e.operations or OPERATIONS)
        collection = "" if len(module.entities) == 1 else f"/{e.plural}"
        payload = json.dumps(_payload(e))
        lines += ["", "", f"def test_{e.snake}_round_trip():", f"    client = TestClient({app_expr})", f'    created = client.post(f"{{PREFIX}}{collection}", json={payload})', "    assert created.status_code == 201, created.text", "    body = created.json()", '    assert "id" in body']
        if "get" in ops:
            lines.append(f'    assert client.get(f"{{PREFIX}}{collection}/{{body[\'id\']}}").json() == body')
            lines.append(f'    assert client.get(f"{{PREFIX}}{collection}/11111111-1111-4111-8111-111111111111").status_code == 404')
        if "list" in ops:
            lines.append(f'    assert body in client.get(f"{{PREFIX}}{collection}").json()')
        if "delete" in ops:
            lines.append(f'    assert client.delete(f"{{PREFIX}}{collection}/{{body[\'id\']}}").status_code == 204')
    return "\n".join(lines) + "\n"


def revision_id(package: str, module: str, head: str) -> str:
    return hashlib.sha1(f"adopted|{package}|{module}|{head}".encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# the mount edit and the extended handlers
# --------------------------------------------------------------------------- #


def insert_import_line(path: str, source: str, line: str) -> StructuralEdit:
    """One import line after the last head import (or after the docstring); a no-op when the exact import exists."""
    tree = _parse(source, path)
    wanted = ast.dump(ast.parse(line).body[0])
    if any(isinstance(n, (ast.Import, ast.ImportFrom)) and ast.dump(n) == wanted for n in tree.body):
        return StructuralEdit(path=path, source=source, diff="", inserted=[], evidence={"import_already_present": True})
    lines = source.splitlines(keepends=True)
    at = _last_import_end(tree)
    text = line.rstrip("\n") + "\n"
    if at == 0 and tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
        at = getattr(tree.body[0], "end_lineno", tree.body[0].lineno)
        text = "\n" + text
    new_source = "".join(lines[:at]) + text + "".join(lines[at:])
    _, evidence = _verify_additive(path, tree, new_source, allowed_new=1)
    return StructuralEdit(path=path, source=new_source, diff=_diff(path, source, new_source), inserted=[line.strip()], evidence=evidence)


def mount_edit(point: MountPoint, source: str, *, import_line: str, statement: str) -> StructuralEdit:
    """The two span-exact edits of the mount file: the router import, then ``<app>.include_router(...)`` after the
    last existing registration in the declared scope. Comments, decorators and every other byte stay."""
    return compose(point.file, source, [lambda p, s: insert_import_line(p, s, import_line), lambda p, s: insert_statement(p, s, statement, function=point.function, after_call=point.after_call)])


def extended_endpoint_code(router: str, ep: Dict[str, Any], *, delegate: Optional[Dict[str, Any]] = None, dependency_names: Sequence[str] = (), response_model: Optional[str] = None) -> str:
    """A handler with typed path and query parameters, allowed dependencies, and either a literal response or a
    delegation to one resolved project callable. No free Python travels from the contract to the file."""
    params: List[str] = []
    for p in ep.get("parameters", []):
        annotation = {"str": "str", "int": "int", "float": "float", "bool": "bool"}[p["type"]]
        if p["in"] == "path":
            params.append(f"{p['name']}: {annotation}")
        elif p.get("required", False):
            params.append(f"{p['name']}: Annotated[{annotation}, Query()]")
        elif "default" in p:
            params.append(f"{p['name']}: Annotated[{annotation}, Query()] = {p['default']!r}")
        else:
            params.append(f"{p['name']}: Annotated[{annotation} | None, Query()] = None")
    for name in dependency_names:
        params.append(f"_{name}: Annotated[object, Depends({name})]")
    summary = f', summary={json.dumps(ep["summary"])}' if ep.get("summary") else ""
    model = f", response_model={response_model}" if response_model else ""
    returns = response_model or "dict"
    if delegate:
        args = ", ".join(delegate.get("arguments", []))
        call = f"{delegate['name']}({args})"
        body = f"    return {'await ' if delegate.get('is_async') else ''}{call}"
    else:
        literal = json.dumps(ep["response"], indent=4, sort_keys=True).replace(": true", ": True").replace(": false", ": False").replace(": null", ": None")
        if literal in ("true", "false", "null"):
            literal = {"true": "True", "false": "False", "null": "None"}[literal]
        body = "\n".join("    " + line for line in f"return {literal}".splitlines())
    return f'@{router}.{ep["method"].lower()}({json.dumps(ep["path"])}{summary}{model})\nasync def {ep["name"]}({", ".join(params)}) -> {returns}:\n{body}\n'


def path_parameters(path: str) -> List[str]:
    return re.findall(r"{([A-Za-z_][A-Za-z0-9_]*)}", path)


def unmanaged_hit(rel: str, unmanaged: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in unmanaged)


def module_names_of(source: str) -> Dict[str, int]:
    return module_names(ast.parse(source))


__all__ = ["ADOPTION_OPERATIONS", "ADOPTION_SCHEMA_11", "MountPoint", "PERSISTENCE_KINDS", "alembic_facts", "existing_tables", "extended_endpoint_code", "insert_import_line", "module_fragment", "mount_candidates", "mount_edit", "path_parameters", "render_module", "resolve_mount", "resolve_symbol", "revision_id", "unmanaged_hit", "validate_adoption_extensions"]

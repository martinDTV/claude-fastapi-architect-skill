"""Strict validation of the auditor's configuration (``.audit-fastapi.json``), v1.7.

The configuration is plain JSON read with ``json.load``; it is never imported or executed. The
sections that drive security and architecture decisions fail closed: when ``contracts``,
``public_routes`` or ``tenant_params`` are present and invalid, ``audit_project`` raises
``ConfigError`` (a ``ValueError``) listing every problem with its JSON path, and the CLI exits
with status 2 without producing a report. Sections that predate v1.7 (``layers``, ``exclude``,
``include_paths``, ``exclude_paths``, ``ignore_rules``, ``modules_root``, ``source_roots``) keep
their lenient v1.4 handling, and unknown top-level keys are still ignored, so a configuration
without ``contracts`` behaves exactly as before.

Policy for ``contracts`` (documented in references/audit-rules.md):
- must be a JSON object; unknown properties are errors unless they start with ``x-`` (annotations);
- ``route_auth_required`` and ``mutation_authorization_required``: non-empty lists of non-empty
  strings that start with ``/`` (fnmatch globs);
- ``forbidden_imports``: non-empty list of objects with exactly ``from`` and ``to`` (dotted module
  prefixes), ``from`` different from ``to`` and neither a prefix of the other;
- ``commit_layers``: non-empty list of layer names, each a default layer or a layer declared in
  ``layers``;
- a glob cannot appear both in ``public_routes`` and in an authentication/authorization contract.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_LAYERS = ("domain", "adapter", "repository", "service", "router", "core", "models", "schemas", "other")
CONTRACT_KEYS = ("route_auth_required", "mutation_authorization_required", "forbidden_imports", "commit_layers")
GLOB_CONTRACTS = ("route_auth_required", "mutation_authorization_required")


class ConfigError(ValueError):
    """Raised by ``validate_config`` / ``audit_project`` when the configuration is invalid."""

    def __init__(self, source: Optional[str], errors: List[str]) -> None:
        self.source = source or ".audit-fastapi.json"
        self.errors = list(errors)
        super().__init__(f"{self.source}: {len(self.errors)} configuration error(s): " + "; ".join(self.errors))


def _glob_list(value: Any, path: str, errors: List[str], allow_empty: bool) -> List[str]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected a list of path globs, got {type(value).__name__}")
        return []
    if not value and not allow_empty:
        errors.append(f"{path}: must not be empty (remove the key to declare nothing)")
    out: List[str] = []
    for i, item in enumerate(value):
        here = f"{path}[{i}]"
        if not isinstance(item, str):
            errors.append(f"{here}: expected a string glob, got {type(item).__name__}")
            continue
        if not item.strip():
            errors.append(f"{here}: empty glob")
            continue
        if item != item.strip():
            errors.append(f"{here}: glob has leading or trailing whitespace: {item!r}")
            continue
        if not item.startswith("/"):
            errors.append(f"{here}: route globs must start with '/': {item!r}")
            continue
        if item.count("[") != item.count("]"):
            errors.append(f"{here}: unbalanced brackets in glob {item!r}")
            continue
        out.append(item)
    return out


def validate_config(config: Dict[str, Any], source: Optional[str] = None) -> List[str]:
    """Return the list of problems (empty when valid). Only v1.7 sections are strict."""
    errors: List[str] = []
    if not isinstance(config, dict):
        return [f"top level: expected an object, got {type(config).__name__}"]
    public = _glob_list(config["public_routes"], "public_routes", errors, allow_empty=True) if "public_routes" in config else []
    if "tenant_params" in config:
        value = config["tenant_params"]
        if not isinstance(value, list):
            errors.append(f"tenant_params: expected a list of parameter names, got {type(value).__name__}")
        else:
            for i, item in enumerate(value):
                if not isinstance(item, str) or not IDENT_RE.match(item):
                    errors.append(f"tenant_params[{i}]: expected a Python identifier, got {item!r}")
    if "contracts" not in config:
        return errors
    contracts = config["contracts"]
    if not isinstance(contracts, dict):
        errors.append(f"contracts: expected an object with {', '.join(CONTRACT_KEYS)}, got {type(contracts).__name__}")
        return errors
    if not any(k in contracts for k in CONTRACT_KEYS):
        errors.append(f"contracts: declares nothing; expected at least one of {', '.join(CONTRACT_KEYS)} (remove the section to declare no contract)")
    for key in contracts:
        if key not in CONTRACT_KEYS and not str(key).startswith("x-"):
            errors.append(f"contracts.{key}: unknown property (known: {', '.join(CONTRACT_KEYS)}; use an 'x-' prefix for annotations)")
    declared_globs: Dict[str, List[str]] = {}
    for key in GLOB_CONTRACTS:
        if key in contracts:
            declared_globs[key] = _glob_list(contracts[key], f"contracts.{key}", errors, allow_empty=False)
    for key, globs in declared_globs.items():
        for g in globs:
            if g in public:
                errors.append(f"contracts.{key}: glob {g!r} is also listed in public_routes; a route cannot be both public and required to be {'authenticated' if key == 'route_auth_required' else 'authorized'}")
    if "forbidden_imports" in contracts:
        pairs = contracts["forbidden_imports"]
        if not isinstance(pairs, list) or not pairs:
            errors.append("contracts.forbidden_imports: expected a non-empty list of {\"from\": ..., \"to\": ...} objects")
        else:
            for i, pair in enumerate(pairs):
                here = f"contracts.forbidden_imports[{i}]"
                if not isinstance(pair, dict):
                    errors.append(f"{here}: expected an object with 'from' and 'to', got {type(pair).__name__}")
                    continue
                for required in ("from", "to"):
                    if required not in pair:
                        errors.append(f"{here}: missing required property '{required}'")
                    elif not isinstance(pair[required], str) or not DOTTED_RE.match(pair[required]):
                        errors.append(f"{here}.{required}: expected a dotted module prefix, got {pair[required]!r}")
                for extra in pair:
                    if extra not in ("from", "to") and not str(extra).startswith("x-"):
                        errors.append(f"{here}.{extra}: unknown property (only 'from' and 'to')")
                src, dst = pair.get("from"), pair.get("to")
                if isinstance(src, str) and isinstance(dst, str) and DOTTED_RE.match(src) and DOTTED_RE.match(dst):
                    if src == dst:
                        errors.append(f"{here}: 'from' and 'to' are the same module ({src!r}); a module cannot be forbidden from itself")
                    elif src.startswith(dst + ".") or dst.startswith(src + "."):
                        errors.append(f"{here}: {src!r} and {dst!r} nest one another; forbidding imports inside a package needs two distinct prefixes")
    if "commit_layers" in contracts:
        layers = contracts["commit_layers"]
        known = set(DEFAULT_LAYERS)
        configured = config.get("layers")
        if isinstance(configured, dict):
            known |= {str(k) for k in configured}
        if not isinstance(layers, list) or not layers:
            errors.append("contracts.commit_layers: expected a non-empty list of layer names")
        else:
            for i, layer in enumerate(layers):
                if not isinstance(layer, str) or not layer:
                    errors.append(f"contracts.commit_layers[{i}]: expected a layer name, got {layer!r}")
                elif layer not in known:
                    errors.append(f"contracts.commit_layers[{i}]: unknown layer {layer!r} (default layers: {', '.join(DEFAULT_LAYERS)}; or declare it under 'layers')")
    return errors

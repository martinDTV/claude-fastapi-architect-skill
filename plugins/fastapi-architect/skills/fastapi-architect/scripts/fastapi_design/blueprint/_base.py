"""Shared vocabulary of the blueprint: type tables, test constants and the three dataclasses every generator sees."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..advisor import STYLE_LAYERED, Recommendation
from ..spec import Spec

PY_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool", "uuid": "UUID", "datetime": "datetime", "date": "date", "decimal": "Decimal"}


SA_TYPES = {"str": "String", "int": "Integer", "float": "Float", "bool": "Boolean", "uuid": "Uuid", "datetime": "DateTime", "date": "Date", "decimal": "Numeric"}


SAMPLE = {"str": '"sample"', "int": "1", "float": "1.5", "bool": "True", "uuid": '"11111111-1111-4111-8111-111111111111"', "datetime": '"2026-01-01T00:00:00Z"', "date": '"2026-01-01"', "decimal": '"9.99"'}


WRONG = {"str": "123", "int": '"not-a-number"', "float": '"not-a-number"', "bool": '"maybe"', "uuid": '"not-a-uuid"', "datetime": '"yesterday"', "date": '"someday"', "decimal": '"many"'}


ECHOED_TYPES = ("str", "int", "bool", "uuid", "date")  # JSON round-trips these unchanged, so tests can compare them


TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


LINE = 120


@dataclass
class Lineage:
    """Migration history of a project that is being evolved: the chain already on disk is kept verbatim and a new,
    additive revision is appended for the tables that do not exist yet."""

    head: str
    files: List[str]  # relative paths of the existing migration files (never regenerated)
    tables: List[str]  # tables created by the existing chain
    revisions: List[Dict[str, object]] = field(default_factory=list)  # manifest entries of the existing chain
    baseline: Optional[Spec] = None  # the contract the chain was generated from (column deltas are computed against it)


@dataclass
class Blueprint:
    style: str
    package: str
    files: Dict[str, str]
    layout: List[str] = field(default_factory=list)  # tree lines for the report
    notes: List[str] = field(default_factory=list)
    migrations: List[Dict[str, object]] = field(default_factory=list)  # [{revision, down_revision, file, tables}] in chain order

    def to_dict(self) -> Dict[str, object]:
        return {"style": self.style, "package": self.package, "files": sorted(self.files), "layout": list(self.layout), "notes": list(self.notes)}


class Layout:
    FOLDERS = {"router": "api", "schemas": "schemas", "service": "services", "repository": "repositories", "models": "models", "dependencies": "dependencies", "ports": "ports", "adapters": "adapters"}

    def __init__(self, spec: Spec, rec: Recommendation) -> None:
        self.pkg = spec.package
        self.style = rec.style
        self.by_layer = rec.style == STYLE_LAYERED

    def path(self, component: str, module: str) -> str:
        if self.by_layer:
            return f"src/{self.pkg}/{self.FOLDERS[component]}/{module}.py"
        if component == "adapters":
            return f"src/{self.pkg}/modules/{module}/adapters/__init__.py"
        return f"src/{self.pkg}/modules/{module}/{component}.py"

    def adapter_path(self, module: str, integration: str) -> str:
        if self.by_layer:
            return f"src/{self.pkg}/adapters/{module}_{integration}.py"
        return f"src/{self.pkg}/modules/{module}/adapters/{integration}.py"

    def imp(self, component: str, module: str) -> str:
        if self.by_layer:
            return f"{self.pkg}.{self.FOLDERS[component]}.{module}"
        return f"{self.pkg}.modules.{module}.{component}"

    def adapter_imp(self, module: str, integration: str) -> str:
        if self.by_layer:
            return f"{self.pkg}.adapters.{module}_{integration}"
        return f"{self.pkg}.modules.{module}.adapters.{integration}"

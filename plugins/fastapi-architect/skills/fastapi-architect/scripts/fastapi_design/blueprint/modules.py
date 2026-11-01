"""Generated per-module layers: schemas, models, repository, service, dependencies, router, ports and adapters."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..advisor import STYLE_HEXAGONAL, Recommendation
from ..spec import EntitySpec, FieldSpec, IntegrationSpec, ModuleSpec, Spec
from ._base import LINE, SA_TYPES, Layout
from ._helpers import (
    _annotation,
    _authz,
    _call,
    _camel,
    _claim_binding,
    _constraints,
    _entity_index,
    _fk_fields,
    _header,
    _py_default,
    _registry,
    _rls,
    _sql_default,
    _tenant,
    _type_imports,
    field_from_doc,
)
from .persistence import _fk_constraint


def _ports_for(spec: Spec, rec: Recommendation, module: ModuleSpec) -> List[IntegrationSpec]:
    """Integrations that become ports of ``module`` under the hexagonal style (the integrating module, or the
    first ports module when the integration does not name one)."""
    if rec.style != STYLE_HEXAGONAL or module.name not in rec.ports_modules:
        return []
    names = {m.name for m in spec.modules}
    out: List[IntegrationSpec] = []
    for i in spec.integrations:
        target = i.module if i.module in names else rec.ports_modules[0]
        if target == module.name:
            out.append(i)
    return out


def _ops(e: EntitySpec) -> Dict[str, bool]:
    ops = set(e.operations)
    return {"create": "create" in ops, "get": bool(ops & {"get", "update", "delete"}), "get_route": "get" in ops, "list": "list" in ops, "update": "update" in ops, "delete": "delete" in ops}


def _schemas(spec: Spec, module: ModuleSpec, pending: frozenset = frozenset(), renames: Optional[Dict[Tuple[str, str], Dict[str, object]]] = None) -> str:
    """``renames`` (v2.2): ``(table, new name) -> rename entry`` for the renames in transition. Request schemas accept
    the old name as a validation alias of the new field; the read schema fills the new field from the old column
    (the source of truth until the rename is retired) and still exposes the old name, marked deprecated."""
    tenant = _tenant(spec)
    renames = renames or {}
    all_fields = [f for e in module.entities for f in e.fields]
    old_fields = {(t, n): field_from_doc(entry["from_field"]) for (t, n), entry in renames.items() if any(e.plural == t for e in module.entities)}  # type: ignore[arg-type]
    pydantic = ["AliasChoices", "BaseModel", "ConfigDict", "Field", "model_validator"] if old_fields else ["BaseModel", "ConfigDict", "Field"]
    lines = _header(
        "Request and response schemas (pydantic v2); unknown properties are rejected." + (" Fields renamed in transition accept either name and expose both, the old one deprecated; a request that sends both names is refused." if old_fields else ""),
        stdlib=_type_imports(all_fields + list(old_fields.values())) + (["from typing import Any"] if old_fields else []),
        third_party=[f"from pydantic import {', '.join(pydantic)}"],
    )
    if old_fields:
        lines += [
            "",
            "def reject_contradictory_names(data: Any, pairs: tuple[tuple[str, str], ...]) -> Any:",
            '    """A request that names a field by both its names is ambiguous (which value is meant when they differ?):',
            '    refused (422) with the reason, never resolved by precedence or by which key came first. One name passes."""',
            "    if isinstance(data, dict):",
            "        for new, old in pairs:",
            "            if new in data and old in data:",
            '                raise ValueError(f"{new!r} and {old!r} name the same field; send one of them")',
            "    return data",
        ]

    def alias(e: EntitySpec, f: FieldSpec) -> List[str]:
        entry = renames.get((e.plural, f.name))
        return [f'validation_alias=AliasChoices("{f.name}", "{entry["from_column"]}")'] if entry else []

    def boundary(e: EntitySpec) -> List[str]:
        pairs = [(f.name, str(renames[(e.plural, f.name)]["from_column"])) for f in e.fields if (e.plural, f.name) in renames]
        if not pairs:
            return []
        return [
            "",
            "    @model_validator(mode=\"before\")",
            "    @classmethod",
            "    def _one_value_per_renamed_field(cls, data: Any) -> Any:",
            f"        return reject_contradictory_names(data, RENAMED_{e.snake.upper()})",
        ]

    for e in module.entities:
        pairs = [(f.name, str(renames[(e.plural, f.name)]["from_column"])) for f in e.fields if (e.plural, f.name) in renames]
        if pairs:
            lines += ["", "", f"RENAMED_{e.snake.upper()} = {tuple(pairs)!r}  # (new name, former name) in transition", ""]

    for e in module.entities:
        ops = _ops(e)
        if ops["create"]:
            create_lines = []
            for f in e.fields:
                constraints = _constraints(f)
                if f.has_default:
                    args = [f"default={_py_default(f)}"] + ([constraints] if constraints else []) + alias(e, f)
                    create_lines += _call(f"{f.name}: {_annotation(f)} = Field(", args, ")", "    ")
                elif f.required and not constraints and not alias(e, f):
                    create_lines.append(f"    {f.name}: {_annotation(f)}")
                else:
                    args = ([] if f.required else ["default=None"]) + ([constraints] if constraints else []) + alias(e, f)
                    create_lines += _call(f"{f.name}: {_annotation(f)} = Field(", args, ")", "    ")
            lines += ["", f"class {e.name}Create(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *create_lines, *boundary(e), ""]
        if ops["update"]:
            update_lines = []
            for f in e.fields:
                update_lines += _call(f"{f.name}: {_annotation(f, optional=True)} = Field(", ["default=None"] + ([_constraints(f)] if _constraints(f) else []) + alias(e, f), ")", "    ")
            lines += ["", f"class {e.name}Update(BaseModel):", '    model_config = ConfigDict(extra="forbid")', *update_lines, *boundary(e), ""]
        read_lines = ["    id: UUID"] + ([f"    {tenant}: UUID"] if tenant else []) + (["    owner_id: str"] if e.owned else [])
        for f in e.fields:
            entry = renames.get((e.plural, f.name))
            if entry is None:
                read_lines.append(f"    {f.name}: {_annotation(f, optional=(e.plural, f.name) in pending)}")
                continue
            old = old_fields[(e.plural, f.name)]
            read_lines += _call(f"{f.name}: {_annotation(f)} = Field(", [f'validation_alias=AliasChoices("{old.name}", "{f.name}")'], ")  # read from the old column: the source of truth in transition", "    ")
            read_lines += _call(f"{old.name}: {_annotation(old)} = Field(", ['json_schema_extra={"deprecated": True}'], f')  # the former name of {f.name}, kept for older clients', "    ")
        read_lines.append("    created_at: datetime")
        lines += ["", f"class {e.name}Read(BaseModel):", "    model_config = ConfigDict(from_attributes=True)", *read_lines, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _models(spec: Spec, module: ModuleSpec, layout: Layout, pending: frozenset = frozenset(), indexed: frozenset = frozenset(), renames: Optional[Dict[Tuple[str, str], Dict[str, object]]] = None) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    renames = renames or {}
    old_fields = {(t, n): field_from_doc(entry["from_field"]) for (t, n), entry in renames.items() if any(e.plural == t for e in module.entities)}  # type: ignore[arg-type]
    all_fields = [f for e in module.entities for f in e.fields] + list(old_fields.values())
    if not pg:
        stdlib = ["from dataclasses import dataclass, field"]
        for line in _type_imports(all_fields, uuid=("UUID", "uuid4")):
            if line.startswith("from datetime import"):
                names = sorted(set(line[len("from datetime import ") :].split(", ")) | {"UTC"})
                line = "from datetime import " + ", ".join(names)
            stdlib.append(line)
        lines = _header("Entities of the module (plain dataclasses; the in-memory repository stores them as they are).", stdlib=stdlib)
        for e in module.entities:
            body = ([f"    {tenant}: UUID"] if tenant else []) + (["    owner_id: str  # subject of the principal that created the row"] if e.owned else []) + [f"    {f.name}: {_annotation(f)}" for f in e.fields if f.required and not f.has_default]
            body += [f"    {f.name}: {_annotation(f)} = {_py_default(f) if f.has_default else 'None'}" for f in e.fields if not f.required or f.has_default]
            body += ["    id: UUID = field(default_factory=uuid4)", "    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))"]
            lines += ["", "@dataclass", f"class {e.name}:", *body, ""]
        return "\n".join(lines).rstrip("\n") + "\n"
    index = _entity_index(spec)
    sa_names = {SA_TYPES[f.type] for f in all_fields} | {"DateTime", "Uuid", "func"}
    if any(f.has_default for f in all_fields):
        sa_names.add("text")
    if any(_fk_fields(e) for e in module.entities):
        sa_names.add("ForeignKeyConstraint")
    if any(e.owned for e in module.entities):
        sa_names.add("String")
    if tenant:
        sa_names.add("UniqueConstraint")
    lines = _header(
        "SQLAlchemy 2 mapped classes of the module (tables are created by Alembic, never by create_all).",
        stdlib=_type_imports(all_fields, uuid=("UUID", "uuid4")),
        third_party=[f"from sqlalchemy import {', '.join(sorted(sa_names))}", "from sqlalchemy.orm import Mapped, mapped_column"],
        local=[f"from {spec.package}.core.db import Base"],
    )
    for e in module.entities:
        table = e.plural
        body = [f'    __tablename__ = "{table}"']
        args: List[str] = []
        if tenant:
            args.append(f'        UniqueConstraint("{tenant}", "id", name="uq_{table}_{tenant}_id"),')
        for f in _fk_fields(e):
            _tm, te = index[f.references]  # type: ignore[index]
            args += _fk_constraint(table, f, te.plural, tenant, "", "        ")
        if args:
            body += ["    __table_args__ = (", *args, "    )"]
        body.append("    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)")
        if tenant:
            body.append(f"    {tenant}: Mapped[UUID] = mapped_column(Uuid, index=True)")
        if e.owned:
            body.append("    owner_id: Mapped[str] = mapped_column(String(255), index=True)  # subject of the creating principal")
        for f in e.fields:
            col = SA_TYPES[f.type]
            arg = f"String({f.max_length or 255})" if f.type == "str" else ("Numeric(18, 2)" if f.type == "decimal" else ("DateTime(timezone=True)" if f.type == "datetime" else col))
            phased = (table, f.name) in pending  # expand applied, contract not yet: nullable until the backfill is complete
            extras = ("" if f.required and not phased else ", nullable=True") + (", index=True" if f.references or (table, f.name) in indexed else "")
            if f.has_default:
                extras += f", default={_py_default(f)}, server_default=text(\"{_sql_default(f)}\")"
            body.append(f"    {f.name}: Mapped[{_annotation(f, optional=phased)}] = mapped_column({arg}{extras})")
            old = old_fields.get((table, f.name))
            if old is not None:  # the old column of a rename in transition: still written, still the source of truth
                old_arg = f"String({old.max_length or 255})" if old.type == "str" else ("Numeric(18, 2)" if old.type == "decimal" else ("DateTime(timezone=True)" if old.type == "datetime" else SA_TYPES[old.type]))
                old_extras = ("" if old.required else ", nullable=True") + (f", default={_py_default(old)}, server_default=text(\"{_sql_default(old)}\")" if old.has_default else "")
                body.append(f"    {old.name}: Mapped[{_annotation(old)}] = mapped_column({old_arg}{old_extras})  # transition: source of truth")
        body.append("    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())")
        lines += ["", f"class {e.name}(Base):", *body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _repository(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    t_sig = f"{tenant}: UUID, " if tenant else ""
    t_arg = f"{tenant}, " if tenant else ""
    lines = _header(
        "Repositories: a Protocol per entity and the implementation chosen by the design."
        + (" Owned entities take an optional owner: when given, reads are restricted to that owner's rows in the query itself." if any(e.owned for e in module.entities) else ""),
        stdlib=["from typing import Protocol", "from uuid import UUID"],
        third_party=["from sqlalchemy import select", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=[f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}"],
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        o_sig = ", owner: str | None = None" if e.owned else ""
        proto: List[str] = []
        if ops["create"]:
            proto.append(f"    async def add(self, {x}: {E}) -> {E}: ...")
        if ops["get"]:
            proto.append(f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None: ...")
        if ops["list"]:
            proto.append(f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]: ...")
        if ops["update"]:
            proto.append(f"    async def save(self, {x}: {E}) -> {E}: ...")
        if ops["delete"]:
            proto.append(f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool: ...")
        lines += ["", f"class {E}Repository(Protocol):"] + "\n\n".join(proto).split("\n") + [""]
        impl: List[str] = []
        if not pg:
            impl += [f"class InMemory{E}Repository:", '    """Process-local store; the lifespan creates one per application, tests get a fresh one per app."""', "", "    def __init__(self) -> None:", f"        self._items: dict[UUID, {E}] = {{}}"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["get"]:
                conditions = ([f"item.{tenant} != {tenant}"] if tenant else []) + (["(owner is not None and item.owner_id != owner)"] if e.owned else [])
                if conditions:
                    impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None:", f"        item = self._items.get({x}_id)", f"        if item is None or {' or '.join(conditions)}:", "            return None", "        return item"]
                else:
                    impl += ["", f"    async def get(self, {x}_id: UUID) -> {E} | None:", f"        return self._items.get({x}_id)"]
            if ops["list"]:
                conditions = ([f"item.{tenant} == {tenant}"] if tenant else []) + (["(owner is None or item.owner_id == owner)"] if e.owned else [])
                if not conditions:
                    source = ["        scoped = list(self._items.values())"]
                elif len(f"        scoped = [item for item in self._items.values() if {' and '.join(conditions)}]") <= LINE:
                    source = [f"        scoped = [item for item in self._items.values() if {' and '.join(conditions)}]"]
                else:
                    source = ["        scoped = [", "            item", "            for item in self._items.values()", f"            if {' and '.join(conditions)}", "        ]"]
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]:", *source, "        items = sorted(scoped, key=lambda item: (item.created_at, str(item.id)))", "        return items[offset : offset + limit]"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        self._items[{x}.id] = {x}", f"        return {x}"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", f"        del self._items[{x}_id]", "        return True"]
        else:
            where = f".where({E}.{tenant} == {tenant})" if tenant else ""
            owner_filter = ["        if owner is not None:", f"            statement = statement.where({E}.owner_id == owner)"] if e.owned else []
            impl += [f"class Sql{E}Repository:", '    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""', "", "    def __init__(self, session: AsyncSession) -> None:", "        self._session = session"]
            if ops["create"]:
                impl += ["", f"    async def add(self, {x}: {E}) -> {E}:", f"        self._session.add({x})", f"        return {x}"]
            if ops["get"]:
                impl += ["", f"    async def get(self, {t_sig}{x}_id: UUID{o_sig}) -> {E} | None:", f"        statement = select({E}).where({E}.id == {x}_id){where}", *owner_filter, "        return await self._session.scalar(statement)"]
            if ops["list"]:
                impl += ["", f"    async def list(self, {t_sig}limit: int, offset: int{o_sig}) -> list[{E}]:", f"        statement = select({E}){where}.order_by({E}.created_at, {E}.id)", *owner_filter, "        rows = await self._session.scalars(statement.limit(limit).offset(offset))", "        return list(rows)"]
            if ops["update"]:
                impl += ["", f"    async def save(self, {x}: {E}) -> {E}:", f"        return {x}  # tracked by the session; written at commit"]
            if ops["delete"]:
                impl += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> bool:", f"        item = await self.get({t_arg}{x}_id)", "        if item is None:", "            return False", "        await self._session.delete(item)", "        return True"]
        lines += ["", *impl, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _service(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec], renames: Optional[Dict[Tuple[str, str], Dict[str, object]]] = None) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    tenant = _tenant(spec)
    authz = _authz(spec)
    renames = renames or {}
    owned_here = any(e.owned for e in module.entities)
    # without a policy the use cases take the tenant id; with one they take the principal and derive tenant, owner and scope
    t_sig = "principal: Principal, " if authz else (f"{tenant}: UUID, " if tenant else "")
    t_arg = ("principal.tenant_id, " if tenant else "") if authz else (f"{tenant}, " if tenant else "")
    schema_names: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + ([f"{e.name}Update"] if ops["update"] else [])
    local = [
        f"from {spec.package}.core.errors import {'ConflictError, ' if pg else ''}NotFoundError",
        f"from {layout.imp('models', module.name)} import {', '.join(e.name for e in module.entities)}",
        f"from {layout.imp('repository', module.name)} import {', '.join(e.name + 'Repository' for e in module.entities)}",
    ]
    if authz:
        local.append(f"from {spec.package}.core.security import Principal")
    if owned_here:
        local.append(f"from {spec.package}.core.authz import ensure_owner, read_scope")
    if schema_names:
        local.append(f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    doc = "Use cases of the module: validation beyond the schema, domain errors, one commit per use case."
    if owned_here:
        doc += " Ownership is enforced here and in the repository query (read_scope / ensure_owner), never in the router alone."
    lines = _header(
        doc,
        stdlib=["from uuid import UUID"],
        third_party=["from sqlalchemy.exc import IntegrityError", "from sqlalchemy.ext.asyncio import AsyncSession"] if pg else [],
        local=local,
    )
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        o_arg = ", owner=read_scope(principal)" if e.owned else ""
        ctor = [f"repository: {E}Repository"] + (["session: AsyncSession"] if pg else []) + [f"{p.name}: {_camel(p.name)}Port" for p in ports]
        assigns = ["        self._repository = repository"] + (["        self._session = session"] if pg else []) + [f"        self._{p.name} = {p.name}" for p in ports]
        commit = ["        await self._commit()"] if pg else []
        transitions = {f.name: str(renames[(e.plural, f.name)]["from_column"]) for f in e.fields if (e.plural, f.name) in renames}
        body = [f"class {E}Service:", *_call("def __init__(self, ", ctor, ") -> None:", "    "), *assigns]
        if transitions:
            while lines and lines[-1] == "":
                lines.pop()
            lines += ["", f"_TRANSITIONS_{x.upper()} = {transitions!r}  # renames in transition: new -> old, both written", ""]
        if pg:
            body += [
                "",
                "    async def _commit(self) -> None:",
                '        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""',
                "        try:",
                "            await self._session.commit()",
                "        except IntegrityError as exc:",
                "            await self._session.rollback()",
                f'            detail = f"{E} violates a database constraint: {{exc.orig}}"',
                "            raise ConflictError(detail) from exc",
            ]
        if ops["create"]:
            kwargs = ([f"{tenant}=principal.tenant_id" if authz else f"{tenant}={tenant}"] if tenant else []) + (["owner_id=principal.subject"] if e.owned else []) + [f"{f.name}=data.{f.name}" for f in e.fields] + [f"{old}=data.{new}" for new, old in transitions.items()]
            notify = [f'        await self._{p.name}.send({{"event": "{x}.created", "id": str({x}.id)}})' for p in ports]
            body += ["", f"    async def create(self, {t_sig}data: {E}Create) -> {E}:", *_call(f"{x} = {E}(", kwargs, ")", "        "), f"        await self._repository.add({x})", *commit, *notify, f"        return {x}"]
        if ops["get"]:
            body += ["", f"    async def get(self, {t_sig}{x}_id: UUID) -> {E}:", f"        {x} = await self._repository.get({t_arg}{x}_id{o_arg})", f"        if {x} is None:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', f"        return {x}"]
        if ops["list"]:
            body += ["", f"    async def list(self, {t_sig}limit: int = 50, offset: int = 0) -> list[{E}]:", f"        return await self._repository.list({t_arg}limit, offset{o_arg})"]
        if ops["update"]:
            guard = [f'        ensure_owner(principal, {x}.owner_id, "update", "{E}")'] if e.owned else []
            body += ["", f"    async def update(self, {t_sig}{x}_id: UUID, data: {E}Update) -> {E}:", f"        {x} = await self.get({'principal, ' if authz else t_arg}{x}_id)", *guard, "        for name, value in data.model_dump(exclude_unset=True).items():", f"            setattr({x}, name, value)", *([f"        for new_name, old_name in _TRANSITIONS_{x.upper()}.items():  # the old column is the source of truth: write both", "            if new_name in data.model_fields_set:", f"                setattr({x}, old_name, getattr({x}, new_name))"] if transitions else []), f"        await self._repository.save({x})", *commit, f"        return {x}"]
        if ops["delete"]:
            if e.owned:
                body += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> None:", f"        {x} = await self.get(principal, {x}_id)", f'        ensure_owner(principal, {x}.owner_id, "delete", "{E}")', f"        await self._repository.delete({t_arg}{x}.id)", *commit]
            else:
                body += ["", f"    async def delete(self, {t_sig}{x}_id: UUID) -> None:", f"        removed = await self._repository.delete({t_arg}{x}_id)", "        if not removed:", f'            raise NotFoundError(f"{E} {{{x}_id}} not found")', *commit]
        lines += ["", *body, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _dependencies(spec: Spec, module: ModuleSpec, layout: Layout, ports: List[IntegrationSpec]) -> str:
    pg = spec.persistence["kind"] == "postgresql"
    session_dep = "TenantSessionDep" if _rls(spec) else "SessionDep"
    local = [
        f"from {layout.imp('repository', module.name)} import {', '.join(f'{e.name}Repository' + (f', Sql{e.name}Repository' if pg else '') for e in module.entities)}",
        f"from {layout.imp('service', module.name)} import {', '.join(e.name + 'Service' for e in module.entities)}",
    ]
    if pg:
        local.append(f"from {spec.package}.core.db import {session_dep}")
    if ports:
        local.append(f"from {layout.imp('ports', module.name)} import {', '.join(_camel(p.name) + 'Port' for p in ports)}")
    needs_request = (not pg) or bool(ports)
    doc = "Composition of the module: repositories, ports and services as FastAPI dependencies."
    if _rls(spec):
        doc += " The session carries the tenant context of the authenticated principal (TenantSessionDep), so every query runs under the row-level security policies."
    lines = _header(
        doc,
        stdlib=["from typing import Annotated"],
        third_party=["from fastapi import Depends, Request" if needs_request else "from fastapi import Depends"],
        local=local,
    )
    for p in ports:
        lines += ["", f"def get_{p.name}_port(request: Request) -> {_camel(p.name)}Port:", f'    return request.app.state.ports["{p.name}"]', ""]
    for e in module.entities:
        E, x = e.name, e.snake
        port_args = [f"{p.name}: Annotated[{_camel(p.name)}Port, Depends(get_{p.name}_port)]" for p in ports]
        port_pass = "".join(f", {p.name}" for p in ports)
        if pg:
            repo = [f"def get_{x}_repository(session: {session_dep}) -> {E}Repository:", f"    return Sql{E}Repository(session)"]
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]", f"session: {session_dep}"] + port_args
            call = f"{E}Service(repository, session{port_pass})"
        else:
            repo = [f"def get_{x}_repository(request: Request) -> {E}Repository:", f'    return request.app.state.repositories["{x}"]']
            args = [f"repository: Annotated[{E}Repository, Depends(get_{x}_repository)]"] + port_args
            call = f"{E}Service(repository{port_pass})"
        lines += ["", *repo, "", "", *_call(f"def get_{x}_service(", args, f") -> {E}Service:"), f"    return {call}", "", "", f"{E}ServiceDep = Annotated[{E}Service, Depends(get_{x}_service)]", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _router(spec: Spec, module: ModuleSpec, layout: Layout) -> str:
    tenant = _tenant(spec)
    bound = _registry(spec) or _claim_binding(spec)
    authz = _authz(spec)
    # with a tenant binding the tenant comes from the authenticated principal, never from a bare path parameter;
    # with a policy every route carries the principal admitted for its operation (core/authz.py) and passes it on
    can = {"create": "CanCreate", "list": "CanList", "get_route": "CanGet", "update": "CanUpdate", "delete": "CanDelete"}
    if authz:
        t_arg = "principal, "
    elif bound:
        t_arg = "principal.tenant_id, "
    else:
        t_arg = f"{tenant}, " if tenant else ""
    base_params = [] if authz else (["principal: TenantPrincipalDep"] if bound else ([f"{tenant}: UUID"] if tenant else []))

    def params(op: str) -> List[str]:
        return [f"principal: {can[op]}"] if authz else base_params

    schema_names: List[str] = []
    used_can: List[str] = []
    for e in module.entities:
        ops = _ops(e)
        schema_names += ([f"{e.name}Create"] if ops["create"] else []) + [f"{e.name}Read"] + ([f"{e.name}Update"] if ops["update"] else [])
        for op in ("create", "list", "get_route", "update", "delete"):
            if ops[op] and can[op] not in used_can:
                used_can.append(can[op])
    local = [f"from {layout.imp('dependencies', module.name)} import {', '.join(e.name + 'ServiceDep' for e in module.entities)}", f"from {layout.imp('schemas', module.name)} import {', '.join(schema_names)}"]
    if authz:
        local.append(f"from {spec.package}.core.authz import {', '.join(sorted(used_can))}")
    elif bound:
        local.append(f"from {spec.package}.core.security import TenantPrincipalDep")
    lines = _header(
        f"HTTP boundary of the {module.name} module: schema validation, status codes, problem details for domain errors."
        + (" Each route carries the principal admitted for its operation; the service decides on the resource." if authz else ""),
        stdlib=["from typing import Annotated", "from uuid import UUID"],
        third_party=["from fastapi import APIRouter, Query, status"],
        local=local,
    )
    lines.append(f'router = APIRouter(tags=["{module.name}"])')
    for e in module.entities:
        ops = _ops(e)
        E, x = e.name, e.snake
        prefix = (f"/tenants/{{{tenant}}}" if tenant else "") + f"/{e.plural}"
        if ops["create"]:
            lines += ["", "", f'@router.post("{prefix}", status_code=status.HTTP_201_CREATED)', *_call(f"async def create_{x}(", params("create") + [f"payload: {E}Create", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.create({t_arg}payload))"]
        if ops["list"]:
            lines += ["", "", f'@router.get("{prefix}")', f"async def list_{e.plural}(", *[f"    {p}," for p in params("list")], f"    service: {E}ServiceDep,", "    limit: Annotated[int, Query(ge=1, le=200)] = 50,", "    offset: Annotated[int, Query(ge=0)] = 0,", f") -> list[{E}Read]:", f"    return [{E}Read.model_validate(item) for item in await service.list({t_arg}limit, offset)]"]
        if ops["get_route"]:
            lines += ["", "", f'@router.get("{prefix}/{{{x}_id}}")', *_call(f"async def get_{x}(", params("get_route") + [f"{x}_id: UUID", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.get({t_arg}{x}_id))"]
        if ops["update"]:
            lines += ["", "", f'@router.patch("{prefix}/{{{x}_id}}")', *_call(f"async def update_{x}(", params("update") + [f"{x}_id: UUID", f"payload: {E}Update", f"service: {E}ServiceDep"], f") -> {E}Read:"), f"    return {E}Read.model_validate(await service.update({t_arg}{x}_id, payload))"]
        if ops["delete"]:
            lines += ["", "", f'@router.delete("{prefix}/{{{x}_id}}", status_code=status.HTTP_204_NO_CONTENT)', *_call(f"async def delete_{x}(", params("delete") + [f"{x}_id: UUID", f"service: {E}ServiceDep"], ") -> None:"), f"    await service.delete({t_arg}{x}_id)"]
    return "\n".join(lines) + "\n"


def _ports(module: ModuleSpec, ports: List[IntegrationSpec]) -> str:
    lines = _header(f"Ports of the {module.name} module: what the use cases need from the outside, as Protocols.", stdlib=["from typing import Protocol"])
    for p in ports:
        lines += ["", f"class {_camel(p.name)}Port(Protocol):", f'    """{p.kind} integration ({p.direction}, criticality {p.criticality})."""', "", "    async def send(self, payload: dict[str, object]) -> None: ...", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _adapter(p: IntegrationSpec) -> str:
    cls = _camel(p.name)
    if p.kind == "http":
        return f'''"""HTTP adapter of the {p.name} port: one client per call with an explicit timeout; errors reach the use case."""

import httpx


class Http{cls}Adapter:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def send(self, payload: dict[str, object]) -> None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_s) as client:
            response = await client.post("/events", json=payload)
            response.raise_for_status()


class Recording{cls}Adapter:
    """Development and test adapter: records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
'''
    return f'''"""Adapter of the {p.name} port ({p.kind}): a recording implementation until the real transport is decided."""


class Recording{cls}Adapter:
    """Records what would have been sent; replace with the {p.kind} client once the integration is specified."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
'''

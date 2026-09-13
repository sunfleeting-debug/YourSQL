"""可解释的逻辑/物理计划节点。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .ast import CreateIndex, CreateRole, CreateTable, CreateUser, CreateView, Delete, DropIndex, DropTable, DropView, Explain, Grant, Insert, Revoke, Select, Show, ShowGrants, Statement, Update


def _json_value(value: object) -> object:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class PlanNode:
    """计划算子；properties 保留语义信息，children 表示输入。"""

    kind: str
    properties: Mapping[str, object] = ()  # type: ignore[assignment]
    children: tuple["PlanNode", ...] = ()
    statement: Statement | None = None

    @property
    def op(self) -> str:
        return self.kind

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "node": self.kind,
            "kind": self.kind,
            "properties": _json_value(dict(self.properties)),
            "children": [child.to_dict() for child in self.children],
        }
        if self.statement is not None:
            result["statement"] = _json_value(self.statement)
        return result

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    def explain(self, depth: int = 0) -> str:
        prefix = "  " * depth
        details = ", ".join(f"{key}={_json_value(value)!r}" for key, value in self.properties.items())
        line = f"{prefix}{self.kind}" + (f" [{details}]" if details else "")
        return "\n".join([line, *(child.explain(depth + 1) for child in self.children)])


LogicalPlan = PlanNode
PhysicalPlan = PlanNode


def _select_plan(statement: Select) -> PlanNode:
    if statement.from_table is None:
        root = PlanNode("Values", {"rows": 1})
    else:
        root = PlanNode("SeqScan", {"table": statement.from_table.name, "alias": statement.from_table.alias})
        for join in statement.joins:
            right = PlanNode("SeqScan", {"table": join.table.name, "alias": join.table.alias})
            root = PlanNode("Join", {"join_type": join.join_type, "on": join.on}, (root, right))
        if statement.where is not None:
            root = PlanNode("Filter", {"predicate": statement.where}, (root,))
    if statement.group_by or statement.having is not None:
        root = PlanNode("Aggregate", {"group_by": statement.group_by, "having": statement.having}, (root,))
    root = PlanNode("Project", {"items": statement.items, "distinct": statement.distinct}, (root,))
    if statement.order_by:
        root = PlanNode("Sort", {"order_by": statement.order_by}, (root,))
    if statement.limit is not None or statement.offset:
        root = PlanNode("Limit", {"limit": statement.limit, "offset": statement.offset}, (root,))
    return root


def plan_from_statement(statement: Statement) -> PlanNode:
    """把 AST 映射为可解释的基础物理计划。"""

    if isinstance(statement, Select):
        root = _select_plan(statement)
    elif isinstance(statement, CreateTable):
        root = PlanNode("CreateTable", {"table": statement.name, "columns": statement.columns})
    elif isinstance(statement, CreateView):
        root = PlanNode("CreateView", {"view": statement.name, "query": statement.query})
    elif isinstance(statement, DropTable):
        root = PlanNode("DropTable", {"table": statement.name})
    elif isinstance(statement, DropView):
        root = PlanNode("DropView", {"view": statement.name})
    elif isinstance(statement, Insert):
        root = PlanNode("Insert", {"table": statement.table, "columns": statement.columns, "rows": statement.values})
    elif isinstance(statement, Update):
        root = PlanNode("Update", {"table": statement.table, "assignments": statement.assignments, "where": statement.where})
    elif isinstance(statement, Delete):
        root = PlanNode("Delete", {"table": statement.table, "where": statement.where})
    elif isinstance(statement, CreateIndex):
        root = PlanNode("CreateIndex", {"index": statement.name, "table": statement.table, "columns": statement.columns, "unique": statement.unique})
    elif isinstance(statement, DropIndex):
        root = PlanNode("DropIndex", {"index": statement.name})
    elif isinstance(statement, CreateRole):
        root = PlanNode("CreateRole", {"role": statement.name})
    elif isinstance(statement, CreateUser):
        root = PlanNode("CreateUser", {"user": statement.name, "roles": statement.roles})
    elif isinstance(statement, Grant):
        root = PlanNode("Grant", {"privileges": statement.privileges, "object": statement.object_name, "target": statement.target_name})
    elif isinstance(statement, Revoke):
        root = PlanNode("Revoke", {"privileges": statement.privileges, "object": statement.object_name, "target": statement.target_name})
    elif isinstance(statement, Show):
        root = PlanNode("Show", {"target": statement.target, "object": statement.object_name})
    elif isinstance(statement, ShowGrants):
        root = PlanNode("ShowGrants", {"target_kind": statement.target_kind, "target": statement.target_name})
    elif isinstance(statement, Explain):
        root = PlanNode("Explain", {"statement": statement.statement}, (plan_from_statement(statement.statement),))
    else:
        root = PlanNode(type(statement).__name__, {})
    return PlanNode(root.kind, root.properties, root.children, statement)


__all__ = ["LogicalPlan", "PhysicalPlan", "PlanNode", "plan_from_statement"]

"""物理计划节点及其可解释序列化。

逻辑计划和物理计划目前共享同一套节点字段，以保持优化规则可以逐节点
改写；但物理计划拥有独立的模块边界，后续可以在这里加入具体访问路径
和执行算子绑定，而无需把这些概念重新放回 SQL 前端。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from yoursql.sql.ast import (
    BetweenPredicate,
    BinaryOp,
    CaseExpression,
    CastExpression,
    ColumnRef,
    ExistsPredicate,
    Expr,
    FunctionCall,
    InPredicate,
    IsNull,
    Literal,
    OrderItem,
    Parameter,
    SelectItem,
    Star,
    Statement,
    Subquery,
    UnaryOp,
)


# HOW：图形标签里塞完整表达式会撑爆节点，这里统一截断到可读长度。
_LABEL_LIMIT = 56


def _literal_text(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _type_text(value: object) -> str:
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _expression_text(expression: object) -> str:
    """把表达式压成一行短 SQL 片段，用于计划节点的图形标签。"""

    if expression is None:
        return ""
    if isinstance(expression, Literal):
        return _literal_text(expression.value)
    if isinstance(expression, ColumnRef):
        return expression.qualified_name
    if isinstance(expression, Star):
        return f"{expression.table}.*" if expression.table else "*"
    if isinstance(expression, Parameter):
        return expression.name
    if isinstance(expression, UnaryOp):
        operator = expression.operator.upper()
        operand = _expression_text(expression.operand)
        return f"{operator} {operand}" if operator == "NOT" else f"{expression.operator}{operand}"
    if isinstance(expression, BinaryOp):
        return (
            f"{_expression_text(expression.left)} {expression.operator} "
            f"{_expression_text(expression.right)}"
        )
    if isinstance(expression, IsNull):
        negated = "NOT " if expression.negated else ""
        return f"{_expression_text(expression.expression)} IS {negated}NULL"
    if isinstance(expression, InPredicate):
        negated = "NOT " if expression.negated else ""
        joined = ", ".join(_expression_text(value) for value in expression.values)
        return f"{_expression_text(expression.expression)} {negated}IN ({joined})"
    if isinstance(expression, BetweenPredicate):
        negated = "NOT " if expression.negated else ""
        return (
            f"{_expression_text(expression.expression)} {negated}BETWEEN "
            f"{_expression_text(expression.lower)} AND {_expression_text(expression.upper)}"
        )
    if isinstance(expression, FunctionCall):
        prefix = "DISTINCT " if expression.distinct else ""
        joined = ", ".join(_expression_text(argument) for argument in expression.args)
        return f"{expression.name}({prefix}{joined})"
    if isinstance(expression, CastExpression):
        return f"CAST({_expression_text(expression.expression)} AS {_type_text(expression.data_type)})"
    if isinstance(expression, Subquery):
        return "(SELECT ...)"
    if isinstance(expression, ExistsPredicate):
        return f"{'NOT ' if expression.negated else ''}EXISTS (SELECT ...)"
    if isinstance(expression, CaseExpression):
        return "CASE ... END"
    return type(expression).__name__


def _property_text(value: object) -> str | None:
    """把计划属性渲染成短文本；无法/不值得展示的返回 ``None``。"""

    if isinstance(value, Expr):
        return _expression_text(value)
    if isinstance(value, SelectItem):
        alias = f" AS {value.alias}" if value.alias else ""
        return f"{_expression_text(value.expression)}{alias}"
    if isinstance(value, OrderItem):
        direction = " DESC" if value.descending else ""
        return f"{_expression_text(value.expression)}{direction}"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, (tuple, list)):
        parts = [_property_text(item) for item in value]
        rendered = [part for part in parts if part]
        return ", ".join(rendered) if rendered else None
    return None


def _clip(text: str) -> str:
    return text if len(text) <= _LABEL_LIMIT else text[: _LABEL_LIMIT - 3] + "..."


def _escape_mermaid(text: str) -> str:
    # HOW：先转义 & 再转义其余实体，否则会把刚生成的实体二次转义。
    return (
        text.replace("&", "&amp;")
        .replace('"', "#quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_dot(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


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
    """计划树的公共结构；具体计划阶段通过模块和类型别名区分。"""

    kind: str
    properties: Mapping[str, object] = field(default_factory=dict)
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
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True
        )

    def explain(self, depth: int = 0) -> str:
        prefix = "  " * depth
        details = ", ".join(
            f"{key}={_json_value(value)!r}" for key, value in self.properties.items()
        )
        line = f"{prefix}{self.kind}" + (f" [{details}]" if details else "")
        return "\n".join([line, *(child.explain(depth + 1) for child in self.children)])

    def label_lines(self) -> list[str]:
        """节点在图形里的多行标签：首行是算子名，其余是值得展示的属性。"""

        lines = [self.kind]
        for key, value in self.properties.items():
            text = _property_text(value)
            if text:
                lines.append(f"{key}={_clip(text)}")
        return lines

    def _preorder(self) -> list["PlanNode"]:
        nodes: list[PlanNode] = []

        def visit(node: PlanNode) -> None:
            nodes.append(node)
            for child in node.children:
                visit(child)

        visit(self)
        return nodes

    def to_mermaid(self, direction: str = "TD") -> str:
        """输出 Mermaid flowchart，可直接粘进 Markdown / 在线编辑器渲染。

        HOW：节点编号按先序遍历分配，父节点声明在子节点之前，保证图的可读顺序
        与计划树的阅读顺序一致。
        """

        nodes = self._preorder()
        index_of = {id(node): index for index, node in enumerate(nodes)}
        lines = [f"flowchart {direction}"]
        for index, node in enumerate(nodes):
            label = "<br/>".join(_escape_mermaid(line) for line in node.label_lines())
            lines.append(f'    n{index}["{label}"]')
        for index, node in enumerate(nodes):
            for child in node.children:
                lines.append(f"    n{index} --> n{index_of[id(child)]}")
        return "\n".join(lines)

    def to_dot(self) -> str:
        """输出 Graphviz DOT，可用 ``dot -Tsvg`` 渲染成图片。"""

        nodes = self._preorder()
        index_of = {id(node): index for index, node in enumerate(nodes)}
        lines = [
            "digraph YourSQLPlan {",
            '    rankdir=TB;',
            '    node [shape=box, style=rounded, fontname="Helvetica"];',
        ]
        for index, node in enumerate(nodes):
            label = "\\n".join(_escape_dot(line) for line in node.label_lines())
            lines.append(f'    n{index} [label="{label}"];')
        for index, node in enumerate(nodes):
            for child in node.children:
                lines.append(f"    n{index} -> n{index_of[id(child)]};")
        lines.append("}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PhysicalPlanNode(PlanNode):
    """优化器输出的物理计划节点。"""


PhysicalPlan = PhysicalPlanNode


def as_physical(plan: PlanNode) -> PhysicalPlanNode:
    """把逻辑计划树完整 materialize 为物理计划树。"""

    return PhysicalPlanNode(
        plan.kind,
        dict(plan.properties),
        tuple(as_physical(child) for child in plan.children),
        plan.statement,
    )


__all__ = ["PhysicalPlan", "PhysicalPlanNode", "PlanNode", "as_physical"]

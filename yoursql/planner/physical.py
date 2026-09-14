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

from ..sql.ast import Statement


def _json_value(value: object) -> object:
    """将计划属性转换为可序列化的 JSON 值。"""
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
        """返回计划节点的算子名称。"""
        return self.kind

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
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
        """将对象转换为 JSON 表示。"""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True
        )

    def explain(self, depth: int = 0) -> str:
        """生成适合展示的物理计划说明。"""
        prefix = "  " * depth
        details = ", ".join(
            f"{key}={_json_value(value)!r}" for key, value in self.properties.items()
        )
        line = f"{prefix}{self.kind}" + (f" [{details}]" if details else "")
        return "\n".join([line, *(child.explain(depth + 1) for child in self.children)])


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

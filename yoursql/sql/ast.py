"""SQL AST 节点；节点只描述语句，不负责目录访问或执行。"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from ..common.types import DataType


def _as_dict(value: object) -> object:
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_as_dict(item) for item in value]
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _as_dict(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class Node:
    """所有 AST 节点的可序列化基类。"""

    @property
    def source_location(self) -> tuple[int, int] | None:
        """返回解析器附加的起始位置；该元数据不参与 AST 序列化。"""

        return getattr(self, "_source_location", None)

    def source_location_for(self, key: str) -> tuple[int, int] | None:
        """返回命名语法元素的位置；该元数据不参与 AST 序列化。"""

        return getattr(self, "_source_locations", {}).get(key)

    def with_source_location(self, line: int, column: int) -> "Node":
        """为节点附加源码起点，保持现有 AST 构造器和 JSON 结构不变。"""

        object.__setattr__(self, "_source_location", (line, column))
        return self

    def with_named_source_location(self, key: str, line: int, column: int) -> "Node":
        """为语句中的列名、表名等命名元素附加源码起点。"""

        locations = dict(getattr(self, "_source_locations", {}))
        locations[key] = (line, column)
        object.__setattr__(self, "_source_locations", locations)
        return self

    def copy_source_metadata_from(self, source: "Node") -> "Node":
        """复制位置元数据，供 ``dataclasses.replace`` 后保留诊断位置。"""

        if source.source_location is not None:
            self.with_source_location(*source.source_location)
        for key, location in getattr(source, "_source_locations", {}).items():
            self.with_named_source_location(key, *location)
        return self

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"node": type(self).__name__}
        for item in fields(self):
            result[item.name] = _as_dict(getattr(self, item.name))
        return result


class Expr(Node):
    """表达式节点标记类。"""


class Statement(Node):
    """语句节点标记类。"""


@dataclass(frozen=True)
class Literal(Expr):
    value: object


@dataclass(frozen=True)
class ColumnRef(Expr):
    name: str
    table: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Parameter(Expr):
    name: str = "?"


@dataclass(frozen=True)
class Star(Expr):
    table: str | None = None


@dataclass(frozen=True)
class FunctionCall(Expr):
    name: str
    args: tuple[Expr, ...] = ()
    distinct: bool = False


@dataclass(frozen=True)
class UnaryOp(Expr):
    operator: str
    operand: Expr


@dataclass(frozen=True)
class BinaryOp(Expr):
    left: Expr
    operator: str
    right: Expr


@dataclass(frozen=True)
class IsNull(Expr):
    expression: Expr
    negated: bool = False


@dataclass(frozen=True)
class InPredicate(Expr):
    expression: Expr
    values: tuple[Expr | "Subquery", ...]
    negated: bool = False


@dataclass(frozen=True)
class Subquery(Expr):
    """只读子查询表达式，目前用于 IN (SELECT ...)。"""

    query: "Select"


@dataclass(frozen=True)
class BetweenPredicate(Expr):
    expression: Expr
    lower: Expr
    upper: Expr
    negated: bool = False


@dataclass(frozen=True)
class ColumnDefinition(Node):
    name: str
    data_type: DataType
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: Expr | None = None


@dataclass(frozen=True)
class CreateTable(Statement):
    name: str
    columns: tuple[ColumnDefinition, ...]
    if_not_exists: bool = False


@dataclass(frozen=True)
class CreateView(Statement):
    """创建只读逻辑视图。"""

    name: str
    query: "Select"
    definition_sql: str = ""
    if_not_exists: bool = False


@dataclass(frozen=True)
class CreateRole(Statement):
    name: str


@dataclass(frozen=True)
class CreateUser(Statement):
    name: str
    password: str
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grant(Statement):
    privileges: tuple[str, ...]
    object_name: str | None
    target_kind: str
    target_name: str


@dataclass(frozen=True)
class Revoke(Statement):
    privileges: tuple[str, ...]
    object_name: str | None
    target_kind: str
    target_name: str


@dataclass(frozen=True)
class DropTable(Statement):
    name: str
    if_exists: bool = False


@dataclass(frozen=True)
class DropView(Statement):
    """删除只读逻辑视图。"""

    name: str
    if_exists: bool = False


@dataclass(frozen=True)
class Insert(Statement):
    table: str
    values: tuple[tuple[Expr, ...], ...]
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectItem(Node):
    expression: Expr
    alias: str | None = None


@dataclass(frozen=True)
class TableRef(Node):
    name: str
    alias: str | None = None

    @property
    def effective_name(self) -> str:
        return self.alias or self.name


@dataclass(frozen=True)
class JoinClause(Node):
    join_type: str
    table: TableRef
    on: Expr | None = None


@dataclass(frozen=True)
class OrderItem(Node):
    expression: Expr
    descending: bool = False
    nulls_first: bool | None = None


@dataclass(frozen=True)
class Select(Statement):
    items: tuple[SelectItem, ...]
    from_table: TableRef | None = None
    joins: tuple[JoinClause, ...] = ()
    where: Expr | None = None
    group_by: tuple[Expr, ...] = ()
    having: Expr | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    offset: int = 0
    distinct: bool = False
    union: "Select | None" = None
    union_all: bool = False

    @property
    def from_source(self) -> TableRef | None:
        return self.from_table


@dataclass(frozen=True)
class Update(Statement):
    table: str
    assignments: tuple[tuple[str, Expr], ...]
    where: Expr | None = None


@dataclass(frozen=True)
class Delete(Statement):
    table: str
    where: Expr | None = None


@dataclass(frozen=True)
class CreateIndex(Statement):
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False
    if_not_exists: bool = False
    # HOW：INCLUDE 列不参与排序与唯一性，只作为覆盖列随条目存储（IndexOnlyScan 用）。
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class DropIndex(Statement):
    name: str
    if_exists: bool = False


@dataclass(frozen=True)
class Explain(Statement):
    statement: Statement


@dataclass(frozen=True)
class Show(Statement):
    target: str = "TABLES"
    object_name: str | None = None


@dataclass(frozen=True)
class ShowGrants(Statement):
    target_kind: str | None = None
    target_name: str | None = None


__all__ = [
    "BetweenPredicate",
    "BinaryOp",
    "ColumnDefinition",
    "ColumnRef",
    "CreateRole",
    "CreateIndex",
    "CreateTable",
    "CreateView",
    "CreateUser",
    "Delete",
    "DropIndex",
    "DropTable",
    "DropView",
    "Explain",
    "Expr",
    "FunctionCall",
    "Grant",
    "InPredicate",
    "Insert",
    "IsNull",
    "JoinClause",
    "Literal",
    "Node",
    "OrderItem",
    "Parameter",
    "Revoke",
    "Select",
    "SelectItem",
    "Show",
    "ShowGrants",
    "Star",
    "Statement",
    "Subquery",
    "TableRef",
    "UnaryOp",
    "Update",
]

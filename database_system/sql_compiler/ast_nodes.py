"""AST（抽象语法树）节点定义。

语句节点：CreateTable / DropTable / Insert / Select / Delete / Update / Explain
表达式节点：Literal / Identifier / Unary / Binary / IsNull / Star

所有节点都携带源码位置（line / column），表达式节点在语义分析阶段
被补上 data_type 与 ref（名字绑定结果），实现「语法结构与语义信息分离」。

注意：基类 Node 不是 dataclass，line / column 由每个节点类自行声明在末尾，
这样 `Literal(value, data_type, line=1, column=2)` 这类位置参数构造才不会错位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from database_system.utils.constants import DataType, UNKNOWN_TYPE


# ============================ 基类 ============================


class Node:
    """AST 节点基类（非 dataclass，仅约定位置信息接口）。"""

    line: int = 0
    column: int = 0

    @property
    def node_name(self) -> str:
        return type(self).__name__


# ============================ 名字绑定结果 ============================


@dataclass
class ColumnRef:
    """语义分析后，标识符指向的 Catalog 中的具体列。"""

    table_name: str = ""
    column_name: str = ""
    ordinal: int = -1
    data_type: DataType = UNKNOWN_TYPE


# ============================ 表达式节点 ============================


@dataclass
class Literal(Node):
    """常量：整数 / 字符串 / TRUE / FALSE / NULL"""

    value: Any = None
    data_type: DataType = UNKNOWN_TYPE
    line: int = 0
    column: int = 0


@dataclass
class Identifier(Node):
    """列引用，可带表限定符 t.c"""

    name: str = ""
    qualifier: Optional[str] = None
    ref: Optional[ColumnRef] = None
    data_type: DataType = UNKNOWN_TYPE
    line: int = 0
    column: int = 0


@dataclass
class Unary(Node):
    """一元运算：NOT / 负号 / 正号"""

    op: str = ""
    operand: Optional[Node] = None
    data_type: DataType = UNKNOWN_TYPE
    line: int = 0
    column: int = 0


@dataclass
class Binary(Node):
    """二元运算：算术 / 比较 / AND / OR"""

    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None
    data_type: DataType = UNKNOWN_TYPE
    line: int = 0
    column: int = 0


@dataclass
class IsNull(Node):
    """IS NULL / IS NOT NULL"""

    operand: Optional[Node] = None
    negated: bool = False
    data_type: DataType = UNKNOWN_TYPE
    line: int = 0
    column: int = 0


@dataclass
class Star(Node):
    """SELECT *"""

    line: int = 0
    column: int = 0


# ============================ 语句节点 ============================


@dataclass
class ColumnDef(Node):
    """列定义"""

    name: str = ""
    type_name: str = "INT"
    type_length: int = 0
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    default: Optional[Literal] = None
    line: int = 0
    column: int = 0


@dataclass
class CreateTable(Node):
    table_name: str = ""
    columns: list = field(default_factory=list)  # list[ColumnDef]
    if_not_exists: bool = False
    line: int = 0
    column: int = 0


@dataclass
class DropTable(Node):
    table_name: str = ""
    if_exists: bool = False
    line: int = 0
    column: int = 0


@dataclass
class Insert(Node):
    table_name: str = ""
    columns: Optional[list] = None  # list[str]，None 表示未指定（按表定义顺序）
    rows: list = field(default_factory=list)  # list[list[Node]]
    normalized_rows: Optional[list] = None  # 语义阶段补齐：按表列顺序的完整值
    line: int = 0
    column: int = 0


@dataclass
class SelectItem:
    expr: Any = None
    alias: Optional[str] = None


@dataclass
class OrderKey:
    expr: Any = None
    desc: bool = False


@dataclass
class Select(Node):
    items: list = field(default_factory=list)  # list[SelectItem]
    from_table: str = ""
    from_alias: Optional[str] = None
    where: Optional[Node] = None
    distinct: bool = False
    order_by: list = field(default_factory=list)  # list[OrderKey]
    limit: Optional[int] = None
    line: int = 0
    column: int = 0


@dataclass
class Delete(Node):
    table_name: str = ""
    where: Optional[Node] = None
    line: int = 0
    column: int = 0


@dataclass
class Update(Node):
    table_name: str = ""
    assignments: list = field(default_factory=list)  # list[(Column|str, Node)]
    where: Optional[Node] = None
    line: int = 0
    column: int = 0


@dataclass
class Explain(Node):
    stmt: Optional[Node] = None
    line: int = 0
    column: int = 0


# ============================ AST 输出 ============================


def expr_to_str(node: Optional[Node]) -> str:
    """把表达式还原为 SQL 文本，用于 Plan / 错误信息展示。"""
    if node is None:
        return "?"
    if isinstance(node, Literal):
        if node.value is None:
            return "NULL"
        if isinstance(node.value, bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node.value, str):
            return "'" + node.value.replace("'", "''") + "'"
        return str(node.value)
    if isinstance(node, Star):
        return "*"
    if isinstance(node, Identifier):
        return f"{node.qualifier}.{node.name}" if node.qualifier else node.name
    if isinstance(node, Unary):
        if node.op.upper() == "NOT":
            return f"NOT {expr_to_str(node.operand)}"
        return f"{node.op}{expr_to_str(node.operand)}"
    if isinstance(node, Binary):
        return f"({expr_to_str(node.left)} {node.op} {expr_to_str(node.right)})"
    if isinstance(node, IsNull):
        return f"({expr_to_str(node.operand)} IS {'NOT ' if node.negated else ''}NULL)"
    return "?"


def to_tree(node: Any) -> str:
    """把 AST 打印成树形结构。"""
    lines: list = []
    _tree_walk(node, "", True, lines)
    return "\n".join(lines)


def _tree_walk(node: Any, prefix: str, is_last: bool, out: list) -> None:
    if node is None:
        return
    connector = "" if prefix == "" else ("└── " if is_last else "├── ")
    out.append(prefix + connector + _node_label(node))
    children = _node_children(node)
    child_prefix = prefix + ("" if prefix == "" else ("    " if is_last else "│   "))
    for i, child in enumerate(children):
        _tree_walk(child, child_prefix, i == len(children) - 1, out)


def _node_label(node: Any) -> str:
    cls = type(node).__name__
    if isinstance(node, Literal):
        return f"Literal {expr_to_str(node)} : {node.data_type}"
    if isinstance(node, Identifier):
        name = f"{node.qualifier}.{node.name}" if node.qualifier else node.name
        bound = f" -> #{node.ref.ordinal}" if node.ref else ""
        return f"Identifier {name}{bound} : {node.data_type}"
    if isinstance(node, Binary):
        return f"Binary '{node.op}' : {node.data_type}"
    if isinstance(node, Unary):
        return f"Unary '{node.op}' : {node.data_type}"
    if isinstance(node, IsNull):
        return f"IsNull{' NOT' if node.negated else ''} : {node.data_type}"
    if isinstance(node, Star):
        return "Star"
    if isinstance(node, ColumnDef):
        length = f"({node.type_length})" if node.type_length else ""
        return f"ColumnDef {node.name} {node.type_name}{length}"
    if isinstance(node, CreateTable):
        return f"CreateTable {node.table_name}"
    if isinstance(node, DropTable):
        return f"DropTable {node.table_name}"
    if isinstance(node, Insert):
        cols = "" if not node.columns else "(" + ",".join(node.columns) + ")"
        return f"Insert {node.table_name}{cols} rows={len(node.rows)}"
    if isinstance(node, Select):
        return f"Select{' DISTINCT' if node.distinct else ''} FROM {node.from_table}"
    if isinstance(node, Delete):
        return f"Delete {node.table_name}"
    if isinstance(node, Update):
        return f"Update {node.table_name}"
    if isinstance(node, Explain):
        return "Explain"
    if isinstance(node, SelectItem):
        return f"SelectItem alias={node.alias}"
    if isinstance(node, OrderKey):
        return f"OrderKey {'DESC' if node.desc else 'ASC'}"
    return cls


def _node_children(node: Any) -> list:
    if isinstance(node, CreateTable):
        return list(node.columns)
    if isinstance(node, Insert):
        return list(node.rows[0]) if node.rows else []
    if isinstance(node, Select):
        kids: list = list(node.items)
        if node.where is not None:
            kids.append(node.where)
        kids.extend(node.order_by)
        return kids
    if isinstance(node, Delete):
        return [node.where] if node.where is not None else []
    if isinstance(node, Update):
        kids = [e for _, e in node.assignments]
        if node.where is not None:
            kids.append(node.where)
        return kids
    if isinstance(node, Explain):
        return [node.stmt] if node.stmt else []
    if isinstance(node, SelectItem):
        return [node.expr] if node.expr is not None else []
    if isinstance(node, OrderKey):
        return [node.expr] if node.expr is not None else []
    if isinstance(node, Binary):
        return [node.left, node.right]
    if isinstance(node, Unary):
        return [node.operand]
    if isinstance(node, IsNull):
        return [node.operand]
    return []

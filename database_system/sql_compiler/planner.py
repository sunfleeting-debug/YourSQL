"""阶段 4：逻辑执行计划生成。

转换规则（与 grammar.md 中「AST -> Plan」一节一致）：
    FROM   -> SeqScan          （数据源）
    WHERE  -> Filter           （逐行过滤）
    ORDER  -> OrderBy          （排序，放在 Project 之下以便引用未投影列）
    SELECT -> Project          （投影 / DISTINCT）
    LIMIT  -> Limit            （截断）
    DELETE -> Delete           （按 rowid 删除）
其余 CREATE TABLE / INSERT / UPDATE / DROP TABLE 各自对应一个算子。

Plan 节点只保存执行所需的信息（表名、列、谓词、表达式），不携带语法细节。
输出形式支持：树形 / JSON / S 表达式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.ast_nodes import expr_to_str
from database_system.sql_compiler.catalog import Catalog, Column
from database_system.utils.errors import SemanticError


# ============================ Plan 节点 ============================


@dataclass
class PlanNode:
    """算子基类。"""

    def children(self) -> list:
        return []

    def with_children(self, children: list) -> "PlanNode":
        raise NotImplementedError

    def output_columns(self) -> list:
        return []

    def describe(self) -> str:
        return type(self).__name__

    # -------- 输出形式 --------
    def to_s_expr(self) -> str:
        kids = self.children()
        if not kids:
            return f"({self.describe()})"
        inner = " ".join(c.to_s_expr() for c in kids)
        return f"({self.describe()} {inner})"

    def to_dict(self) -> dict:
        return {"node": self.describe(), "children": [c.to_dict() for c in self.children()]}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_tree(self) -> str:
        lines: list = []
        _walk(self, "", True, lines)
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.to_tree()


def _walk(node: PlanNode, prefix: str, is_last: bool, out: list) -> None:
    connector = "" if prefix == "" else ("└── " if is_last else "├── ")
    out.append(prefix + connector + node.describe())
    kids = node.children()
    child_prefix = prefix + ("" if prefix == "" else ("    " if is_last else "│   "))
    for i, kid in enumerate(kids):
        _walk(kid, child_prefix, i == len(kids) - 1, out)


@dataclass
class CreateTablePlan(PlanNode):
    table_name: str = ""
    columns: list = field(default_factory=list)

    def describe(self) -> str:
        cols = ", ".join(f"{c.name} {c.data_type}" for c in self.columns)
        return f"CreateTable {self.table_name}({cols})"

    def with_children(self, children):
        return self


@dataclass
class DropTablePlan(PlanNode):
    table_name: str = ""

    def describe(self) -> str:
        return f"DropTable {self.table_name}"

    def with_children(self, children):
        return self


@dataclass
class InsertPlan(PlanNode):
    table_name: str = ""
    columns: list = field(default_factory=list)  # list[Column]，按表列顺序
    rows: list = field(default_factory=list)  # list[list[Literal]]

    def describe(self) -> str:
        return f"Insert {self.table_name} rows={len(self.rows)}"

    def with_children(self, children):
        return self


@dataclass
class SeqScanPlan(PlanNode):
    table_name: str = ""
    alias: Optional[str] = None
    columns: list = field(default_factory=list)  # list[Column] 全列
    projection: Optional[list] = None  # 需要的列序号；None 表示全列（投影裁剪后设置）

    def describe(self) -> str:
        if self.projection is None:
            cols = ", ".join(c.name for c in self.columns)
        else:
            cols = ", ".join(self.columns[i].name for i in self.projection) or "<rowid only>"
        alias = f" AS {self.alias}" if self.alias else ""
        return f"SeqScan {self.table_name}{alias} [{cols}]"

    def output_columns(self) -> list:
        if self.projection is None:
            return [c.name for c in self.columns]
        return [self.columns[i].name for i in self.projection]

    def with_children(self, children):
        return self


@dataclass
class FilterPlan(PlanNode):
    predicate: Optional[ast.Node] = None
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        return f"Filter {expr_to_str(self.predicate)}"

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return FilterPlan(self.predicate, children[0])

    def output_columns(self) -> list:
        return self.child.output_columns() if self.child else []


@dataclass
class ProjectItem:
    expr: Optional[ast.Node] = None
    name: str = ""


@dataclass
class ProjectPlan(PlanNode):
    items: list = field(default_factory=list)  # list[ProjectItem]
    distinct: bool = False
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        cols = ", ".join(i.name for i in self.items)
        tag = " DISTINCT" if self.distinct else ""
        return f"Project{tag} [{cols}]"

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return ProjectPlan(self.items, self.distinct, children[0])

    def output_columns(self) -> list:
        return [i.name for i in self.items]


@dataclass
class OrderByPlan(PlanNode):
    keys: list = field(default_factory=list)  # list[(expr, desc)]
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        keys = ", ".join(
            f"{expr_to_str(e)}{' DESC' if d else ' ASC'}" for e, d in self.keys
        )
        return f"OrderBy [{keys}]"

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return OrderByPlan(self.keys, children[0])

    def output_columns(self) -> list:
        return self.child.output_columns() if self.child else []


@dataclass
class LimitPlan(PlanNode):
    limit: int = 0
    offset: int = 0
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        return f"Limit {self.limit}" + (f" offset {self.offset}" if self.offset else "")

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return LimitPlan(self.limit, self.offset, children[0])

    def output_columns(self) -> list:
        return self.child.output_columns() if self.child else []


@dataclass
class DeletePlan(PlanNode):
    table_name: str = ""
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        return f"Delete {self.table_name}"

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return DeletePlan(self.table_name, children[0])


@dataclass
class UpdatePlan(PlanNode):
    table_name: str = ""
    assignments: list = field(default_factory=list)  # list[(Column, expr)]
    columns: list = field(default_factory=list)  # 全列，执行期重建整行需要
    child: Optional[PlanNode] = None

    def describe(self) -> str:
        sets = ", ".join(f"{c.name} = {expr_to_str(e)}" for c, e in self.assignments)
        return f"Update {self.table_name} SET {sets}"

    def children(self) -> list:
        return [self.child] if self.child is not None else []

    def with_children(self, children):
        return UpdatePlan(self.table_name, self.assignments, self.columns, children[0])


# ============================ 表达式列引用收集 ============================


def collect_columns(node, out: Optional[set] = None) -> set:
    """收集表达式引用的列名集合（小写），供投影裁剪使用。"""
    if out is None:
        out = set()
    if node is None:
        return out
    if isinstance(node, ast.Identifier):
        if node.ref is not None:
            out.add(node.ref.column_name.lower())
        else:
            out.add(node.name.lower())
    elif isinstance(node, ast.Binary):
        collect_columns(node.left, out)
        collect_columns(node.right, out)
    elif isinstance(node, ast.Unary):
        collect_columns(node.operand, out)
    elif isinstance(node, ast.IsNull):
        collect_columns(node.operand, out)
    return out


def _output_name(node) -> str:
    """投影列的默认输出名：列引用去掉表限定符，其余用表达式文本。"""
    if isinstance(node, ast.Identifier):
        return node.ref.column_name if node.ref is not None else node.name
    return expr_to_str(node)


def is_true_literal(node) -> bool:
    return isinstance(node, ast.Literal) and node.value is True


def is_false_literal(node) -> bool:
    return isinstance(node, ast.Literal) and node.value is False


# ============================ Planner ============================


class Planner:
    """AST -> 逻辑执行计划。"""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def build(self, stmt) -> PlanNode:
        if isinstance(stmt, ast.CreateTable):
            return self._build_create_table(stmt)
        if isinstance(stmt, ast.DropTable):
            return DropTablePlan(stmt.table_name)
        if isinstance(stmt, ast.Insert):
            return self._build_insert(stmt)
        if isinstance(stmt, ast.Select):
            return self._build_select(stmt)
        if isinstance(stmt, ast.Delete):
            return self._build_delete(stmt)
        if isinstance(stmt, ast.Update):
            return self._build_update(stmt)
        if isinstance(stmt, ast.Explain):
            return self.build(stmt.stmt)
        raise SemanticError(f"cannot build plan for {type(stmt).__name__}",
                            stmt.line, stmt.column)

    # ------------------------------ 各类语句 ------------------------------

    def _build_create_table(self, stmt: ast.CreateTable) -> PlanNode:
        columns = [Column.from_def(cd, i) for i, cd in enumerate(stmt.columns)]
        return CreateTablePlan(stmt.table_name, columns)

    def _build_insert(self, stmt: ast.Insert) -> PlanNode:
        table = self.catalog.get_table(stmt.table_name)
        rows = stmt.normalized_rows
        if rows is None:
            raise SemanticError("INSERT statement has not been analyzed",
                                stmt.line, stmt.column)
        return InsertPlan(table.name, list(table.columns), rows)

    def _build_select(self, stmt: ast.Select) -> PlanNode:
        table = self.catalog.get_table(stmt.from_table)
        node: PlanNode = SeqScanPlan(table.name, stmt.from_alias, list(table.columns))

        if stmt.where is not None:
            node = FilterPlan(stmt.where, node)

        if stmt.order_by:
            node = OrderByPlan([(k.expr, k.desc) for k in stmt.order_by], node)

        items = []
        for item in stmt.items:
            name = item.alias if item.alias else _output_name(item.expr)
            items.append(ProjectItem(item.expr, name))
        node = ProjectPlan(items, stmt.distinct, node)

        if stmt.limit is not None:
            node = LimitPlan(stmt.limit, 0, node)
        return node

    def _build_delete(self, stmt: ast.Delete) -> PlanNode:
        table = self.catalog.get_table(stmt.table_name)
        node: PlanNode = SeqScanPlan(table.name, None, list(table.columns))
        if stmt.where is not None:
            node = FilterPlan(stmt.where, node)
        return DeletePlan(table.name, node)

    def _build_update(self, stmt: ast.Update) -> PlanNode:
        table = self.catalog.get_table(stmt.table_name)
        # UPDATE 需要重建整行，因此扫描全列
        node: PlanNode = SeqScanPlan(table.name, None, list(table.columns))
        if stmt.where is not None:
            node = FilterPlan(stmt.where, node)
        return UpdatePlan(table.name, stmt.assignments, list(table.columns), node)

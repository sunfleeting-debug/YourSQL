"""阶段 3：语义分析 + Catalog。

职责：
  1. 表存在性 / 列存在性检查
  2. 名字绑定：Identifier -> Catalog 中的具体列（ColumnRef）
  3. 类型一致性检查（集中式类型规则表）
  4. INSERT 列数 / 列序 / 值类型匹配检查
  5. 维护 Catalog（CREATE TABLE 检查通过后立即注册，保证同一脚本后续语句可见）
"""

from __future__ import annotations

from typing import Optional

from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.ast_nodes import ColumnRef
from database_system.sql_compiler.catalog import Catalog, Column, TableSchema
from database_system.utils.constants import (
    ARITHMETIC_OPS,
    BOOL_TYPE,
    COMPARISON_OPS,
    INT_TYPE,
    NULL_TYPE,
    UNKNOWN_TYPE,
    DataType,
)
from database_system.utils.errors import SemanticError


class Scope:
    """名字解析作用域：一张表（可带别名）。"""

    def __init__(self, table: TableSchema, alias: Optional[str] = None):
        self.table = table
        self.alias = alias

    def matches(self, qualifier: str) -> bool:
        q = qualifier.lower()
        return q == self.table.key or (self.alias is not None and q == self.alias.lower())

    def __str__(self) -> str:
        if self.alias:
            return f"{self.table.name} AS {self.alias}"
        return self.table.name


class SemanticAnalyzer:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    # ------------------------------ 入口 ------------------------------

    def analyze(self, stmt) -> None:
        if isinstance(stmt, ast.CreateTable):
            self._analyze_create_table(stmt)
        elif isinstance(stmt, ast.DropTable):
            self._analyze_drop_table(stmt)
        elif isinstance(stmt, ast.Insert):
            self._analyze_insert(stmt)
        elif isinstance(stmt, ast.Select):
            self._analyze_select(stmt)
        elif isinstance(stmt, ast.Delete):
            self._analyze_delete(stmt)
        elif isinstance(stmt, ast.Update):
            self._analyze_update(stmt)
        elif isinstance(stmt, ast.Explain):
            self.analyze(stmt.stmt)
        else:
            raise SemanticError(f"unsupported statement {type(stmt).__name__}",
                                stmt.line, stmt.column)
        return None

    # ------------------------------ CREATE TABLE ------------------------------

    def _analyze_create_table(self, stmt: ast.CreateTable) -> None:
        if not stmt.columns:
            raise SemanticError(
                f"table '{stmt.table_name}' must have at least one column",
                stmt.line, stmt.column,
            )
        seen = set()
        columns = []
        for coldef in stmt.columns:
            key = coldef.name.lower()
            if key in seen:
                raise SemanticError(
                    f"duplicate column '{coldef.name}' in table '{stmt.table_name}'",
                    coldef.line, coldef.column,
                )
            seen.add(key)
            columns.append(Column.from_def(coldef, len(columns)))
        # 检查通过 -> 立即注册到 Catalog（root_page_id 由执行阶段补齐）
        self.catalog.create_table(
            stmt.table_name, columns, root_page_id=-1, if_not_exists=stmt.if_not_exists
        )

    def _analyze_drop_table(self, stmt: ast.DropTable) -> None:
        if self.catalog.find_table(stmt.table_name) is None and not stmt.if_exists:
            raise SemanticError(
                f"table '{stmt.table_name}' does not exist", stmt.line, stmt.column
            )

    # ------------------------------ INSERT ------------------------------

    def _analyze_insert(self, stmt: ast.Insert) -> None:
        table = self.catalog.find_table(stmt.table_name)
        if table is None:
            raise SemanticError(
                f"table '{stmt.table_name}' does not exist", stmt.line, stmt.column
            )

        # 1) 解析列列表
        if stmt.columns is None:
            target_cols = list(table.columns)
        else:
            target_cols = []
            for name in stmt.columns:
                col = table.find_column(name)
                if col is None:
                    raise SemanticError(
                        f"column '{name}' does not exist in table '{table.name}'",
                        stmt.line, stmt.column,
                    )
                if any(c.key == col.key for c in target_cols):
                    raise SemanticError(
                        f"duplicate column '{name}' in INSERT column list",
                        stmt.line, stmt.column,
                    )
                target_cols.append(col)

        # 2) 逐行检查：列数 + 值必须可常量折叠 + 类型匹配
        normalized = []
        for row in stmt.rows:
            if len(row) != len(target_cols):
                raise SemanticError(
                    f"column count mismatch: expected {len(target_cols)} value(s), got {len(row)}",
                    stmt.line, stmt.column,
                )
            values = [None] * len(table.columns)
            for col, expr in zip(target_cols, row):
                if not self._is_constant(expr):
                    raise SemanticError(
                        "INSERT values must be constant expressions",
                        expr.line, expr.column,
                    )
                vtype = self._infer_const_type(expr)
                self._check_assign(col, vtype, expr)
                values[col.ordinal] = expr
            # 未指定的列填 NULL；NOT NULL 且无默认值则报错
            for col in table.columns:
                if values[col.ordinal] is None:
                    if col.not_null and col.default is None:
                        raise SemanticError(
                            f"column '{col.name}' is NOT NULL and has no value",
                            stmt.line, stmt.column,
                        )
                    default_literal = ast.Literal(None, NULL_TYPE, stmt.line, stmt.column)
                    if col.default is not None:
                        default_literal = ast.Literal(
                            col.default, self._python_type(col.default),
                            stmt.line, stmt.column,
                        )
                    values[col.ordinal] = default_literal
            normalized.append(values)

        stmt.normalized_rows = normalized

    def _python_type(self, value) -> DataType:
        if isinstance(value, bool):
            return BOOL_TYPE
        if isinstance(value, int):
            return INT_TYPE
        if value is None:
            return NULL_TYPE
        return DataType("VARCHAR")

    def _is_constant(self, expr) -> bool:
        if isinstance(expr, ast.Literal):
            return True
        if isinstance(expr, ast.Unary):
            return self._is_constant(expr.operand)
        if isinstance(expr, ast.Binary):
            return self._is_constant(expr.left) and self._is_constant(expr.right)
        if isinstance(expr, ast.IsNull):
            return self._is_constant(expr.operand)
        return False

    def _infer_const_type(self, expr) -> DataType:
        """常量表达式的类型推导（不依赖 Catalog）。"""
        if isinstance(expr, ast.Literal):
            return expr.data_type
        if isinstance(expr, ast.Unary):
            if expr.op.upper() == "NOT":
                return BOOL_TYPE
            return self._infer_const_type(expr.operand)
        if isinstance(expr, ast.IsNull):
            return BOOL_TYPE
        if isinstance(expr, ast.Binary):
            lt = self._infer_const_type(expr.left)
            rt = self._infer_const_type(expr.right)
            return self._binary_result_type(expr.op, lt, rt, expr)
        return UNKNOWN_TYPE

    def _check_assign(self, col: Column, vtype: DataType, node) -> None:
        """INSERT / UPDATE 赋值类型检查。"""
        if vtype.kind == "NULL":
            if col.not_null:
                raise SemanticError(
                    f"column '{col.name}' is NOT NULL, cannot assign NULL",
                    node.line, node.column,
                )
            return
        if not self._assignable(col.data_type, vtype):
            raise SemanticError(
                f"type mismatch: cannot assign {vtype} to column "
                f"'{col.name}' of type {col.data_type}",
                node.line, node.column,
            )

    @staticmethod
    def _assignable(target: DataType, source: DataType) -> bool:
        if target.kind != source.kind:
            return False
        if target.kind == "VARCHAR" and target.length and source.length > target.length:
            return False
        return True

    # ------------------------------ SELECT ------------------------------

    def _analyze_select(self, stmt: ast.Select) -> None:
        table = self.catalog.find_table(stmt.from_table)
        if table is None:
            raise SemanticError(
                f"table '{stmt.from_table}' does not exist", stmt.line, stmt.column
            )
        scope = Scope(table, stmt.from_alias)

        # 1) 展开 SELECT *
        expanded = []
        for item in stmt.items:
            if isinstance(item.expr, ast.Star):
                for col in table.columns:
                    expanded.append(
                        ast.SelectItem(
                            ast.Identifier(col.name, None, item.expr.line, item.expr.column)
                        )
                    )
            else:
                expanded.append(item)
        stmt.items = expanded
        if not stmt.items:
            raise SemanticError("SELECT list cannot be empty", stmt.line, stmt.column)

        # 2) 别名表（供 ORDER BY 引用 SELECT 别名）
        alias_map = {}
        for item in stmt.items:
            if item.alias:
                alias_map[item.alias.lower()] = item.expr

        # 3) 投影表达式类型检查
        for item in stmt.items:
            self._visit(item.expr, scope, alias_map)

        # 4) WHERE 必须是布尔型
        if stmt.where is not None:
            wtype = self._visit(stmt.where, scope, alias_map)
            self._require_bool(stmt.where, wtype, "WHERE clause")

        # 5) ORDER BY：别名替换 + 类型检查（可排序类型：INT / VARCHAR / BOOL）
        for key in stmt.order_by:
            key.expr = self._substitute_alias(key.expr, alias_map)
            ktype = self._visit(key.expr, scope, alias_map)
            if ktype.kind not in ("INT", "VARCHAR", "BOOL"):
                raise SemanticError(
                    f"ORDER BY expression must be a scalar type, got {ktype}",
                    key.expr.line, key.expr.column,
                )

        if stmt.limit is not None and stmt.limit < 0:
            raise SemanticError("LIMIT must be a non-negative integer",
                                stmt.line, stmt.column)

    def _substitute_alias(self, expr, alias_map):
        """把 ORDER BY 中引用 SELECT 别名的标识符替换为对应表达式。"""
        if isinstance(expr, ast.Identifier) and not expr.qualifier:
            target = alias_map.get(expr.name.lower())
            if target is not None:
                return target
        return expr

    def _require_bool(self, node, dtype: DataType, what: str) -> None:
        if dtype.kind not in ("BOOL", "NULL"):
            raise SemanticError(
                f"{what} must be of type BOOL, got {dtype}", node.line, node.column
            )

    # ------------------------------ DELETE / UPDATE ------------------------------

    def _analyze_delete(self, stmt: ast.Delete) -> None:
        table = self.catalog.find_table(stmt.table_name)
        if table is None:
            raise SemanticError(
                f"table '{stmt.table_name}' does not exist", stmt.line, stmt.column
            )
        if stmt.where is not None:
            scope = Scope(table)
            wtype = self._visit(stmt.where, scope)
            self._require_bool(stmt.where, wtype, "WHERE clause")

    def _analyze_update(self, stmt: ast.Update) -> None:
        table = self.catalog.find_table(stmt.table_name)
        if table is None:
            raise SemanticError(
                f"table '{stmt.table_name}' does not exist", stmt.line, stmt.column
            )
        scope = Scope(table)
        resolved = []
        for name, expr in stmt.assignments:
            col = table.find_column(name)
            if col is None:
                raise SemanticError(
                    f"column '{name}' does not exist in table '{table.name}'",
                    stmt.line, stmt.column,
                )
            vtype = self._visit(expr, scope)
            self._check_assign(col, vtype, expr)
            resolved.append((col, expr))
        stmt.assignments = resolved
        if stmt.where is not None:
            wtype = self._visit(stmt.where, scope)
            self._require_bool(stmt.where, wtype, "WHERE clause")

    # ------------------------------ 表达式类型检查 ------------------------------

    def _visit(self, node, scope: Scope, alias_map: Optional[dict] = None) -> DataType:
        """遍历表达式：做名字绑定 + 类型推导，返回节点类型。"""
        if node is None:
            return UNKNOWN_TYPE
        if isinstance(node, ast.Literal):
            return node.data_type
        if isinstance(node, ast.Identifier):
            self._bind_identifier(node, scope, alias_map)
            return node.data_type
        if isinstance(node, ast.Unary):
            otype = self._visit(node.operand, scope, alias_map)
            node.data_type = self._unary_result_type(node.op, otype, node)
            return node.data_type
        if isinstance(node, ast.Binary):
            ltype = self._visit(node.left, scope, alias_map)
            rtype = self._visit(node.right, scope, alias_map)
            node.data_type = self._binary_result_type(node.op, ltype, rtype, node)
            return node.data_type
        if isinstance(node, ast.IsNull):
            self._visit(node.operand, scope, alias_map)
            node.data_type = BOOL_TYPE
            return BOOL_TYPE
        if isinstance(node, ast.Star):
            raise SemanticError("'*' is not allowed here", node.line, node.column)
        raise SemanticError(f"unsupported expression {type(node).__name__}",
                            node.line, node.column)

    def _bind_identifier(self, node: ast.Identifier, scope: Scope,
                         alias_map: Optional[dict]) -> None:
        if node.qualifier and not scope.matches(node.qualifier):
            raise SemanticError(
                f"unknown table '{node.qualifier}' in column reference",
                node.line, node.column,
            )
        col = scope.table.find_column(node.name)
        if col is None:
            raise SemanticError(
                f"column '{node.name}' does not exist in table '{scope.table.name}'",
                node.line, node.column,
            )
        node.ref = ColumnRef(scope.table.name, col.name, col.ordinal, col.data_type)
        node.data_type = col.data_type

    # ------------------------------ 类型规则表 ------------------------------

    def _unary_result_type(self, op: str, otype: DataType, node) -> DataType:
        op_u = op.upper()
        if op_u == "NOT":
            if otype.kind == "NULL":
                return BOOL_TYPE
            if otype.kind != "BOOL":
                raise SemanticError(
                    f"operator 'NOT' cannot be applied to {otype}", node.line, node.column
                )
            return BOOL_TYPE
        # 一元正负号
        if otype.kind == "NULL":
            return INT_TYPE
        if otype.kind != "INT":
            raise SemanticError(
                f"unary operator '{op}' cannot be applied to {otype}",
                node.line, node.column,
            )
        return INT_TYPE

    def _binary_result_type(self, op: str, lt: DataType, rt: DataType, node) -> DataType:
        left, right = str(lt), str(rt)

        if op in ARITHMETIC_OPS:
            if lt.kind == "NULL" or rt.kind == "NULL":
                return INT_TYPE
            if lt.kind != "INT" or rt.kind != "INT":
                raise SemanticError(
                    f"operator '{op}' cannot be applied to {left} and {right}",
                    node.line, node.column,
                )
            return INT_TYPE

        if op in COMPARISON_OPS:
            if lt.kind == "NULL" or rt.kind == "NULL":
                return BOOL_TYPE
            if lt.kind != rt.kind:
                raise SemanticError(
                    f"operator '{op}' cannot be applied to {left} and {right}",
                    node.line, node.column,
                )
            return BOOL_TYPE

        op_u = op.upper()
        if op_u in ("AND", "OR"):
            if lt.kind == "NULL" or rt.kind == "NULL":
                return BOOL_TYPE
            if lt.kind != "BOOL" or rt.kind != "BOOL":
                raise SemanticError(
                    f"operator '{op_u}' cannot be applied to {left} and {right}",
                    node.line, node.column,
                )
            return BOOL_TYPE

        raise SemanticError(f"unknown operator '{op}'", node.line, node.column)

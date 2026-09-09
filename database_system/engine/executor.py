"""阶段 6：执行引擎（Volcano / 迭代器模型）。

每个算子实现 open() -> next() -> close()：
    SeqScan  遍历表的所有数据页，逐行产出
    Filter   按 WHERE 条件过滤（NULL 视为不满足）
    Project  按 SELECT 列表求值投影，支持 DISTINCT
    OrderBy  物化后排序（放在 Project 之下，可引用未投影的列）
    Limit    截断结果
    Insert / Delete / Update / CreateTable / DropTable 为 DML 算子，产出状态

表达式求值遵循 SQL 三值逻辑：任意操作数为 NULL 时，结果一般为 NULL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Optional

from database_system.engine.record import MAX_ROW_SIZE, row_size
from database_system.engine.storage_engine import RowId, TableHeap
from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.catalog import Catalog
from database_system.sql_compiler.planner import (
    CreateTablePlan,
    DeletePlan,
    DropTablePlan,
    FilterPlan,
    InsertPlan,
    LimitPlan,
    OrderByPlan,
    PlanNode,
    ProjectPlan,
    SeqScanPlan,
    UpdatePlan,
)
from database_system.storage.buffer import BufferPoolManager
from database_system.utils.constants import ARITHMETIC_OPS, COMPARISON_OPS, PageType
from database_system.utils.errors import ExecutionError


# ============================ 运行时数据结构 ============================


class Row:
    """一行数据 + 物理地址。"""

    __slots__ = ("values", "rid")

    def __init__(self, values: list, rid: Optional[RowId] = None):
        self.values = values
        self.rid = rid

    def __repr__(self) -> str:
        return f"Row({self.values}, {self.rid})"


@dataclass
class ExecContext:
    """执行期共享上下文。"""

    catalog: Catalog
    buffer: BufferPoolManager
    catalog_manager: object = None


# ============================ 表达式求值 ============================


def _column_name(node: ast.Identifier) -> str:
    return node.ref.column_name if node.ref is not None else node.name


def evaluate(expr, values: list, index: dict):
    """在三值逻辑下求值表达式。index: 列名(小写) -> 值在 values 中的位置。"""
    if expr is None:
        return None
    if isinstance(expr, ast.Literal):
        return expr.value
    if isinstance(expr, ast.Identifier):
        name = _column_name(expr).lower()
        if name not in index:
            raise ExecutionError(
                f"column '{_column_name(expr)}' is not available in this operator",
                expr.line, expr.column,
            )
        return values[index[name]]
    if isinstance(expr, ast.Unary):
        value = evaluate(expr.operand, values, index)
        op = expr.op.upper()
        if op == "NOT":
            return None if value is None else (not value)
        if value is None:
            return None
        return -value if op == "-" else +value
    if isinstance(expr, ast.IsNull):
        value = evaluate(expr.operand, values, index)
        result = value is None
        return (not result) if expr.negated else result
    if isinstance(expr, ast.Binary):
        left = evaluate(expr.left, values, index)
        right = evaluate(expr.right, values, index)
        op = expr.op
        if op in ARITHMETIC_OPS:
            if left is None or right is None:
                return None
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if right == 0:
                raise ExecutionError("division by zero", expr.line, expr.column)
            quotient = abs(left) // abs(right)
            return quotient if (left < 0) == (right < 0) else -quotient
        if op in COMPARISON_OPS:
            if left is None or right is None:
                return None
            if op == "=":
                return left == right
            if op == "!=":
                return left != right
            if op == ">":
                return left > right
            if op == ">=":
                return left >= right
            if op == "<":
                return left < right
            return left <= right
        op_u = op.upper()
        if op_u == "AND":
            if left is False or right is False:
                return False
            if left is None or right is None:
                return None
            return True
        if op_u == "OR":
            if left is True or right is True:
                return True
            if left is None or right is None:
                return None
            return False
        raise ExecutionError(f"unknown operator '{op}'", expr.line, expr.column)
    raise ExecutionError(f"cannot evaluate {type(expr).__name__}")


def _build_index(columns: list) -> dict:
    return {name.lower(): i for i, name in enumerate(columns)}


# ============================ 算子基类 ============================


class Executor:
    """流式算子：产出 Row。"""

    def open(self) -> None:
        pass

    def next(self) -> Optional[Row]:
        return None

    def close(self) -> None:
        pass

    def output_columns(self) -> list:
        return []


class DmlExecutor(Executor):
    """DDL / DML 算子：产出 (message, affected_rows)。"""

    def run(self):
        raise NotImplementedError


# ============================ 查询算子 ============================


class SeqScanExecutor(Executor):
    """SeqScan：顺序迭代表的所有数据页。"""

    def __init__(self, plan: SeqScanPlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx
        table = ctx.catalog.get_table(plan.table_name)
        self.table = table
        self.heap = TableHeap(ctx.buffer, table.root_page_id)
        if plan.projection is None:
            self.indices = list(range(len(plan.columns)))
        else:
            self.indices = list(plan.projection)
        self._it = None

    def output_columns(self) -> list:
        return [self.plan.columns[i].name for i in self.indices]

    def open(self) -> None:
        self._it = self.heap.iter_rows()

    def next(self) -> Optional[Row]:
        for rid, values in self._it:
            return Row([values[i] for i in self.indices], rid)
        return None

    def close(self) -> None:
        self._it = None


class FilterExecutor(Executor):
    def __init__(self, plan: FilterPlan, ctx: ExecContext):
        self.predicate = plan.predicate
        self.child = create_executor(plan.child, ctx)
        self._index: dict = {}

    def output_columns(self) -> list:
        return self.child.output_columns()

    def open(self) -> None:
        self.child.open()
        self._index = _build_index(self.child.output_columns())

    def next(self) -> Optional[Row]:
        while True:
            row = self.child.next()
            if row is None:
                return None
            if evaluate(self.predicate, row.values, self._index) is True:
                return row

    def close(self) -> None:
        self.child.close()


class ProjectExecutor(Executor):
    def __init__(self, plan: ProjectPlan, ctx: ExecContext):
        self.items = plan.items
        self.distinct = plan.distinct
        self.child = create_executor(plan.child, ctx)
        self._index: dict = {}
        self._seen: set = set()

    def output_columns(self) -> list:
        return [item.name for item in self.items]

    def open(self) -> None:
        self.child.open()
        self._index = _build_index(self.child.output_columns())
        self._seen = set()

    def next(self) -> Optional[Row]:
        while True:
            row = self.child.next()
            if row is None:
                return None
            out = [evaluate(item.expr, row.values, self._index) for item in self.items]
            if self.distinct:
                key = tuple(out)
                if key in self._seen:
                    continue
                self._seen.add(key)
            return Row(out, row.rid)

    def close(self) -> None:
        self.child.close()


class OrderByExecutor(Executor):
    def __init__(self, plan: OrderByPlan, ctx: ExecContext):
        self.keys = plan.keys
        self.child = create_executor(plan.child, ctx)
        self._index: dict = {}
        self._rows = None

    def output_columns(self) -> list:
        return self.child.output_columns()

    def open(self) -> None:
        self.child.open()
        self._index = _build_index(self.child.output_columns())
        rows = []
        while True:
            row = self.child.next()
            if row is None:
                break
            rows.append(row)
        keyed = [
            (tuple(evaluate(e, r.values, self._index) for e, _ in self.keys), r)
            for r in rows
        ]
        descs = [bool(d) for _, d in self.keys]

        def cmp_items(a, b):
            ka, kb = a[0], b[0]
            for i, desc in enumerate(descs):
                va, vb = ka[i], kb[i]
                if va is None and vb is None:
                    continue
                if va is None:
                    return 1  # NULL 排在最后
                if vb is None:
                    return -1
                if va == vb:
                    continue
                result = -1 if va < vb else 1
                return -result if desc else result
            return 0

        keyed.sort(key=cmp_to_key(cmp_items))
        self._rows = iter([r for _, r in keyed])

    def next(self) -> Optional[Row]:
        if self._rows is None:
            return None
        return next(self._rows, None)

    def close(self) -> None:
        self.child.close()
        self._rows = None


class LimitExecutor(Executor):
    def __init__(self, plan: LimitPlan, ctx: ExecContext):
        self.limit = plan.limit
        self.offset = plan.offset or 0
        self.child = create_executor(plan.child, ctx)
        self._emitted = 0
        self._skipped = 0

    def output_columns(self) -> list:
        return self.child.output_columns()

    def open(self) -> None:
        self.child.open()
        self._emitted = 0
        self._skipped = 0

    def next(self) -> Optional[Row]:
        while self._skipped < self.offset:
            row = self.child.next()
            if row is None:
                return None
            self._skipped += 1
        if self._emitted >= self.limit:
            return None
        row = self.child.next()
        if row is None:
            return None
        self._emitted += 1
        return row

    def close(self) -> None:
        self.child.close()


# ============================ DDL / DML 算子 ============================


class CreateTableExecutor(DmlExecutor):
    def __init__(self, plan: CreateTablePlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx

    def run(self):
        catalog, buffer = self.ctx.catalog, self.ctx.buffer
        existing = catalog.find_table(self.plan.table_name)
        if existing is not None and existing.root_page_id >= 0:
            return f"table '{self.plan.table_name}' already exists (skipped)", 0

        # new_page_unpinned：只占位，不做后续写入，因此立即归还 pin 引用
        root = buffer.new_page_unpinned(PageType.DATA)

        if existing is not None:
            existing.root_page_id = root
        else:
            catalog.create_table(self.plan.table_name, self.plan.columns, root_page_id=root)
        self.ctx.catalog_manager.save(catalog)
        return f"table '{self.plan.table_name}' created (root_page={root})", 0


class DropTableExecutor(DmlExecutor):
    def __init__(self, plan: DropTablePlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx

    def run(self):
        catalog, buffer = self.ctx.catalog, self.ctx.buffer
        schema = catalog.find_table(self.plan.table_name)
        if schema is None:
            return f"table '{self.plan.table_name}' does not exist (skipped)", 0
        freed = 0
        if schema.root_page_id >= 0:
            freed = TableHeap(buffer, schema.root_page_id).drop_all()
        catalog.drop_table(self.plan.table_name, if_exists=True)
        self.ctx.catalog_manager.save(catalog)
        return f"table '{self.plan.table_name}' dropped ({freed} page(s) freed)", 0


class InsertExecutor(DmlExecutor):
    def __init__(self, plan: InsertPlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx

    def run(self):
        table = self.ctx.catalog.get_table(self.plan.table_name)
        if table.root_page_id < 0:
            raise ExecutionError(f"table '{table.name}' is not initialized")
        heap = TableHeap(self.ctx.buffer, table.root_page_id)
        count = 0
        for row_exprs in self.plan.rows:
            values = [evaluate(e, [], {}) for e in row_exprs]
            if len(values) != len(table.columns):
                raise ExecutionError(
                    f"row has {len(values)} value(s), table has {len(table.columns)} column(s)"
                )
            if row_size(values) > MAX_ROW_SIZE:
                raise ExecutionError("row is too large to fit in one page")
            heap.insert_row(values)
            count += 1
        return f"{count} row(s) inserted into '{table.name}'", count


class DeleteExecutor(DmlExecutor):
    def __init__(self, plan: DeletePlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx
        self.child = create_executor(plan.child, ctx)

    def run(self):
        table = self.ctx.catalog.get_table(self.plan.table_name)
        heap = TableHeap(self.ctx.buffer, table.root_page_id)
        self.child.open()
        rids = []
        try:
            while True:
                row = self.child.next()
                if row is None:
                    break
                if row.rid is not None:
                    rids.append(row.rid)
        finally:
            self.child.close()
        for rid in rids:
            heap.delete_row(rid)
        return f"{len(rids)} row(s) deleted from '{table.name}'", len(rids)


class UpdateExecutor(DmlExecutor):
    def __init__(self, plan: UpdatePlan, ctx: ExecContext):
        self.plan = plan
        self.ctx = ctx
        self.child = create_executor(plan.child, ctx)

    def run(self):
        table = self.ctx.catalog.get_table(self.plan.table_name)
        heap = TableHeap(self.ctx.buffer, table.root_page_id)
        self.child.open()
        pending = []
        try:
            while True:
                row = self.child.next()
                if row is None:
                    break
                pending.append((row.rid, list(row.values)))
        finally:
            self.child.close()

        index = _build_index(self.child.output_columns())
        count = 0
        for rid, values in pending:
            new_values = list(values)
            for column, expr in self.plan.assignments:
                new_values[column.ordinal] = evaluate(expr, values, index)
            heap.delete_row(rid)
            heap.insert_row(new_values)
            count += 1
        return f"{count} row(s) updated in '{table.name}'", count


# ============================ 工厂 ============================


def create_executor(plan: PlanNode, ctx: ExecContext) -> Executor:
    """由逻辑计划构建执行器树。"""
    if isinstance(plan, SeqScanPlan):
        return SeqScanExecutor(plan, ctx)
    if isinstance(plan, FilterPlan):
        return FilterExecutor(plan, ctx)
    if isinstance(plan, ProjectPlan):
        return ProjectExecutor(plan, ctx)
    if isinstance(plan, OrderByPlan):
        return OrderByExecutor(plan, ctx)
    if isinstance(plan, LimitPlan):
        return LimitExecutor(plan, ctx)
    if isinstance(plan, CreateTablePlan):
        return CreateTableExecutor(plan, ctx)
    if isinstance(plan, DropTablePlan):
        return DropTableExecutor(plan, ctx)
    if isinstance(plan, InsertPlan):
        return InsertExecutor(plan, ctx)
    if isinstance(plan, DeletePlan):
        return DeleteExecutor(plan, ctx)
    if isinstance(plan, UpdatePlan):
        return UpdateExecutor(plan, ctx)
    raise ExecutionError(f"no executor for plan node {type(plan).__name__}")


def collect_rows(executor: Executor) -> list:
    """把流式算子的结果物化为列表。"""
    executor.open()
    rows = []
    try:
        while True:
            row = executor.next()
            if row is None:
                break
            rows.append(row.values)
    finally:
        executor.close()
    return rows

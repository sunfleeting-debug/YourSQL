"""SELECT 查询的连接、扫描、索引访问与结果投影。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from functools import cmp_to_key
from operator import itemgetter
from typing import Callable, Iterable, Protocol

from ..common import (
    CatalogError,
    ExecutionError,
    ExecutionResult,
    Schema,
    compare_values,
    sql_truth,
)
from ..common.types import PageId, RowId
from .evaluator import ConstantValue, _AMBIGUOUS, _MISSING, constant_value
from ..sql.ast import (
    BetweenPredicate,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    InPredicate,
    IsNull,
    Select,
    Star,
    Subquery,
    TableRef,
)
from ..engine.catalog import IndexMetadata, TableMetadata, ViewMetadata
from ..common.trace import ExecutionTrace, current_trace

# HOW：连接策略的代价常数，全部由本机 TPC-H SF0.01 实测标定。
_JOIN_HASH_BUILD_COST = 0.22e-6  # 60,175 行建哈希表 13 ms
_JOIN_HASH_PROBE_COST = 0.05e-6  # 2,000 次探测 0.1 ms
_JOIN_INDEX_LOOKUP_COST = 1.4e-3  # 索引等值查找（2 万条目复合索引）
_JOIN_NESTED_LOOP_PAIR_COST = (
    10.4e-6  # 嵌套循环每对候选的合并 + 谓词成本（234 s / 2,250 万对）
)
# HOW：哈希建侧的内存上限（行上下文实测 464 B/行）；超了就改用索引连接或嵌套循环。
_JOIN_HASH_MEMORY_BUDGET = 256 * 1024 * 1024
_JOIN_CONTEXT_BYTES = 464


class _RowLookup(Protocol):
    """扫描前过滤器所需的最小行访问协议。"""

    def get(self, key: str, default: object = None) -> object:
        """按名称或键获取对象；找不到时遵循调用方约定返回默认值。"""
        ...


class _RowContext(dict[str, object]):
    """查询执行中的动态行上下文；列值与行元数据共享一个命名容器。"""


class _PlanNodeLike(Protocol):
    """查询执行所需的最小物理计划协议，避免执行层反向依赖计划器实现。"""

    kind: str
    properties: Mapping[str, object]
    children: tuple["_PlanNodeLike", ...]


@dataclass(frozen=True)
class _RowContextTemplate:
    """逐行上下文的静态骨架：键、取值下标与元数据都是查询级常量。

    WHY：`_table_context` 原来每行都重算列名小写、限定键字符串和 `__row_order__`；
    在 16 列宽表上这些字符串操作会占单次全表查询三成以上的时间。
    """

    keys: tuple[str, ...]
    indices: tuple[int, ...]
    lookup: dict[str, int]
    alias: str
    table_name: str
    schema: Schema
    schemas: dict[str, Schema]
    row_order: bool


class _RowView:
    """只读的行视图：支持 dict 风格的 `get`，供 WHERE 在构建完整上下文之前先过滤。

    HOW：键到下标在模板里已算好，访问只需一次 dict 查找 + 一次 tuple 下标。
    """

    __slots__ = ("_lookup", "_row")

    def __init__(self, lookup: dict[str, int], row: tuple[object, ...]) -> None:
        """初始化实例所需的状态和依赖。"""
        self._lookup = lookup
        self._row = row

    def get(self, key: str, default: object = None) -> object:
        """按名称或键获取对象；找不到时遵循调用方约定返回默认值。"""
        index = self._lookup.get(key)
        return default if index is None else self._row[index]


@dataclass(frozen=True)
class _JoinKeyInference:
    """连接条件拆分结果：可用于键连接的列对与残余谓词。"""

    key_pairs: tuple[tuple[str, str], ...]
    residual_atoms: tuple[Expr, ...]


@dataclass(frozen=True)
class _TablePrefilter:
    """单表谓词下推结果。"""

    predicate: Callable[[_RowLookup], object] | None
    needed_columns: frozenset[str]


@dataclass(frozen=True)
class _IndexConstraintMatch:
    """谓词转换出的列名和单列索引约束。"""

    column: str
    constraint: "_IndexConstraint"


@dataclass(frozen=True)
class _IndexBoundary:
    """索引边界值及其是否包含端点。"""

    value: object
    inclusive: bool


@dataclass(frozen=True)
class _IndexProbeBounds:
    """覆盖索引首列探测范围。"""

    low: object | None
    high: object | None
    include_low: bool
    include_high: bool


@dataclass(frozen=True)
class _ProjectedRow:
    """投影结果、排序上下文和别名值的组合。"""

    values: tuple[object, ...]
    context: _RowContext
    aliases: dict[str, object]


@dataclass(frozen=True)
class _ContextColumnSelection:
    """逐行上下文需要的列集合与是否保留原始行顺序。"""

    needed: frozenset[str] | None
    row_order: bool


@dataclass(frozen=True)
class _IndexCandidateSet:
    """一个索引约束签名对应的候选 RowId 集合。"""

    signature: tuple[object, ...]
    row_ids: tuple[RowId, ...] | None


@dataclass(frozen=True)
class _RowOrderEntry:
    """SELECT * 展开所需的限定名、列名和原始值。"""

    alias: str
    column: str
    value: object

    def __iter__(self):
        """兼容旧的三元组遍历。"""

        yield self.alias
        yield self.column
        yield self.value


def _collect_column_ref_nodes(value: object, sink: list[ColumnRef]) -> bool:
    """递归收集表达式里的 ColumnRef 节点；返回是否遇到子查询（子查询不可下推）。"""

    if isinstance(value, ColumnRef):
        sink.append(value)
        return False
    if isinstance(value, Subquery):
        return True
    if is_dataclass(value):
        found = False
        for field in fields(value):
            found |= _collect_column_ref_nodes(getattr(value, field.name), sink)
        return found
    if isinstance(value, (list, tuple)):
        found = False
        for item in value:
            found |= _collect_column_ref_nodes(item, sink)
        return found
    return False


def _collect_column_refs(value: object, sink: set[str]) -> bool:
    """递归收集表达式引用的列名（含限定名，均小写），返回是否遇到 `*`。

    HOW：按 dataclass 字段泛化遍历，新增 AST 节点类型无需同步修改。
    """

    if isinstance(value, ColumnRef):
        name = value.name.lower()
        sink.add(name)
        if value.table:
            sink.add(f"{value.table.lower()}.{name}")
        return False
    if isinstance(value, Star):
        return True
    if is_dataclass(value):
        found = False
        for field in fields(value):
            found |= _collect_column_refs(getattr(value, field.name), sink)
        return found
    if isinstance(value, (list, tuple)):
        found = False
        for item in value:
            found |= _collect_column_refs(item, sink)
        return found
    return False


@dataclass
class _IndexConstraint:
    """单列索引条件的交集；用于按联合索引前缀生成扫描边界。"""

    allowed: list[object] | None = None
    lower: _IndexBoundary | None = None
    upper: _IndexBoundary | None = None
    not_null: bool = False


class QueryExecutionMixin:
    """提供 SELECT 查询的执行细节；数据库实例只负责提供目录、存储与生命周期依赖。"""

    # ----- SELECT 主流程：扫描上下文 -> 聚合/投影 -> 排序分页 -----
    def _execute_select(
        self,
        statement: Select,
        output_columns: Iterable[str] = (),
        *,
        plan: _PlanNodeLike | None = None,
        allow_system_tables: bool = False,
    ) -> ExecutionResult:
        # HOW：查询入口组织记录流，再处理聚合、投影、去重、排序和分页，最后返回结果。
        """执行 SELECT 的扫描、连接、过滤、聚合、排序和投影阶段。"""
        trace = current_trace.get()
        if trace is not None:
            trace.check()
        previous_scan_kind = self._last_scan_kind
        previous_join_kinds = self._join_kinds
        self._last_scan_kind = None
        self._join_kinds = []
        if statement.union is not None:
            left = self._execute_select(
                replace(statement, union=None), output_columns, plan=plan
            )
            right = self._execute_select(statement.union)
            rows = [*left.rows, *right.rows]
            if not statement.union_all:
                rows = list(dict.fromkeys(rows))
            return ExecutionResult(
                left.columns,
                rows,
                stats={
                    "operator": "Union",
                    "left_rows": len(left.rows),
                    "right_rows": len(right.rows),
                },
            )
        before = self.buffer_pool.stats()
        # HOW：列名先按折叠前的语句计算，避免 `SELECT 1 + 2` 这类表达式的输出名随折叠改变。
        names = list(output_columns)#开始时先确定结果列名：
        if not names:
            names = self._output_names(statement)
        statement = self._fold_statement(statement)
        # HOW：扫描/连接保持惰性；只有聚合、排序、去重或需要全量结果时才全部消费。
        scanned = 0

        def _count(
            iterable: Iterable[_RowContext],
        ) -> Iterable[_RowContext]:
            """统计输入中的记录数量。"""
            nonlocal scanned
            for item in iterable:
                scanned += 1
                yield item
        # 请下层提供这次查询需要处理的记录，我再对这些记录计算输出内容。

        contexts: Iterable[_RowContext] = _count(
            self._iter_select_contexts(
                statement, plan=plan, allow_system_tables=allow_system_tables
            )
        )
        has_aggregate = any(
            self._contains_aggregate(item.expression) for item in statement.items
        ) or self._contains_aggregate(statement.having)
        grouped: Iterable[_RowContext]
        if statement.group_by or has_aggregate:
            group_keys = [
                self._compile_expr(expression) for expression in statement.group_by
            ]
            having = self._compile_expr(statement.having)
            groups: dict[tuple[object, ...], list[_RowContext]] = {}
            for context in contexts:
                if trace is not None:
                    trace.step()
                key = tuple(expression(context) for expression in group_keys)
                groups.setdefault(key, []).append(context)
            if has_aggregate and not groups:
                groups[()] = []
            grouped_rows: list[_RowContext] = []
            for group in groups.values():
                base = (
                    _RowContext(group[0])
                    if group
                    else _RowContext(__row_order__=[], __row_ids__={})
                )
                base["__group__"] = group
                if statement.having is None or sql_truth(having(base)):
                    grouped_rows.append(base)
            grouped = grouped_rows
        else:
            grouped = contexts
        projected: list[_ProjectedRow] = []
        item_evaluators = [
            (
                None
                if isinstance(item.expression, Star)
                else self._compile_expr(item.expression),
                item,
            )
            for item in statement.items
        ]
        # HOW：无排序/去重/聚合时，只需凑够 offset+limit 行就可以提前结束扫描。
        streamable = (
            not statement.order_by
            and not statement.distinct
            and not (statement.group_by or has_aggregate)
        )
        stop_after = (
            (statement.offset or 0) + statement.limit
            if (streamable and statement.limit is not None)
            else None
        )
        seen: set[tuple[object, ...]] = set()
        for context in grouped:
            if trace is not None:
                trace.step()
            values: list[object] = []
            aliases: dict[str, object] = {}
            for evaluator, item in item_evaluators:
                if isinstance(item.expression, Star):
                    values.extend(self._expand_star(context, item.expression.table))
                    continue
                if evaluator is None:
                    raise ExecutionError("非 Star 投影缺少表达式求值器")
                # HOW：计算当前行的 SELECT 表达式，例如 name 或 age+1，不修改表中的原值。
                value = evaluator(context)
                values.append(value)
                if item.alias:
                    aliases[item.alias.lower()] = value
            row_key = tuple(values)
            if statement.distinct:
                if row_key in seen:
                    continue
                seen.add(row_key)
            projected.append(_ProjectedRow(row_key, context, aliases))
            if stop_after is not None and len(projected) >= stop_after:
                break
        if statement.distinct:
            unique: dict[tuple[object, ...], _ProjectedRow] = {}
            for item in projected:
                unique.setdefault(item.values, item)
            projected = list(unique.values())
        for order_item in reversed(statement.order_by):
            evaluator = self._compile_expr(order_item.expression)
            by_alias = (
                isinstance(order_item.expression, ColumnRef)
                and not order_item.expression.table
            )
            alias_key = order_item.expression.name.lower() if by_alias else ""

            def key(
                item: _ProjectedRow,
            ) -> tuple[int, object]:
                """根据输入构造稳定的键值。"""
                value = (
                    item.aliases.get(alias_key, _MISSING) if by_alias else _MISSING
                )
                if value is _MISSING:
                    value = evaluator(item.context)
                nulls_first = (
                    order_item.nulls_first
                    if order_item.nulls_first is not None
                    else order_item.descending
                )
                if value is None:
                    return (0 if nulls_first else 1, 0)
                return (1 if nulls_first else 0, value)

            try:
                projected.sort(key=key, reverse=order_item.descending)
            except TypeError:
                projected.sort(
                    key=lambda item: repr(key(item)), reverse=order_item.descending
                )
        if statement.offset:
            projected = projected[statement.offset :]
        if statement.limit is not None:
            projected = projected[: statement.limit]
        after = self.buffer_pool.stats()
        stats = {
            "operator": "SeqScan",
            "page_reads": int(after["misses"] - before["misses"]),
            "cache_hits": int(after["hits"] - before["hits"]),
            "rows_examined": scanned,
        }
        uses_index = (
            self._plan_uses_index(plan)
            if plan is not None
            else self._uses_index(statement)
        )
        scan_kind = self._last_scan_kind
        join_kinds = self._join_kinds
        self._last_scan_kind = previous_scan_kind
        self._join_kinds = previous_join_kinds
        if scan_kind is not None:
            stats["operator"] = scan_kind
        elif uses_index:
            stats["operator"] = "IndexScan"
        if join_kinds:
            stats["joins"] = list(join_kinds)
        return ExecutionResult(
            tuple(names), [item.values for item in projected], stats=stats
        )

    # ----- 扫描与连接上下文 -----
    # HOW：决定 WHERE 的执行位置；单表可以先过滤再创建完整上下文，覆盖索引可直接供值。
    def _iter_select_contexts(#决定记录怎样产生、条件在哪里判断
        self,
        statement: Select,
        *,
        plan: _PlanNodeLike | None = None,
        allow_system_tables: bool = False,
    ) -> Iterable[_RowContext]:
        """惰性产出 SELECT 使用的逐行查询上下文。"""
        if self._plan_contains_kind(plan, "EmptyScan"):
            return []
        selection = self._needed_context_columns(statement)
        needed = selection.needed
        row_order = selection.row_order
        if statement.from_table is not None and not statement.joins:
            # HOW：覆盖索引直读优先；只在所有被引用列都在索引里时启用，且仍需用 WHERE 过滤残余谓词。
            index_only = self._index_only_contexts(
                statement.from_table, statement.where, needed
            )
            if index_only is not None:
                self._last_scan_kind = "IndexOnlyScan"
                if statement.where is None:
                    return iter(index_only)
                predicate = self._compile_expr(statement.where)
                return (
                    context for context in index_only if sql_truth(predicate(context))
                )
        # HOW：WHERE 只引用本表列且没有 JOIN 时，可以在建上下文之前先过滤（Q6 只 800/60175 行通过）。
        prefilter: Callable[[_RowLookup], object] | None = None
        prefilter_needed: frozenset[str] = frozenset()
        if (
            statement.from_table is not None
            and not statement.joins
            and statement.where is not None
        ):
            where_columns: set[str] = set()
            if not _collect_column_refs(statement.where, where_columns):
                prefilter = self._compile_expr(statement.where)
                prefilter_needed = frozenset(where_columns)
        scan_plans = self._scan_plans(plan)
        if statement.from_table is None:
            return iter(
                [
                    _RowContext(
                        __row_order=[], __row_ids={}, __schemas={}
                    )
                ]
            )
        generated = self._joined_contexts(
            statement,
            scan_plans=scan_plans,
            allow_system_tables=allow_system_tables,
            needed=needed,
            row_order=row_order,
            prefilter=prefilter,
            prefilter_needed=prefilter_needed,
        )
        if statement.where is not None and prefilter is None:
            # HOW：无预过滤（例如带 JOIN）时，WHERE 在流上惰性求值，不再先物化全部连接结果。
            predicate = self._compile_expr(statement.where)
            return (context for context in generated if sql_truth(predicate(context)))
        return generated

    def _joined_contexts(
        self,
        statement: Select,
        *,
        scan_plans: list[_PlanNodeLike],
        allow_system_tables: bool,
        needed: frozenset[str] | None,
        row_order: bool,
        prefilter: Callable[[_RowLookup], object] | None,
        prefilter_needed: frozenset[str],
    ) -> Iterable[_RowContext]:
        """惰性产出逐行上下文：单表扫描与连接都不再全量物化。

        HOW：左表（及每个连接的左侧）保持流式；右表需要反复扫描，因此只物化右表。
        RIGHT/FULL 需要在连接结束后知道哪些右行未被匹配，这两种连接类型仍会缓存匹配状态。
        """

        trace = current_trace.get()
        primary_plan = scan_plans[0] if scan_plans else None
        # HOW：把 WHERE 拆成 AND 原子，能只引用单表的原子直接下推到该表扫描（带 JOIN 时原来只在连接后过滤）。
        atoms = (
            self._conjunction_atoms(statement.where)
            if statement.where is not None
            else ()
        )
        # HOW：按表名/别名记录列名集合，用于判定未限定列名属于哪一侧、以及原子是否已就绪。
        scope_columns: dict[str, set[str]] = {}
        for reference in (
            statement.from_table,
            *(join.table for join in statement.joins),
        ):
            columns = self._relation_columns(reference.name)
            if columns is None:
                continue
            scope_columns[reference.name.lower()] = columns
            if reference.alias:
                scope_columns[reference.alias.lower()] = columns
        primary_prefilter = self._table_prefilter(atoms, statement.from_table)
        primary_filter = primary_prefilter.predicate
        primary_needed = primary_prefilter.needed_columns
        contexts: Iterable[_RowContext] = self._scan_contexts(
            statement.from_table,
            self._scan_predicate(
                scan_plans[0] if scan_plans else None, statement.where
            ),
            scan_plan=primary_plan,
            allow_system_tables=allow_system_tables,
            needed=needed,
            row_order=row_order,
            prefilter=primary_filter if primary_filter is not None else prefilter,
            prefilter_needed=primary_needed
            if primary_filter is not None
            else prefilter_needed,
        )
        for join_index, join in enumerate(statement.joins, start=1):
            join_plan = scan_plans[join_index] if join_index < len(scan_plans) else None
            join_prefilter = self._table_prefilter(atoms, join.table)
            join_filter = join_prefilter.predicate
            join_needed = join_prefilter.needed_columns
            right_qualifiers = {
                join.table.name.lower(),
                (join.table.alias or "").lower(),
            } - {""}
            left_qualifiers = {
                statement.from_table.name.lower(),
                (statement.from_table.alias or "").lower(),
            } - {""}
            for previous in statement.joins[: join_index - 1]:
                left_qualifiers |= {
                    previous.table.name.lower(),
                    (previous.table.alias or "").lower(),
                } - {""}
            on_atoms = (
                self._conjunction_atoms(join.on)
                if join.join_type != "CROSS" and join.on is not None
                else ()
            )
            # WHY：逗号连接（`FROM a, b WHERE a.x = b.y`）会被解析成 CROSS 且 ON 为空，
            # 连接条件全在 WHERE 里；只在 INNER/CROSS 下从 WHERE 推断连接键，
            # 外连接的 WHERE 必须在连接之后生效，不能提升为连接条件。
            inferred = atoms if join.join_type in {"INNER", "CROSS"} else ()
            # HOW：先把范围限定到“已就绪的表”（左侧已连接的表 + 当前右表）；
            # 引用后续表的原子留给那一层连接或最后的 WHERE 过滤，否则会报“执行时找不到列”。
            available = left_qualifiers | right_qualifiers
            # HOW：只丢弃“引用尚未就绪的表”的推断原子（它们会在后续连接或最后 WHERE 里生效）；
            # ON 原子是连接语义的一部分，必须全部保留。
            usable_inferred = tuple(
                atom
                for atom in inferred
                if (scope := self._atom_scope(atom, scope_columns)) is not None
                and scope <= available
            )
            join_atoms = (*on_atoms, *usable_inferred)
            left_columns = self._relation_columns(statement.from_table.name) or set()
            for previous in statement.joins[: join_index - 1]:
                left_columns |= self._relation_columns(previous.table.name) or set()
            right_columns = self._relation_columns(join.table.name) or set()
            join_keys = self._join_key_pairs(
                join_atoms,
                left_qualifiers,
                right_qualifiers,
                left_columns,
                right_columns,
            )
            pairs = join_keys.key_pairs
            residual_atoms = join_keys.residual_atoms
            right_prefilter = self._table_prefilter(
                residual_atoms, join.table
            )
            left_prefilter = self._table_prefilter(
                residual_atoms, statement.from_table
            )
            right_only = right_prefilter.predicate
            right_only_needed = right_prefilter.needed_columns
            left_only = left_prefilter.predicate
            remaining = residual_atoms
            if right_only is not None:
                # HOW：已下推到右侧扫描的原子不再重复求值；其余（含左侧相关原子）留作连接后的残余谓词。
                pushed = set(self._conjunction_atoms(right_only))
                remaining = tuple(atom for atom in residual_atoms if atom not in pushed)
            residual = (
                self._compile_expr(self._combine_atoms(remaining))
                if remaining
                else None
            )
            # HOW：右表在连接列上的索引可用于索引嵌套循环（前导列需与连接键列一致）。
            join_columns = [pair[1] for pair in pairs]
            index_metadata = (
                self._join_index_metadata(join.table, join_columns)
                if join_columns
                else None
            )
            right_rows = int(self.catalog.get_table(join.table.name).stats.row_count)
            # HOW：左侧行数按首表统计粗估（左侧可能已被连接放大，这里宁可偏低以便优先选哈希）。
            left_rows = int(
                self.catalog.get_table(statement.from_table.name).stats.row_count
            )
            strategy = self._choose_join_strategy(
                pairs, right_rows, left_rows, index_metadata
            )
            self._join_kinds.append(
                {
                    "hash": "HashJoin",
                    "index": "IndexNestedLoop",
                    "nested_loop": "NestedLoop",
                }[strategy]
            )
            if strategy == "index" and index_metadata is not None:
                # HOW：索引连接不需要物化右表（内存与扫描成本都省掉）。
                contexts = self._index_join(
                    contexts,
                    pairs=pairs,
                    metadata=index_metadata,
                    join_reference=join.table,
                    from_table=statement.from_table,
                    table=self.catalog.get_table(join.table.name),
                    residual=residual,
                    left_only=left_only,
                    join_type=join.join_type,
                    needed=needed,
                    row_order=row_order,
                )
                continue
            right = list(
                self._scan_contexts(
                    join.table,
                    self._scan_predicate(join_plan, None),
                    scan_plan=join_plan,
                    allow_system_tables=allow_system_tables,
                    needed=needed,
                    row_order=row_order,
                    prefilter=right_only if right_only is not None else join_filter,
                    prefilter_needed=right_only_needed
                    if right_only is not None
                    else join_needed,
                )
            )
            if strategy == "hash":
                contexts = self._hash_join(
                    contexts,
                    right,
                    pairs=pairs,
                    residual=residual,
                    left_only=left_only,
                    join_type=join.join_type,
                    join_table=join.table,
                    from_table=statement.from_table,
                    needed=needed,
                    row_order=row_order,
                )
                continue
            condition = (
                self._compile_expr(self._combine_atoms(tuple(join_atoms)))
                if join.join_type == "CROSS" and join_atoms
                else (
                    None if join.join_type == "CROSS" else self._compile_expr(join.on)
                )
            )
            contexts = self._stream_join(
                contexts,
                right,
                join_type=join.join_type,
                join_table=join.table,
                from_table=statement.from_table,
                condition=condition,
                needed=needed,
                row_order=row_order,
                trace=trace,
            )
        return contexts

    # ----- 连接条件分析与策略选择 -----
    def _combine_atoms(self, atoms: tuple[Expr, ...]) -> Expr | None:
        """把多个原子用 AND 串成单个表达式。"""

        if not atoms:
            return None
        combined: Expr = atoms[0]
        for extra in atoms[1:]:
            combined = BinaryOp(combined, "AND", extra)
        return combined

    def _join_index_metadata(
        self, reference: TableRef, columns: list[str]
    ) -> IndexMetadata | None:
        """找出前导列恰好等于连接键列的索引；找不到返回 None。"""

        try:
            table = self.catalog.get_table(reference.name)
        except CatalogError:
            return None
        wanted = [column.lower() for column in columns]
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id or len(metadata.columns) < len(
                wanted
            ):
                continue
            if [column.lower() for column in metadata.columns[: len(wanted)]] == wanted:
                return metadata
        return None

    def _relation_columns(self, name: str) -> set[str] | None:
        """关系的列名集合（表或视图）；取不到返回 None。"""

        try:
            relation = self.catalog.get_relation(name)
        except CatalogError:
            try:
                relation = self.catalog.get_table(name, include_system=True)
            except CatalogError:
                return None
        return {column.name.lower() for column in relation.schema}

    def _subquery_is_correlated(self, query: Select) -> bool:
        """子查询是否引用外层列（相关子查询）；含嵌套子查询时保守视为相关。"""

        local: set[str] = set()
        local_columns: set[str] = set()
        for reference in (query.from_table, *(join.table for join in query.joins)):
            if reference is None:
                continue
            local.add(reference.name.lower())
            if reference.alias:
                local.add(reference.alias.lower())
            local_columns |= self._relation_columns(reference.name) or set()
        refs: list[ColumnRef] = []
        if _collect_column_ref_nodes(query, refs):
            return True
        for ref in refs:
            qualifier = (ref.table or "").lower()
            if qualifier:
                if qualifier not in local:
                    return True
            elif ref.name.lower() not in local_columns:
                return True
        return False

    def _atom_scope(
        self, atom: Expr, scope_columns: dict[str, set[str]]
    ) -> set[str] | None:
        """原子引用了哪些表；含子查询或列归属不唯一时返回 None（不参与本层连接）。"""

        refs: list[ColumnRef] = []
        if _collect_column_ref_nodes(atom, refs):
            return None
        scope: set[str] = set()
        for ref in refs:
            qualifier = (ref.table or "").lower()
            if qualifier:
                scope.add(qualifier)
                continue
            owners = {
                name
                for name, columns in scope_columns.items()
                if ref.name.lower() in columns
            }
            if len(owners) != 1:
                return None
            scope |= owners
        return scope

    def _join_key_pairs(
        self,
        atoms: tuple[Expr, ...],
        left_qualifiers: set[str],
        right_qualifiers: set[str],
        left_columns: set[str],
        right_columns: set[str],
    ) -> _JoinKeyInference:
        """从连接条件里抽出等值键对（左列, 右列），其余原子作为残余谓词。

        HOW：同时处理两种写法——`JOIN ... ON a = b` 与逗号连接 `FROM a, b WHERE a.x = b.y`
        （后者解析成 CROSS 连接，谓词全在 WHERE 里）；未限定的列名用两侧模式列名判定归属。
        """

        pairs: list[tuple[str, str]] = []
        residual: list[Expr] = []
        for atom in atoms:
            if isinstance(atom, BinaryOp) and atom.operator.upper() == "OR":
                # WHY：像 Q19 那样把连接键写在每个 OR 分支里（`(p_partkey = l_partkey AND ...) OR ...`）时，
                # 只有“每个分支都要求的等式”才能当连接键（它是必要条件，哈希连接不会漏行）。
                left_result = self._join_key_pairs(
                    self._conjunction_atoms(atom.left),
                    left_qualifiers,
                    right_qualifiers,
                    left_columns,
                    right_columns,
                )
                right_result = self._join_key_pairs(
                    self._conjunction_atoms(atom.right),
                    left_qualifiers,
                    right_qualifiers,
                    left_columns,
                    right_columns,
                )
                common = [
                    pair
                    for pair in left_result.key_pairs
                    if pair in right_result.key_pairs
                ]
                pairs.extend(pair for pair in common if pair not in pairs)
                residual.append(atom)
                continue
            refs: list[ColumnRef] = []
            if _collect_column_ref_nodes(atom, refs) or not refs:
                residual.append(atom)
                continue
            if not (isinstance(atom, BinaryOp) and atom.operator == "="):
                residual.append(atom)
                continue
            if not (
                isinstance(atom.left, ColumnRef) and isinstance(atom.right, ColumnRef)
            ):
                residual.append(atom)
                continue
            left_side = self._column_side(
                atom.left,
                left_qualifiers,
                right_qualifiers,
                left_columns,
                right_columns,
            )
            right_side = self._column_side(
                atom.right,
                left_qualifiers,
                right_qualifiers,
                left_columns,
                right_columns,
            )
            if left_side == "left" and right_side == "right":
                pairs.append((atom.left.name.lower(), atom.right.name.lower()))
            elif left_side == "right" and right_side == "left":
                pairs.append((atom.right.name.lower(), atom.left.name.lower()))
            else:
                residual.append(atom)
        return _JoinKeyInference(tuple(pairs), tuple(residual))

    @staticmethod
    def _column_side(
        column: ColumnRef,
        left_qualifiers: set[str],
        right_qualifiers: set[str],
        left_columns: set[str],
        right_columns: set[str],
    ) -> str | None:
        """列引用属于连接哪一侧；两侧都可能（歧义）或找不到时返回 None。"""

        qualifier = (column.table or "").lower()
        name = column.name.lower()
        if qualifier:
            if qualifier in left_qualifiers and qualifier not in right_qualifiers:
                return "left"
            if qualifier in right_qualifiers and qualifier not in left_qualifiers:
                return "right"
            return None
        in_left, in_right = name in left_columns, name in right_columns
        if in_left and not in_right:
            return "left"
        if in_right and not in_left:
            return "right"
        return None

    @staticmethod
    def _compile_key_extractor(
        pairs: tuple[tuple[str, str], ...],
        reference: TableRef,
        *,
        side: str,
    ) -> Callable[[_RowContext], tuple[object, ...]]:
        """把连接键编译成从上下文取值的闭包；side 决定取 left 还是 right 列。"""

        alias = (reference.alias or reference.name).lower()
        table_name = reference.name.lower()
        index = 0 if side == "left" else 1
        keys = tuple(f"{alias}.{pair[index]}" for pair in pairs)
        fallback = tuple(f"{table_name}.{pair[index]}" for pair in pairs)
        bare = tuple(pair[index] for pair in pairs)

        def extract(context: _RowContext) -> tuple[object, ...]:
            """从当前输入提取调用方需要的信息。"""
            values = []
            for primary, secondary, plain in zip(keys, fallback, bare, strict=True):
                value = context.get(primary, _MISSING)
                if value is _MISSING:
                    value = context.get(secondary, _MISSING)
                if value is _MISSING:
                    # HOW：逗号连接的连接列常不带限定名，此时上下文里只有裸列名。
                    value = context.get(plain, _MISSING)
                values.append(value)
            return tuple(values)

        return extract

    def _choose_join_strategy(
        self,
        pairs: list[tuple[str, str]],
        right_rows: int,
        left_rows: int | None,
        index_metadata: IndexMetadata | None,
    ) -> str:
        """按实测代价选择连接策略：hash / index / nested_loop。"""

        if not pairs:
            return "nested_loop"
        right_count = max(1, right_rows)
        left_count = max(1, left_rows or right_count)
        hash_cost = (
            right_count * _JOIN_HASH_BUILD_COST + left_count * _JOIN_HASH_PROBE_COST
        )
        hash_fits = right_count * _JOIN_CONTEXT_BYTES <= _JOIN_HASH_MEMORY_BUDGET
        nested_cost = left_count * right_count * _JOIN_NESTED_LOOP_PAIR_COST
        if hash_fits and hash_cost <= nested_cost:
            return "hash"
        if index_metadata is not None:
            index_cost = left_count * _JOIN_INDEX_LOOKUP_COST
            if index_cost < min(hash_cost if hash_fits else nested_cost, nested_cost):
                return "index"
        return "nested_loop"

    def _hash_join(
        self,
        left_contexts: Iterable[_RowContext],
        right: list[_RowContext],
        *,
        pairs: list[tuple[str, str]],
        residual: Callable[[_RowContext], object] | None,
        left_only: Callable[[_RowContext], object] | None,
        join_type: str,
        join_table: TableRef,
        from_table: TableRef,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> Iterable[_RowContext]:
        """哈希连接：右表（建侧）建哈希表，左表流式探测；NULL 键永不匹配。"""

        right_key = self._compile_key_extractor(tuple(pairs), join_table, side="right")
        left_key = (
            self._compile_key_extractor(tuple(pairs), from_table, side="left")
            if len(pairs)
            else None
        )
        buckets: dict[tuple[object, ...], list[tuple[int, _RowContext]]] = {}
        for index, context in enumerate(right):
            key = right_key(context)
            if any(value is None or value is _MISSING for value in key):
                continue
            buckets.setdefault(key, []).append((index, context))
        matched_right: set[int] = set()
        null_right: list[_RowContext] | None = None
        for left_context in left_contexts:
            if left_only is not None and not sql_truth(left_only(left_context)):
                continue
            key = left_key(left_context) if left_key is not None else ()
            matched = False
            if not any(value is None or value is _MISSING for value in key):
                for index, right_context in buckets.get(key, []):
                    merged = self._merge_context(left_context, right_context)
                    if residual is None or sql_truth(residual(merged)):
                        matched = True
                        matched_right.add(index)
                        yield merged
            if not matched and join_type == "LEFT":
                if null_right is None:
                    null_right = [
                        self._null_context(
                            join_table, needed=needed, row_order=row_order
                        )
                    ]
                yield self._merge_context(left_context, null_right[0])
        if join_type in {"RIGHT", "FULL"}:
            null_left: _RowContext | None = None
            for index, right_context in enumerate(right):
                if index in matched_right:
                    continue
                if null_left is None:
                    null_left = self._null_context(
                        from_table, needed=needed, row_order=row_order
                    )
                yield self._merge_context(null_left, right_context)

    def _index_join(
        self,
        left_contexts: Iterable[_RowContext],
        *,
        pairs: list[tuple[str, str]],
        metadata: IndexMetadata,
        join_reference: TableRef,
        from_table: TableRef,
        table: TableMetadata,
        residual: Callable[[_RowContext], object] | None,
        left_only: Callable[[_RowContext], object] | None,
        join_type: str,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> Iterable[_RowContext]:
        """索引嵌套循环：左表每行用连接键去右表索引上等值查找。

        HOW：仅支持 INNER/LEFT（RIGHT/FULL 需要知道哪些右行未匹配，交给哈希连接或嵌套循环）。
        """

        left_key = self._compile_key_extractor(tuple(pairs), from_table, side="left")
        columns = [name.lower() for name in metadata.columns[: len(pairs)]]
        right_template = self._context_template(
            join_reference, table, needed, row_order
        )
        heap = self._heap(table)
        null_right: _RowContext | None = None
        for left_context in left_contexts:
            if left_only is not None and not sql_truth(left_only(left_context)):
                continue
            key = left_key(left_context)
            matched = False
            if not any(value is None or value is _MISSING for value in key):
                constraints = {
                    column: _IndexConstraint(allowed=[value])
                    for column, value in zip(columns, key, strict=True)
                }
                candidates = self._scan_index_candidates(metadata, constraints) or ()
                for row_id in candidates:
                    row = heap.read(row_id)
                    if row is None:
                        continue
                    right_context = self._table_context(
                        join_reference, row, row_id, table, template=right_template
                    )
                    merged = self._merge_context(left_context, right_context)
                    if residual is None or sql_truth(residual(merged)):
                        matched = True
                        yield merged
            if not matched and join_type == "LEFT":
                if null_right is None:
                    null_right = self._null_context(
                        join_reference, needed=needed, row_order=row_order
                    )
                yield self._merge_context(left_context, null_right)

    def _stream_join(
        self,
        left_contexts: Iterable[_RowContext],
        right: list[_RowContext],
        *,
        join_type: str,
        join_table: TableRef,
        from_table: TableRef,
        condition: Callable[[_RowContext], object] | None,
        needed: frozenset[str] | None,
        row_order: bool,
        trace: ExecutionTrace | None,
    ) -> Iterable[_RowContext]:
        """流式连接：左表逐行拉取，右表已物化；RIGHT/FULL 在末尾补未匹配的右行。"""

        matched_right: set[int] = set() if join_type in {"RIGHT", "FULL"} else set()
        for left_context in left_contexts:
            matched = False
            for index, right_context in enumerate(right):
                if trace is not None:
                    trace.step()
                merged = self._merge_context(left_context, right_context)
                if condition is None or sql_truth(condition(merged)):
                    if matched_right is not None:
                        matched_right.add(index)
                    matched = True
                    yield merged
            if not matched and join_type == "LEFT":
                yield self._merge_context(
                    left_context,
                    self._null_context(join_table, needed=needed, row_order=row_order),
                )
        if join_type in {"RIGHT", "FULL"}:
            null_left = None
            for index, right_context in enumerate(right):
                if index in matched_right:
                    continue
                if null_left is None:
                    null_left = self._null_context(
                        from_table, needed=needed, row_order=row_order
                    )
                yield self._merge_context(null_left, right_context)

    def _table_prefilter(
        self,
        atoms: tuple[Expr, ...],
        reference: TableRef,
    ) -> _TablePrefilter:
        """抽出只引用单表的 AND 原子，编译成该表扫描用的下推过滤。

        WHY：带 JOIN 时原实现只在连接后过滤 WHERE，导致左表全量参与嵌套循环
        （实测 20 客户 × 15,000 订单的聚合要 234 s）；按表下推后只剩真正需要的行。
        """

        try:
            relation = self.catalog.get_relation(reference.name)
        except CatalogError:
            return _TablePrefilter(None, frozenset())
        qualifiers = {reference.name.lower(), (reference.alias or "").lower()} - {""}
        column_names = {column.name.lower() for column in relation.schema}
        picked: list[Expr] = []
        needed: set[str] = set()
        for atom in atoms:
            refs: list[ColumnRef] = []
            if _collect_column_ref_nodes(atom, refs):
                # 含子查询的原子不下推：子查询可能引用其它表。
                continue
            if not refs:
                continue
            belongs = True
            for ref in refs:
                if ref.table:
                    if ref.table.lower() not in qualifiers:
                        belongs = False
                        break
                elif ref.name.lower() not in column_names:
                    belongs = False
                    break
                needed.add(ref.name.lower())
            if belongs:
                picked.append(atom)
        if not picked:
            return _TablePrefilter(None, frozenset())
        predicate: Expr = picked[0]
        for extra in picked[1:]:
            predicate = BinaryOp(predicate, "AND", extra)
        return _TablePrefilter(
            self._compile_expr(self._fold_constants(predicate)),
            frozenset(needed),
        )

    # HOW：从目录取得表和堆，根据访问路径读取记录，建立列名到字段值的查询上下文。
    def _scan_contexts(#找符合条件的记录
        self,
        reference: TableRef,
        predicate: Expr | None,
        *,
        scan_plan: _PlanNodeLike | None = None,
        allow_system_tables: bool = False,
        needed: frozenset[str] | None = None,
        row_order: bool = True,
        prefilter: Callable[[_RowLookup], object] | None = None,
        prefilter_needed: frozenset[str] | None = None,
    ) -> Iterable[_RowContext]:
        """扫描一张表或视图，产出逐行上下文。

        HOW：传入 `prefilter` 时先用只读行视图过滤（WHERE 只涉及本表的情况），
        不通过的行根本不会构建完整上下文，全表扫描的分配成本随之下降。
        """
        trace = current_trace.get()
        try:
            relation = self.catalog.get_relation(reference.name)
        except CatalogError:
            if not allow_system_tables:
                raise
            # HOW：只有系统视图的内部定义允许回读隐藏权限表，普通 SQL 仍由 Binder 拒绝。
            relation = self.catalog.get_table(reference.name, include_system=True)
        if isinstance(relation, ViewMetadata):
            # HOW：逻辑视图先执行定义查询得到内存行，再由外层 SELECT 继续过滤/连接。
            result = self._execute_select(
                self._view_query(relation),
                relation.schema.names(),
                allow_system_tables=relation.system,
            )
            if trace is not None:
                trace.scans.append(
                    {
                        "table": relation.name,
                        "operator": "ViewScan",
                        "candidate_rows": len(result.rows),
                    }
                )
            template = self._context_template(reference, relation, needed, row_order)
            view = (
                self._context_template(reference, relation, prefilter_needed, False)
                if prefilter is not None
                else None
            )
            for slot_id, row in enumerate(result.rows):
                if trace is not None:
                    trace.step()
                typed = tuple(row)
                if (
                    view is not None
                    and prefilter is not None
                    and not sql_truth(prefilter(_RowView(view.lookup, typed)))
                ):
                    continue
                yield self._table_context(
                    reference,
                    typed,
                    RowId(PageId(-1), slot_id),
                    relation,
                    template=template,
                )
            return

        table = relation
        heap = self._heap(table)
        # WHY：计划下推的谓词可能仍带 `DATE('1994-01-01')` 这类可折叠调用，而候选集抽取只认字面量；
        # 不先折叠会让按字符串列范围建的索引直接失效（降级为 SeqScan）。
        effective_predicate = self._fold_constants(
            self._scan_predicate(scan_plan, predicate)
        )
        candidates = None
        if scan_plan is None or scan_plan.kind == "IndexScan":
            candidates = self._candidate_row_ids(table, reference, effective_predicate)
        if trace is not None:
            trace.scans.append(
                {
                    "table": table.name,
                    "operator": "SeqScan" if candidates is None else "IndexScan",
                    "candidate_rows": None if candidates is None else len(candidates),
                }
            )
        if candidates is None:
            # HOW：没有索引候选集才全表扫描；空候选集表示无候选记录，不会回退全表扫描。
            rows = heap.scan()
        else:
            rows = ((row_id, heap.read(row_id)) for row_id in candidates)
        template = self._context_template(reference, table, needed, row_order)
        view = (
            self._context_template(reference, table, prefilter_needed, False)
            if prefilter is not None
            else None
        )
        for row_id, row in rows:
            if trace is not None:
                trace.step()
            if row is None:
                continue
            if (
                view is not None
                and prefilter is not None
                and not sql_truth(prefilter(_RowView(view.lookup, row)))
            ):
                continue
            yield self._table_context(reference, row, row_id, table, template=template)

    @staticmethod
    def _scan_plans(plan: _PlanNodeLike | None) -> list[_PlanNodeLike]:
        """按输入顺序提取计划中的扫描节点，供 AST evaluator 使用。"""

        if plan is None:
            return []
        result: list[_PlanNodeLike] = []
        if plan.kind in {"SeqScan", "IndexScan"}:
            result.append(plan)
        for child in plan.children:
            result.extend(QueryExecutionMixin._scan_plans(child))
        return result

    @staticmethod
    def _plan_contains_kind(plan: _PlanNodeLike | None, kind: str) -> bool:
        """递归判断计划树是否包含指定算子类型。"""
        if plan is None:
            return False
        return plan.kind == kind or any(
            QueryExecutionMixin._plan_contains_kind(child, kind)
            for child in plan.children
        )

    @staticmethod
    def _scan_predicate(
        scan_plan: _PlanNodeLike | None, fallback: Expr | None
    ) -> Expr | None:
        """提取扫描节点上可直接执行的谓词。"""
        if scan_plan is not None:
            pushed = scan_plan.properties.get("pushed_predicate")
            if isinstance(pushed, Expr):
                return pushed
        return fallback

    @staticmethod
    def _plan_uses_index(plan: _PlanNodeLike) -> bool:
        """判断计划树是否使用索引访问。"""
        return any(
            node.kind == "IndexScan" for node in QueryExecutionMixin._scan_plans(plan)
        )

    # ----- 索引候选集与覆盖索引 -----
    def _candidate_row_ids(
        self, table: TableMetadata, reference: TableRef, predicate: Expr | None
    ) -> tuple[RowId, ...] | None:
        """根据扫描计划计算候选行号。"""
        if predicate is None:
            return None
        if isinstance(predicate, BetweenPredicate) and predicate.negated:
            # WHY：NOT BETWEEN 是两个可分别定位的范围；直接把它当成不支持
            # 的谓词会错过索引，而把整个结果取反又无法避免顺序扫描。
            return self._candidate_row_ids(
                table,
                reference,
                BinaryOp(
                    BinaryOp(predicate.expression, "<", predicate.lower),
                    "OR",
                    BinaryOp(predicate.expression, ">", predicate.upper),
                ),
            )
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "OR":
            left = self._candidate_row_ids(table, reference, predicate.left)
            right = self._candidate_row_ids(table, reference, predicate.right)
            if left is None or right is None:
                return None
            return tuple(sorted(set(left) | set(right)))
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            atoms = self._conjunction_atoms(predicate)
            combined = self._candidate_for_atoms(table, reference, atoms)
            if combined is not None:
                return combined
            # HOW：当 AND 的一侧仍是 OR 时，先分别取得可用候选集；
            # 这样 ``(a = 1 OR a = 2) AND b = 3`` 至少能利用 a 的索引。
            left = self._candidate_row_ids(table, reference, predicate.left)
            right = self._candidate_row_ids(table, reference, predicate.right)
            if left is None:
                return right
            if right is None:
                return left
            return tuple(sorted(set(left) & set(right)))
        else:
            atoms = (predicate,)
        return self._candidate_for_atoms(table, reference, atoms)

    def _candidate_for_atoms(
        self,
        table: TableMetadata,
        reference: TableRef,
        atoms: tuple[Expr, ...],
    ) -> tuple[RowId, ...] | None:
        """按索引元数据计算一组 AND 条件的候选 RowId 交集（带缓存）。"""

        per_index: list[_IndexCandidateSet] = []
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id:
                continue
            constraints = self._constraints_for_atoms(reference, atoms)
            signature = self._index_constraint_signature(metadata, constraints)
            per_index.append(
                _IndexCandidateSet(
                    signature,
                    self._index_candidates_for_atoms(
                        table, reference, metadata, atoms
                    ),
                )
            )
        if not any(item.row_ids is not None for item in per_index):
            return None
        # HOW：交集按“参与索引的约束组合”缓存；任一写操作都会清空整个缓存。
        intersection_key = ("intersection",) + tuple(
            sorted(item.signature for item in per_index)
        )
        if intersection_key in self._candidate_cache:
            return self._candidate_cache[intersection_key]
        candidates = [item.row_ids for item in per_index if item.row_ids is not None]
        result = set(candidates[0])
        for current in candidates[1:]:
            result.intersection_update(current)
        merged = tuple(sorted(result))
        self._candidate_cache[intersection_key] = merged
        return merged

    @staticmethod
    def _constraints_for_atoms(
        reference: TableRef,
        atoms: tuple[Expr, ...],
    ) -> dict[str, "_IndexConstraint"]:
        """把 AND 原子归并成每列一个约束；与实例方法共用同一套转换规则。"""

        constraints: dict[str, _IndexConstraint] = {}
        for atom in atoms:
            parsed = QueryExecutionMixin._index_atom_constraint(atom, reference)
            if parsed is None:
                continue
            current = constraints.setdefault(parsed.column, _IndexConstraint())
            incoming = parsed.constraint
            if incoming.allowed is not None:
                current.allowed = QueryExecutionMixin._merge_allowed(
                    current.allowed, incoming.allowed
                )
            if incoming.lower is not None:
                current.lower = QueryExecutionMixin._merge_lower(
                    current.lower, incoming.lower
                )
            if incoming.upper is not None:
                current.upper = QueryExecutionMixin._merge_upper(
                    current.upper, incoming.upper
                )
            current.not_null = current.not_null or incoming.not_null
        return constraints

    @staticmethod
    def _index_constraint_signature(
        metadata: IndexMetadata,
        constraints: dict[str, "_IndexConstraint"],
    ) -> tuple[object, ...]:
        """约束签名：同签名的索引查询可共用候选集。"""

        return (metadata.name,) + tuple(
            sorted(
                (
                    column,
                    None if constraint.allowed is None else tuple(constraint.allowed),
                    constraint.lower,
                    constraint.upper,
                    constraint.not_null,
                )
                for column, constraint in constraints.items()
            )
        )

    @staticmethod
    def _conjunction_atoms(predicate: Expr) -> tuple[Expr, ...]:
        """展开 AND，便于把多个条件组合成联合索引的连续前缀。"""

        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return (
                *QueryExecutionMixin._conjunction_atoms(predicate.left),
                *QueryExecutionMixin._conjunction_atoms(predicate.right),
            )
        return (predicate,)

    @staticmethod
    def _constant_expression(expression: Expr) -> ConstantValue:
        """提取索引边界所需的常量，也覆盖负数等一元字面量。"""

        return constant_value(expression)

    @staticmethod
    def _same_index_value(left: object, right: object) -> bool:
        """比较两个值是否可作为同一个索引键值。"""
        if left is None or right is None:
            return left is None and right is None
        return compare_values(left, right, "=") is True

    @classmethod
    def _compare_index_values(cls, left: object, right: object) -> int:
        """按索引排序规则比较两个值。"""
        if cls._same_index_value(left, right):
            return 0
        return -1 if compare_values(left, right, "<") is True else 1

    @staticmethod
    def _merge_allowed(
        existing: list[object] | None, incoming: list[object]
    ) -> list[object]:
        """合并两个索引边界时判断端点是否仍然有效。"""
        if existing is None:
            return list(incoming)
        return [
            value
            for value in existing
            if any(
                QueryExecutionMixin._same_index_value(value, candidate)
                for candidate in incoming
            )
        ]

    @classmethod
    def _merge_lower(
        cls,
        existing: _IndexBoundary | None,
        incoming: _IndexBoundary,
    ) -> _IndexBoundary:
        """合并两个下界并保留更严格的边界。"""
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming.value, existing.value)
        if comparison > 0:
            return incoming
        if comparison < 0:
            return existing
        return incoming if not incoming.inclusive else existing

    @classmethod
    def _merge_upper(
        cls,
        existing: _IndexBoundary | None,
        incoming: _IndexBoundary,
    ) -> _IndexBoundary:
        """合并两个上界并保留更严格的边界。"""
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming.value, existing.value)
        if comparison < 0:
            return incoming
        if comparison > 0:
            return existing
        return incoming if not incoming.inclusive else existing

    @staticmethod
    def _index_column(column: ColumnRef, reference: TableRef) -> str | None:
        """判断列引用是否对应目标表的索引列。"""
        if column.table and column.table.lower() not in {
            reference.name.lower(),
            (reference.alias or "").lower(),
        }:
            return None
        return column.name.lower()

    @staticmethod
    def _index_atom_constraint(
        atom: Expr,
        reference: TableRef,
    ) -> _IndexConstraintMatch | None:
        """把一个谓词转换成单列约束；无法安全定位时返回 None。"""

        if isinstance(atom, BinaryOp):
            operator = atom.operator.upper()
            left_column = atom.left if isinstance(atom.left, ColumnRef) else None
            right_column = atom.right if isinstance(atom.right, ColumnRef) else None
            if left_column is not None and right_column is None:
                column = QueryExecutionMixin._index_column(left_column, reference)
                constant = QueryExecutionMixin._constant_expression(atom.right)
            elif right_column is not None and left_column is None:
                column = QueryExecutionMixin._index_column(right_column, reference)
                constant = QueryExecutionMixin._constant_expression(atom.left)
                if operator in {"<", "<=", ">", ">="}:
                    operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[operator]
            else:
                return None
            if column is None or not constant.found:
                return None
            value = constant.value
            if operator == "LIKE":
                if not isinstance(value, str) or any(
                    marker in value for marker in ("%", "_")
                ):
                    return None
                operator = "="
            if operator == "=":
                return _IndexConstraintMatch(column, _IndexConstraint(allowed=[value]))
            if operator in {"<", "<=", ">", ">="}:
                boundary = _IndexBoundary(value, operator in {">=", "<="})
                return _IndexConstraintMatch(column, _IndexConstraint(
                    lower=boundary if operator in {">", ">="} else None,
                    upper=boundary if operator in {"<", "<="} else None,
                ))
            return None

        if isinstance(atom, IsNull) and isinstance(atom.expression, ColumnRef):
            column = QueryExecutionMixin._index_column(atom.expression, reference)
            if column is None:
                return None
            return _IndexConstraintMatch(
                column,
                _IndexConstraint(
                    allowed=None if atom.negated else [None],
                    not_null=atom.negated,
                ),
            )

        if (
            isinstance(atom, InPredicate)
            and not atom.negated
            and isinstance(atom.expression, ColumnRef)
        ):
            column = QueryExecutionMixin._index_column(atom.expression, reference)
            if column is None:
                return None
            values: list[object] = []
            for expression in atom.values:
                constant = QueryExecutionMixin._constant_expression(expression)
                if not constant.found:
                    return None
                values.append(constant.value)
            return _IndexConstraintMatch(column, _IndexConstraint(allowed=values))

        if (
            isinstance(atom, BetweenPredicate)
            and not atom.negated
            and isinstance(atom.expression, ColumnRef)
        ):
            column = QueryExecutionMixin._index_column(atom.expression, reference)
            if column is None:
                return None
            lower_constant = QueryExecutionMixin._constant_expression(atom.lower)
            upper_constant = QueryExecutionMixin._constant_expression(atom.upper)
            if not lower_constant.found or not upper_constant.found:
                return None
            return _IndexConstraintMatch(
                column,
                _IndexConstraint(
                    lower=_IndexBoundary(lower_constant.value, True),
                    upper=_IndexBoundary(upper_constant.value, True),
                ),
            )
        return None

    def _index_candidates_for_atoms(
        self,
        table: TableMetadata,
        reference: TableRef,
        metadata: IndexMetadata,
        atoms: tuple[Expr, ...],
    ) -> tuple[RowId, ...] | None:
        """单索引候选集（带约束签名缓存）。"""

        constraints = self._constraints_for_atoms(reference, atoms)
        signature = self._index_constraint_signature(metadata, constraints)
        if signature in self._candidate_cache:
            return self._candidate_cache[signature]
        result = self._scan_index_candidates(metadata, constraints)
        self._candidate_cache[signature] = result
        return result

    def _scan_index_candidates(
        self,
        metadata: IndexMetadata,
        constraints: dict[str, _IndexConstraint],
    ) -> tuple[RowId, ...] | None:
        """沿索引列从左往右取候选 RowId；无可用前导约束时返回 None。"""

        tree = self.index_manager.get(metadata.name)
        columns = tuple(column.lower() for column in metadata.columns)

        def scan(position: int, prefix: tuple[object, ...]) -> tuple[RowId, ...] | None:
            """按页和槽顺序扫描输入中的有效记录。"""
            if position >= len(columns):
                return tree.search(prefix)
            constraint = constraints.get(columns[position])
            if constraint is None:
                entries = tree.prefix_scan(prefix) if prefix else ()
                return (
                    tuple(entry.row_id for entry in entries) if prefix else None
                )

            allowed = constraint.allowed
            if allowed is not None:
                values = [
                    value
                    for value in allowed
                    if not constraint.not_null or value is not None
                ]
                if not values:
                    return ()
                result: set[RowId] = set()
                for value in values:
                    if constraint.lower is not None:
                        comparison = self._compare_index_values(
                            value, constraint.lower.value
                        )
                        if comparison < 0 or (
                            comparison == 0 and not constraint.lower.inclusive
                        ):
                            continue
                    if constraint.upper is not None:
                        comparison = self._compare_index_values(
                            value, constraint.upper.value
                        )
                        if comparison > 0 or (
                            comparison == 0 and not constraint.upper.inclusive
                        ):
                            continue
                    nested = scan(position + 1, (*prefix, value))
                    if nested is None:
                        entries = tree.prefix_scan((*prefix, value))
                        return tuple(entry.row_id for entry in entries)
                    result.update(nested)
                return tuple(sorted(result))

            if (
                constraint.lower is None
                and constraint.upper is None
                and not constraint.not_null
            ):
                return tree.prefix_scan(prefix) if prefix else None
            if constraint.lower is not None and constraint.lower.value is None:
                return ()
            if constraint.upper is not None and constraint.upper.value is None:
                return ()
            entries = tree.range_scan_prefix(
                prefix,
                constraint.lower.value if constraint.lower is not None else None,
                constraint.upper.value if constraint.upper is not None else None,
                include_low=constraint.lower.inclusive
                if constraint.lower is not None
                else True,
                include_high=constraint.upper.inclusive
                if constraint.upper is not None
                else True,
            )
            if constraint.not_null:
                entries = tuple(
                    entry
                    for entry in entries
                    if len(entry.key) > position and entry.key[position] is not None
                )
            return tuple(entry.row_id for entry in entries)

        has_leading_constraint = bool(columns) and columns[0] in constraints
        if not has_leading_constraint:
            return None
        return scan(0, ())

    def _index_only_contexts(
        self,
        reference: TableRef,
        where: Expr | None,
        needed: frozenset[str] | None,
    ) -> list[_RowContext] | None:
        """用覆盖索引直接产出逐行上下文，完全不读堆页。

        HOW：启用条件——单表查询；被引用列全部落在某个索引的（键列 + INCLUDE 列）内；
        首列有可用约束；且页级代价模型认为回表/扫描值得（候选集命中缓存，遍历成本已摊薄）。
        """

        if not needed:
            return None
        try:
            relation = self.catalog.get_relation(reference.name)
        except CatalogError:
            return None
        if not isinstance(relation, TableMetadata):
            return None
        atoms = self._conjunction_atoms(where) if where is not None else ()
        template = self._context_template(reference, relation, needed, False)
        for metadata in self.catalog.indexes():
            if metadata.table_id != relation.table_id:
                continue
            covered = {column.lower() for column in metadata.columns} | {
                column.lower() for column in metadata.payload_columns
            }
            if not needed <= covered:
                continue
            constraints = self._constraints_for_atoms(reference, atoms)
            bounds = self._leading_probe_bounds(
                constraints.get(metadata.columns[0].lower())
            )
            if bounds is None:
                continue
            candidates = self._index_candidates_for_atoms(
                relation, reference, metadata, atoms
            )
            if candidates is None or not candidates:
                continue
            if not self.optimizer.should_use_index_only(relation.name, len(candidates)):
                continue
            tree = self.index_manager.get(metadata.name)
            low = bounds.low
            high = bounds.high
            include_low = bounds.include_low
            include_high = bounds.include_high
            # WHY：必须用“前缀位置范围”而不是全键范围。联合索引下 `(1,'paid')` 与上界 `(1,)`
            # 做元组比较会被判为越界，导致等值查询返回空集（实测演示库 `customer_id = 1` 返回 0 行）。
            entries = tree.range_scan_prefix_entries(
                (), low, high, include_low=include_low, include_high=include_high
            )
            key_positions = [
                relation.schema.index(column) for column in metadata.columns
            ]
            payload_positions = [
                relation.schema.index(column) for column in metadata.payload_columns
            ]
            contexts: list[_RowContext] = []
            for entry in entries:
                values: list[object] = [None] * len(relation.schema)
                for value, position in zip(entry.key, key_positions, strict=True):
                    values[position] = value
                for value, position in zip(
                    entry.payload, payload_positions, strict=True
                ):
                    values[position] = value
                contexts.append(
                    self._table_context(
                        reference,
                        tuple(values),
                        entry.row_id,
                        relation,
                        template=template,
                    )
                )
            return contexts
        return None

    @classmethod
    def _leading_probe_bounds(
        cls,
        constraint: _IndexConstraint | None,
    ) -> _IndexProbeBounds | None:
        """把首列约束换成可复用的范围上下界；等值/IN 归为包含端点的区间。"""

        if constraint is None:
            return None
        if constraint.allowed is not None:
            values = [
                value
                for value in constraint.allowed
                if value is not None or not constraint.not_null
            ]
            if not values:
                return None
            ordered = sorted(values, key=cmp_to_key(cls._compare_index_values))
            return _IndexProbeBounds(ordered[0], ordered[-1], True, True)
        low = constraint.lower.value if constraint.lower is not None else None
        high = constraint.upper.value if constraint.upper is not None else None
        if low is None and high is None:
            return None
        include_low = (
            constraint.lower.inclusive if constraint.lower is not None else True
        )
        include_high = (
            constraint.upper.inclusive if constraint.upper is not None else True
        )
        return _IndexProbeBounds(low, high, include_low, include_high)

    # ----- 行上下文裁剪与结果列展开 -----
    def _needed_context_columns(
        self, statement: Select
    ) -> _ContextColumnSelection:
        """收集语句引用到的列名，作为逐行上下文的裁剪依据。

        HOW：`needed=None` 表示退回全列（遇到 `*` 时）；结果对象的 ``row_order``
        表示是否必须构建 `__row_order__`。
        """

        if any(isinstance(item.expression, Star) for item in statement.items):
            return _ContextColumnSelection(None, True)
        sink: set[str] = set()
        found_star = False
        for item in statement.items:
            found_star |= _collect_column_refs(item.expression, sink)
        for expression in statement.group_by:
            found_star |= _collect_column_refs(expression, sink)
        for clause in statement.joins:
            found_star |= _collect_column_refs(clause.on, sink)
        for expression in (statement.where, statement.having):
            found_star |= _collect_column_refs(expression, sink)
        for order_item in statement.order_by:
            found_star |= _collect_column_refs(order_item.expression, sink)
        if found_star:
            # `COUNT(*)` 之类只需行数的聚合不引用具名列，但保守退回全列以免漏掉消费点。
            return _ContextColumnSelection(None, False)
        return _ContextColumnSelection(frozenset(sink), False)

    def _context_template(
        self,
        reference: TableRef,
        table: TableMetadata | ViewMetadata,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> _RowContextTemplate:
        """获取（或建立）行上下文模板；限定名与裸列名去重后保持 schema 顺序。"""

        alias = (reference.alias or reference.name).lower()
        table_name = reference.name.lower()
        key = (alias, table_name, needed, row_order)
        cached = self._context_templates.get(key)
        if cached is not None and cached.schema is table.schema:
            return cached
        keys: list[str] = []
        indices: list[int] = []
        for index, column in enumerate(table.schema):
            column_name = column.name.lower()
            if needed is not None and column_name not in needed:
                continue
            for candidate in (
                f"{alias}.{column_name}",
                f"{table_name}.{column_name}",
                column_name,
            ):
                if candidate not in keys:
                    keys.append(candidate)
                    indices.append(index)
        template = _RowContextTemplate(
            tuple(keys),
            tuple(indices),
            dict(zip(keys, indices, strict=True)),
            alias,
            table_name,
            table.schema,
            {alias: table.schema, table_name: table.schema},
            row_order,
        )
        if len(self._context_templates) > 128:
            self._context_templates.clear()
        self._context_templates[key] = template
        return template

    def _table_context(
        self,
        reference: TableRef,
        row: tuple[object, ...],
        row_id: RowId,
        table: TableMetadata | ViewMetadata,
        *,
        needed: frozenset[str] | None = None,
        row_order: bool = True,
        template: _RowContextTemplate | None = None,
    ) -> _RowContext:
        """构造一行上下文；needed 不为 None 时只把被引用的列放进上下文。"""

        if template is None:
            template = self._context_template(reference, table, needed, row_order)
        context = _RowContext()
        if template.indices:
            picked = itemgetter(*template.indices)(row)
            context = dict(
                zip(
                    template.keys,
                    picked if isinstance(picked, tuple) else (picked,),
                    strict=True,
                )
            )
        context["__row_ids__"] = {template.alias: row_id, template.table_name: row_id}
        # HOW：`__schemas__` 与表结构同生命周期，直接共享同一份只读映射（合并时会重建新字典）。
        context["__schemas__"] = template.schemas
        if template.row_order:
            context["__row_order__"] = [
                _RowOrderEntry(template.alias, column.name, value)
                for column, value in zip(template.schema, row, strict=True)
            ]
        return context

    def _null_context(
        self,
        reference: TableRef,
        *,
        needed: frozenset[str] | None = None,
        row_order: bool = True,
    ) -> _RowContext:
        """为外连接构造一侧列为空的查询上下文。"""
        relation = self.catalog.get_relation(reference.name)
        row = tuple(None for _column in relation.schema)
        return self._table_context(
            reference,
            row,
            RowId(PageId(-1), -1),
            relation,
            needed=needed,
            row_order=row_order,
        )

    def _merge_context(
        self, left: _RowContext, right: _RowContext
    ) -> _RowContext:
        """合并连接两侧的查询上下文。"""
        merged = _RowContext(
            {
                key: value
                for key, value in left.items()
                if key not in {"__row_ids__", "__schemas__", "__row_order__"}
            }
        )
        for key, value in right.items():
            if key not in {"__row_ids__", "__schemas__", "__row_order__"}:
                if key in merged and "." not in key:
                    merged[key] = _AMBIGUOUS
                else:
                    merged[key] = value
        merged["__row_ids__"] = {
            **left.get("__row_ids__", {}),
            **right.get("__row_ids__", {}),
        }
        merged["__schemas__"] = {
            **left.get("__schemas__", {}),
            **right.get("__schemas__", {}),
        }
        merged["__row_order__"] = [
            *left.get("__row_order__", []),
            *right.get("__row_order__", []),
        ]
        return merged

    def _expand_star(
        self, context: _RowContext, table_name: str | None
    ) -> list[object]:
        """将 SELECT * 展开为实际列列表。"""
        result: list[object] = []
        row_order = context.get("__row_order__")
        if not isinstance(row_order, list):
            return result
        for entry in row_order:
            if table_name is None or table_name.lower() == entry.alias.lower():
                result.append(entry.value)
        return result

    def _output_names(self, statement: Select) -> list[str]:
        """计算 SELECT 输出列的名称列表。"""
        names: list[str] = []
        table_refs: list[TableRef] = []
        if statement.from_table is not None:
            table_refs.append(statement.from_table)
        table_refs.extend(join.table for join in statement.joins)
        for item in statement.items:
            if isinstance(item.expression, Star):
                selected = table_refs
                if item.expression.table:
                    selected = [
                        ref
                        for ref in table_refs
                        if ref.name.lower() == item.expression.table.lower()
                        or (ref.alias or "").lower() == item.expression.table.lower()
                    ]
                for ref in selected:
                    relation = self.catalog.get_relation(ref.name)
                    names.extend(column.name for column in relation.schema)
            elif item.alias:
                names.append(item.alias)
            elif isinstance(item.expression, ColumnRef):
                names.append(item.expression.name)
            elif isinstance(item.expression, FunctionCall):
                names.append(item.expression.name.lower())
            else:
                names.append(type(item.expression).__name__.lower())
        return names

    def _uses_index(self, statement: Select) -> bool:
        """判断查询是否存在可用的索引访问路径。"""
        if statement.from_table is None:
            return False
        relation = self.catalog.find_table(
            statement.from_table.name, include_system=True
        )
        if relation is None:
            relation = self.catalog.find_view(statement.from_table.name)
        if not isinstance(relation, TableMetadata):
            return False
        return (
            self._candidate_row_ids(relation, statement.from_table, statement.where)
            is not None
        )

"""SELECT 查询的连接、扫描、索引访问与结果投影。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from functools import cmp_to_key
from itertools import repeat
from operator import itemgetter
from typing import Callable, Iterable, Protocol

from yoursql.common import (
    BinderError,
    CatalogError,
    Column,
    DataType,
    ExecutionError,
    ExecutionResult,
    Schema,
    Value,
    compare_values,
    sql_truth,
)
from yoursql.common.types import PageId, RowId
from yoursql.execution.evaluator import _AGGREGATE_NAMES, _AMBIGUOUS, _MISSING, constant_value
from yoursql.sql.ast import (
    BetweenPredicate,
    BinaryOp,
    CaseExpression,
    ColumnRef,
    ExistsPredicate,
    Expr,
    FunctionCall,
    InPredicate,
    IsNull,
    Literal,
    Select,
    SelectItem,
    Star,
    Subquery,
    TableRef,
    UnaryOp,
)
from yoursql.engine.catalog import IndexMetadata, TableMetadata, ViewMetadata
from yoursql.common.trace import ExecutionTrace, current_trace

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
# HOW：派生表/CTE 没有统计信息，用中性估计值参与连接策略选择。
_DERIVED_ROW_ESTIMATE = 1000


def _key_is_usable(key: tuple[object, ...]) -> bool:
    """连接键是否可用于哈希探测 / 索引查找。

    WHY：NULL 与缺失值天然不匹配；``_AMBIGUOUS`` 表示裸列名在多表合并后归属不明，
    拿它当连接键会静默失配（结果整片归零），必须显式排除。
    """

    return all(
        value is not None and value is not _MISSING and value is not _AMBIGUOUS
        for value in key
    )


class _RowLookup(Protocol):
    """扫描前过滤器所需的最小行访问协议。"""

    def get(self, key: str, default: object = None) -> object: ...


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
        self._lookup = lookup
        self._row = row

    def get(self, key: str, default: object = None) -> object:
        index = self._lookup.get(key)
        return default if index is None else self._row[index]


def _collect_column_ref_nodes(value: object, sink: list[ColumnRef]) -> bool:
    """递归收集表达式里的 ColumnRef 节点；返回是否遇到子查询（子查询不可下推）。"""

    if isinstance(value, ColumnRef):
        sink.append(value)
        return False
    # HOW：EXISTS 与括号子查询一样内嵌一条 SELECT，只能在完整行上下文里求值；
    # 漏判会让它被下推到 `_RowView` 预过滤，那里拿不到外层列也没有 items()。
    if isinstance(value, (Subquery, ExistsPredicate)):
        return True
    if isinstance(value, (list, tuple)):
        # HOW：先递归进序列再判 dataclass——CASE 的 branches 是「元组的元组」，
        # 只按 dataclass 字段浅层遍历会整块跳过分支里的列引用。
        found = False
        for item in value:
            found |= _collect_column_ref_nodes(item, sink)
        return found
    if is_dataclass(value):
        found = False
        for field in fields(value):
            found |= _collect_column_ref_nodes(getattr(value, field.name), sink)
        return found
    return False


def _collect_column_refs(value: object, sink: set[str]) -> bool:
    """递归收集表达式引用的列名（含限定名，均小写），返回是否遇到 `*`。

    HOW：按 dataclass 字段与嵌套序列泛化遍历，新增 AST 节点类型无需同步修改。
    """

    if isinstance(value, ColumnRef):
        name = value.name.lower()
        sink.add(name)
        if value.table:
            sink.add(f"{value.table.lower()}.{name}")
        return False
    if isinstance(value, Star):
        return True
    if isinstance(value, (list, tuple)):
        found = False
        for item in value:
            found |= _collect_column_refs(item, sink)
        return found
    if is_dataclass(value):
        found = False
        for field in fields(value):
            found |= _collect_column_refs(getattr(value, field.name), sink)
        return found
    return False


def _collect_projection_needs(value: object, sink: set[str]) -> bool:
    """收集投影/聚合表达式真正需要的列名；返回是否遇到"展开全部列"的 `*`。

    WHY：`COUNT(*)` 里的 `Star` 只是"数行数"的占位，与 `SELECT *` 不是一回事。
    早先两者共用同一个递归收集器，于是 `SELECT COUNT(*) FROM lineitem` 被判成
    "需要全部列"，每行都要构造 32 键的全列上下文——实测占单次计数全表查询的
    约三成耗时。这里只在进入聚合函数参数时忽略 `Star`，其余位置照旧。
    """

    if isinstance(value, (ColumnRef, Star)):
        # HOW：ColumnRef/Star 本身是 dataclass，必须先拦下，否则会被当成普通节点
        # 展开字段遍历，列名与 `*` 都收集不到。
        return _collect_column_refs(value, sink)
    if isinstance(value, FunctionCall) and value.name.lower() in _AGGREGATE_NAMES:
        found = False
        for argument in value.args:
            if isinstance(argument, Star):
                # COUNT(*) 只数行数，不取任何列值。
                continue
            found |= _collect_column_refs(argument, sink)
        return found
    if isinstance(value, (list, tuple)):
        found = False
        for item in value:
            found |= _collect_projection_needs(item, sink)
        return found
    if is_dataclass(value):
        found = False
        for field in fields(value):
            found |= _collect_projection_needs(getattr(value, field.name), sink)
        return found
    return False


@dataclass(frozen=True)
class _DerivedRelation:
    """派生表/CTE 在内存中的关系描述；只提供 ``schema``，没有数据页。"""

    name: str
    schema: Schema


@dataclass
class _IndexConstraint:
    """单列索引条件的交集；用于按联合索引前缀生成扫描边界。"""

    allowed: list[object] | None = None
    lower: tuple[object, bool] | None = None
    upper: tuple[object, bool] | None = None
    not_null: bool = False


class QueryExecutionMixin:
    """提供 SELECT 查询的执行细节；数据库实例只负责提供目录、存储与生命周期依赖。"""
#是 SELECT 的执行组织函数：它取得符合条件的记录，计算需要输出的内容，
#再处理分组、去重、排序和分页，最后返回查询结果。
    # ----- SELECT 主流程：扫描上下文 -> 聚合/投影 -> 排序分页 -----
    def _execute_select(
        self,
        statement: Select,#已经解析出来的select语句对象
        output_columns: Iterable[str] = (),#上游提供的结果列名
        *,
        plan: _PlanNodeLike | None = None,#执行计划，用于指导扫描，连接等访问方式
        allow_system_tables: bool = False,#是否允许内部访问系统表
        outer: Mapping[str, object] | None = None,#相关子查询需要使用的外层记录值
    ) -> ExecutionResult:
        """执行一条 SELECT。

        HOW：``outer`` 是相关子查询的外层行上下文。传入时每行上下文会把外层值作为
        兜底合并进来，使子查询里引用外层表的列也能求值；不相关子查询不传，零额外开销。
        """

        trace = current_trace.get()
        if trace is not None:
            trace.check()
        previous_scan_kind = self._last_scan_kind
        previous_join_kinds = self._join_kinds
        self._last_scan_kind = None
        self._join_kinds = []
        if statement.union is not None:
            left = self._execute_select(
                replace(statement, union=None),
                output_columns,
                plan=plan,
                allow_system_tables=allow_system_tables,
                outer=outer,
            )
            right = self._execute_select(
                statement.union, output_columns, outer=outer
            )
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
        names = list(output_columns)
        if not names:
            names = self._output_names(statement)
        statement = self._fold_statement(statement)#预先计算可以确定的常量表达式
        # HOW：扫描/连接保持惰性；只有聚合、排序、去重或需要全量结果时才全部消费。
        scanned = 0

        def _count(
            iterable: Iterable[dict[str, object]],
        ) -> Iterable[dict[str, object]]:
            nonlocal scanned
            for item in iterable:
                scanned += 1
                yield item
        #取得查询需要的记录上下文
        raw_contexts: Iterable[dict[str, object]] = self._iter_select_contexts(
            statement,
            plan=plan,
            allow_system_tables=allow_system_tables,
            outer=outer,
        )
        contexts: Iterable[dict[str, object]] = _count(raw_contexts)
        #如果有分组或聚合，先处理他们
        has_aggregate = any(
            self._contains_aggregate(item.expression) for item in statement.items
        ) or self._contains_aggregate(statement.having)
        grouped: Iterable[dict[str, object]]
        if statement.group_by or has_aggregate:
            group_keys = [
                self._compile_expr(expression) for expression in statement.group_by
            ]
            having = self._compile_expr(statement.having)
            groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
            for context in contexts:
                if trace is not None:
                    trace.step()
                key = tuple(expression(context) for expression in group_keys)
                groups.setdefault(key, []).append(context)
            # HOW：只有"无 GROUP BY 的聚合"才在空输入上产出单行；带 GROUP BY 时
            # 没有分组键就没有行（标准 SQL 语义），多造一行 NULL 会污染结果。
            if has_aggregate and not groups and not statement.group_by:
                groups[()] = []
            grouped_rows: list[dict[str, object]] = []
            for group in groups.values():
                base = (
                    dict(group[0])
                    if group
                    else self._empty_group_context(statement)
                )
                base["__group__"] = group
                if statement.having is None or sql_truth(having(base)):
                    grouped_rows.append(base)
            grouped = grouped_rows
        else:
            grouped = contexts#没有group和聚合时，直接让记录继续往下走
        projected: list[
            tuple[tuple[object, ...], dict[str, object], dict[str, object]]
        ] = []#计算select后面要求输出的内容
        item_evaluators = [
            (
                None
                if isinstance(item.expression, Star)
                else self._compile_expr(item.expression),#把表达式准备成可调用的求值函数
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
                value = evaluator(context)
                values.append(value)#逐行计算投影表达式的结果
                if item.alias:
                    aliases[item.alias.lower()] = value
            row_key = tuple(values)
            if statement.distinct:
                if row_key in seen:
                    continue
                seen.add(row_key)#distinct
            projected.append((row_key, context, aliases))
            if stop_after is not None and len(projected) >= stop_after:
                break
        if statement.distinct:
            unique: dict[
                tuple[object, ...],
                tuple[tuple[object, ...], dict[str, object], dict[str, object]],
            ] = {}
            for item in projected:
                unique.setdefault(item[0], item)
            projected = list(unique.values())
        for order_item in reversed(statement.order_by):#排序代码
            evaluator = self._compile_expr(order_item.expression)
            by_alias = (
                isinstance(order_item.expression, ColumnRef)
                and not order_item.expression.table
            )
            alias_key = order_item.expression.name.lower() if by_alias else ""

            def key(
                item: tuple[tuple[object, ...], dict[str, object], dict[str, object]],
            ) -> tuple[int, object]:
                value = item[2].get(alias_key, _MISSING) if by_alias else _MISSING
                if value is _MISSING:
                    value = evaluator(item[1])
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
        if statement.offset:#执行分页
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
            tuple(names), [item[0] for item in projected], stats=stats
        )

    # ----- 扫描与连接上下文 -----
    def _iter_select_contexts(
        self,
        statement: Select,
        *,
        plan: _PlanNodeLike | None = None,
        allow_system_tables: bool = False,
        outer: Mapping[str, object] | None = None,
    ) -> Iterable[dict[str, object]]:
        """产出逐行上下文；``outer`` 非空时同时负责把外层值合并为兜底。

        WHY：外层兜底必须在 WHERE 过滤之前合入。相关子查询的谓词（如
        ``u.id <= t.id``）只有拿到外层值才能求值，而"先过滤再合并"会让
        ``t.id`` 落到本行的同名列上，把相关谓词算成恒真/恒假。
        """

        if self._plan_contains_kind(plan, "EmptyScan"):
            return []
        fallback: dict[str, object] = {}
        if outer:
            # HOW：外层值只作兜底，且不带 ``__`` 前缀的内部键（row_ids/row_order/schemas）。
            fallback = {
                key: value
                for key, value in outer.items()
                if not str(key).startswith("__")
            }

        def _merge(iterable: Iterable[dict[str, object]]) -> Iterable[dict[str, object]]:
            if not fallback:
                return iterable
            return ({**fallback, **context} for context in iterable)

        # HOW：带外层作用域时，按本行视图做的预过滤与覆盖索引直读都不安全——它们拿不到
        # 外层列，会把 `t.id` 解析成本行的 `id`。相关子查询本来就逐行重跑，这里直接放弃下推。
        has_outer = bool(fallback)
        needed, row_order = self._needed_context_columns(statement)
        if statement.from_table is not None and not statement.joins and not has_outer:
            # HOW：覆盖索引直读优先；只在所有被引用列都在索引里时启用，且仍需用 WHERE 过滤残余谓词。
            index_only = self._index_only_contexts(
                statement.from_table, statement.where, needed
            )
            if index_only is not None:
                self._last_scan_kind = "IndexOnlyScan"
                if statement.where is None:
                    return _merge(index_only)
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
            and not has_outer
        ):
            # HOW：含子查询/EXISTS 的谓词不能下推到行视图——它们需要完整上下文才能求值。
            refs: list[ColumnRef] = []
            if not _collect_column_ref_nodes(statement.where, refs):
                where_columns: set[str] = set()
                for ref in refs:
                    where_columns.add(ref.name.lower())
                    if ref.table:
                        where_columns.add(f"{ref.table.lower()}.{ref.name.lower()}")
                prefilter = self._compile_expr(statement.where)
                prefilter_needed = frozenset(where_columns)
        scan_plans = self._scan_plans(plan)
        if statement.from_table is None:
            return _merge([{"__row_order__": [], "__row_ids__": {}, "__schemas__": {}}])
        generated = self._joined_contexts(
            statement,
            scan_plans=scan_plans,
            allow_system_tables=allow_system_tables,
            needed=needed,
            row_order=row_order,
            prefilter=prefilter,
            prefilter_needed=prefilter_needed,
            pushdown=not has_outer,
        )
        generated = _merge(generated)
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
        pushdown: bool = True,
    ) -> Iterable[dict[str, object]]:
        """惰性产出逐行上下文：单表扫描与连接都不再全量物化。

        HOW：左表（及每个连接的左侧）保持流式；右表需要反复扫描，因此只物化右表。
        RIGHT/FULL 需要在连接结束后知道哪些右行未被匹配，这两种连接类型仍会缓存匹配状态。

        ``pushdown=False``（相关子查询带外层作用域时）关闭所有"按本行视图求值"的
        谓词下推：这类下推拿不到外层列，会把 ``t.id`` 当成本行的同名列求值。
        """

        trace = current_trace.get()
        primary_plan = self._plan_for_reference(scan_plans, statement.from_table)
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
            columns = self._relation_columns_of(reference)
            if columns is None:
                continue
            # HOW：只登记生效名（别名优先）。标准 SQL 里别名会遮蔽原表名，绑定层同样只认别名，
            # 两边保持一致才不会出现"绑定到外层、求值取到内层"的错配。
            scope_columns[reference.effective_name.lower()] = columns
        primary_filter: Callable[[_RowLookup], object] | None = None
        primary_needed: frozenset[str] = frozenset()
        if pushdown:
            primary_filter, primary_needed = self._table_prefilter(
                atoms, statement.from_table
            )
        contexts: Iterable[dict[str, object]] = self._scan_contexts(
            statement.from_table,
            self._scan_predicate(primary_plan, statement.where),
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
            join_plan = self._plan_for_reference(scan_plans, join.table)
            join_filter: Callable[[_RowLookup], object] | None = None
            join_needed: frozenset[str] = frozenset()
            if pushdown:
                join_filter, join_needed = self._table_prefilter(atoms, join.table)
            right_qualifiers = {join.table.effective_name.lower()} - {""}
            left_qualifiers = {statement.from_table.effective_name.lower()} - {""}
            for previous in statement.joins[: join_index - 1]:
                left_qualifiers |= {previous.table.effective_name.lower()} - {""}
            on_atoms = (
                self._conjunction_atoms(join.on)
                if join.join_type != "CROSS" and join.on is not None
                else ()
            )
            # WHY：逗号连接（`FROM a, b WHERE a.x = b.y`）会被解析成 CROSS 且 ON 为空，
            # 连接条件全在 WHERE 里；只在 INNER/CROSS 下从 WHERE 推断连接键，
            # 外连接的 WHERE 必须在连接之后生效，不能提升为连接条件。
            inferred = atoms if pushdown and join.join_type in {"INNER", "CROSS"} else ()
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
            left_columns = self._relation_columns_of(statement.from_table) or set()
            for previous in statement.joins[: join_index - 1]:
                left_columns |= self._relation_columns_of(previous.table) or set()
            right_columns = self._relation_columns_of(join.table) or set()
            pairs, residual_atoms = self._join_key_pairs(
                join_atoms,
                left_qualifiers,
                right_qualifiers,
                left_columns,
                right_columns,
                scope_columns,
            )
            right_only: Callable[[_RowLookup], object] | None = None
            right_only_needed: frozenset[str] = frozenset()
            left_only: Callable[[_RowLookup], object] | None = None
            if pushdown:
                right_only, right_only_needed = self._table_prefilter(
                    residual_atoms, join.table
                )
                left_only, _left_needed = self._table_prefilter(
                    residual_atoms, statement.from_table
                )
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
            join_columns = [pair[2] for pair in pairs]
            index_metadata = (
                self._join_index_metadata(join.table, join_columns)
                if join_columns
                else None
            )
            right_rows = self._row_count_of(join.table)
            # HOW：左侧行数按首表统计粗估（左侧可能已被连接放大，这里宁可偏低以便优先选哈希）。
            left_rows = self._row_count_of(statement.from_table)
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

        if reference.is_derived:
            # 派生表没有索引，只能走哈希或嵌套循环。
            return None
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

    def _derived_schema(self, reference: TableRef) -> Schema:
        """派生表/CTE 的输出模式。

        HOW：列名取自子查询的输出名；类型只在能一眼看出时推断（字面量、直接列引用），
        其余按 VARCHAR 占位——派生表只读，类型不参与强制转换，名称与顺序才是关键。
        """

        query = reference.query
        assert query is not None  # noqa: S101 - is_derived 已保证
        names = self._derived_output_names(query)
        columns: list[Column] = []
        for index, (item, name) in enumerate(zip(query.items, names, strict=True)):
            columns.append(Column(name, self._infer_item_type(query, item, index)))
        try:
            return Schema.from_iterable(columns)
        except ValueError as exc:
            raise ExecutionError(
                f"派生表 {reference.effective_name!r} 的输出列名无效: {exc}"
            ) from exc

    def _derived_output_names(self, query: Select) -> list[str]:
        """派生表的输出列名；与 ``_output_names`` 同规则，但 `*` 递归展开内层来源。"""

        names: list[str] = []
        for item in query.items:
            if isinstance(item.expression, Star):
                for reference in self._source_refs(query):
                    if item.expression.table is not None and (
                        reference.effective_name.lower()
                        != item.expression.table.lower()
                    ):
                        continue
                    names.extend(self._relation_schema_of(reference).names())
                continue
            if item.alias:
                names.append(item.alias)
            elif isinstance(item.expression, ColumnRef):
                names.append(item.expression.name)
            else:
                names.append(self._expression_output_name(item.expression))
        return names

    @staticmethod
    def _expression_output_name(expression: Expr) -> str:
        if isinstance(expression, FunctionCall):
            return expression.name.lower()
        if isinstance(expression, CaseExpression):
            return "case"
        if isinstance(expression, Subquery):
            return "subquery"
        return type(expression).__name__.lower()

    @staticmethod
    def _source_refs(query: Select) -> tuple[TableRef, ...]:
        refs: list[TableRef] = []
        if query.from_table is not None:
            refs.append(query.from_table)
        refs.extend(join.table for join in query.joins)
        return tuple(refs)

    def _infer_item_type(self, query: Select, item: SelectItem, index: int) -> DataType:
        """粗略推断派生表一列的类型；推不出来就按 VARCHAR。"""

        expression = item.expression
        if isinstance(expression, Literal):
            inferred = Value.infer(expression.value)
            return DataType.VARCHAR if inferred.data_type is DataType.NULL else inferred.data_type
        if isinstance(expression, ColumnRef):
            for reference in self._source_refs(query):
                try:
                    schema = self._relation_schema_of(reference)
                except (CatalogError, ExecutionError):
                    continue
                try:
                    return schema.column(expression.name).data_type
                except BinderError:
                    continue
        if index is not None and isinstance(expression, (BinaryOp, UnaryOp)):
            return DataType.VARCHAR
        return DataType.VARCHAR

    def _relation_schema_of(self, reference: TableRef) -> Schema:
        """任一 FROM 来源的模式（表 / 视图 / 派生表）。"""

        if reference.is_derived:
            return self._derived_schema(reference)
        return self.catalog.get_relation(reference.name).schema

    def _relation_columns_of(self, reference: TableRef) -> set[str] | None:
        """任一 FROM 来源的列名集合；取不到返回 None。"""

        if reference.is_derived:
            try:
                return {column.name.lower() for column in self._derived_schema(reference)}
            except (ExecutionError, CatalogError, BinderError):
                return None
        return self._relation_columns(reference.name)

    def _row_count_of(self, reference: TableRef) -> int:
        """任一 FROM 来源的行数估计，用于连接策略选择。"""

        if reference.is_derived:
            # HOW：派生表没有统计信息，用一个中性估计值：太小会误选嵌套循环，
            # 太大又会让哈希连接的内存预算判断失真。
            return _DERIVED_ROW_ESTIMATE
        try:
            return int(self.catalog.get_table(reference.name).stats.row_count)
        except CatalogError:
            return _DERIVED_ROW_ESTIMATE

    def _derived_contexts(
        self,
        reference: TableRef,
        *,
        allow_system_tables: bool,
        needed: frozenset[str] | None,
        row_order: bool,
        prefilter: Callable[[_RowLookup], object] | None,
        prefilter_needed: frozenset[str] | None,
    ) -> Iterable[dict[str, object]]:
        """执行派生表/CTE 的子查询，把结果行当作普通行上下文产出。

        HOW：派生表不参与谓词下推（`_table_prefilter` 找不到目录元数据会自动放弃），
        因而这里只需处理「物化 + 按需列裁剪 + 扫描前过滤」。
        """

        schema = self._derived_schema(reference)
        relation = _DerivedRelation(reference.effective_name, schema)
        result = self._execute_select(
            reference.query,
            schema.names(),
            allow_system_tables=allow_system_tables,
        )
        trace = current_trace.get()
        if trace is not None:
            trace.scans.append(
                {
                    "table": reference.effective_name or "<derived>",
                    "operator": "DerivedScan",
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
                reference, typed, RowId(PageId(-1), slot_id), relation, template=template
            )

    def _empty_group_context(self, statement: Select) -> dict[str, object]:
        """空输入聚合的基上下文：所有来源列取 NULL。

        WHY：``SELECT o_year, SUM(...) FROM empty GROUP BY o_year`` 要按标准 SQL 返回
        一行（``NULL`` 分组键 + ``SUM`` 为 NULL），而不是找不到列。此前这里只放内部键，
        一旦 FROM 为空（或派生表筛出 0 行）投影里的列引用就会抛"执行时找不到列"。
        """

        context: dict[str, object] = {"__row_order__": [], "__row_ids__": {}}
        references = [statement.from_table, *(join.table for join in statement.joins)]
        for reference in references:
            if reference is None:
                continue
            try:
                nulls = self._null_context(reference, needed=None, row_order=False)
            except (CatalogError, ExecutionError, BinderError):
                continue
            for key, value in nulls.items():
                if not str(key).startswith("__"):
                    context.setdefault(key, value)
        return context

    def _null_context(
        self,
        reference: TableRef,
        *,
        needed: frozenset[str] | None = None,
        row_order: bool = True,
    ) -> dict[str, object]:
        relation = self._relation_for_reference(reference)
        row = tuple(None for _column in relation.schema)
        return self._table_context(
            reference,
            row,
            RowId(PageId(-1), -1),
            relation,
            needed=needed,
            row_order=row_order,
        )

    def _relation_for_reference(
        self, reference: TableRef
    ) -> TableMetadata | ViewMetadata | _DerivedRelation:
        """把 FROM 来源解析成可提供 ``schema`` 的关系对象。"""

        if reference.is_derived:
            return _DerivedRelation(
                reference.effective_name, self._derived_schema(reference)
            )
        return self.catalog.get_relation(reference.name)

    def _subquery_is_correlated(self, query: Select) -> bool:
        """子查询是否引用外层列（相关子查询）；含嵌套子查询时保守视为相关。"""

        local: set[str] = set()
        local_columns: set[str] = set()
        for reference in (query.from_table, *(join.table for join in query.joins)):
            if reference is None:
                continue
            # HOW：起了别名时原表名在本层不可见，不能再算作"本地"，否则内层
            # ``t AS u`` 会把外层 ``t.id`` 误判成本层引用，相关子查询退化成一次求值。
            local.add(reference.effective_name.lower())
            local_columns |= self._relation_columns_of(reference) or set()
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
        owners: Mapping[str, set[str]] | None = None,
    ) -> tuple[list[tuple[str | None, str, str]], tuple[Expr, ...]]:
        """从连接条件里抽出等值键对（左归属, 左列, 右列），其余原子作为残余谓词。

        HOW：同时处理两种写法——`JOIN ... ON a = b` 与逗号连接 `FROM a, b WHERE a.x = b.y`
        （后者解析成 CROSS 连接，谓词全在 WHERE 里）；未限定的列名用两侧模式列名判定归属。

        WHY：键对必须带上左列的归属限定符。若只记列名，取值时会退化成裸列名，
        而自连接（如 TPC-H Q8 里 `nation AS n1, nation AS n2`）会让裸列名变成
        ``_AMBIGUOUS``，哈希探测静默失配、结果整片归零。``owners`` 是
        「限定符 → 列名集合」映射，用于给未限定列名定出唯一归属。
        """

        pairs: list[tuple[str | None, str, str]] = []
        residual: list[Expr] = []
        for atom in atoms:
            if isinstance(atom, BinaryOp) and atom.operator.upper() == "OR":
                # WHY：像 Q19 那样把连接键写在每个 OR 分支里（`(p_partkey = l_partkey AND ...) OR ...`）时，
                # 「每个分支都要求的等式」一定是匹配行的必要条件，哈希连接不会漏行。
                left_pairs, _left_residual = self._join_key_pairs(
                    self._conjunction_atoms(atom.left),
                    left_qualifiers,
                    right_qualifiers,
                    left_columns,
                    right_columns,
                    owners,
                )
                right_pairs, _right_residual = self._join_key_pairs(
                    self._conjunction_atoms(atom.right),
                    left_qualifiers,
                    right_qualifiers,
                    left_columns,
                    right_columns,
                    owners,
                )
                if left_pairs and right_pairs:
                    # HOW：两侧分支各自都有等值键时，可以改用「所有分支键的并集」做探测键。
                    # 理由：满足任一分支的行，必然在该分支配对列上相等，因此并集探测得到的
                    # 候选集是真实匹配的超集，由残余 OR 谓词做最终判定；反过来若某个分支
                    # 完全没有等值键（如 `a.x = b.y OR b.z > 5`），并集就会漏掉只满足
                    # 该分支的行，此时必须退回「分支共同等式」以保证正确性。
                    candidates = [*left_pairs, *right_pairs]
                else:
                    candidates = [pair for pair in left_pairs if pair in right_pairs]
                pairs.extend(pair for pair in candidates if pair not in pairs)
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
                pairs.append(
                    (
                        self._column_owner(atom.left, left_qualifiers, owners),
                        atom.left.name.lower(),
                        atom.right.name.lower(),
                    )
                )
            elif left_side == "right" and right_side == "left":
                pairs.append(
                    (
                        self._column_owner(atom.right, left_qualifiers, owners),
                        atom.right.name.lower(),
                        atom.left.name.lower(),
                    )
                )
            else:
                residual.append(atom)
        return pairs, tuple(residual)

    @staticmethod
    def _column_owner(
        column: ColumnRef,
        qualifiers: set[str],
        owners: Mapping[str, set[str]] | None,
    ) -> str | None:
        """列引用在指定一侧的归属限定符；无法唯一确定时返回 None。

        HOW：写了限定符就直接用它（``_column_side`` 已确认它在正确的一侧）；
        没写限定符时，用 ``owners`` 找出该侧唯一拥有这一列名的表。
        WHY：归属必须唯一，否则宁可放弃键对（退回残余谓词逐行求值）也不能猜错表。
        """

        if column.table:
            return column.table.lower()
        if not owners:
            return None
        name = column.name.lower()
        matches = [
            qualifier
            for qualifier in sorted(qualifiers)
            if name in {item.lower() for item in owners.get(qualifier, ())}
        ]
        return matches[0] if len(matches) == 1 else None

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
        pairs: tuple[tuple[str | None, str, str], ...],
        reference: TableRef,
        *,
        side: str,
    ) -> Callable[[dict[str, object]], tuple[object, ...]]:
        """把连接键编译成从上下文取值的闭包；side 决定取 left 还是 right 列。

        HOW：左列优先用键对里记录的归属限定符（``pair[0]``）取值；只有归属未知时
        才退回「本侧表名」和裸列名。右列固定属于连接右表，沿用 ``reference``。
        WHY：多表连接（尤其自连接）里裸列名会被 ``_merge_context`` 标成 ``_AMBIGUOUS``，
        拿它当连接键会静默失配，所以限定符优先、裸列名只作最后兜底。
        """

        alias = (reference.alias or reference.name).lower()
        table_name = reference.name.lower()
        if side == "left":
            primary = tuple(
                (f"{pair[0]}.{pair[1]}" if pair[0] else f"{alias}.{pair[1]}")
                for pair in pairs
            )
            fallback = tuple(f"{table_name}.{pair[1]}" for pair in pairs)
            bare = tuple(pair[1] for pair in pairs)
        else:
            primary = tuple(f"{alias}.{pair[2]}" for pair in pairs)
            fallback = tuple(f"{table_name}.{pair[2]}" for pair in pairs)
            bare = tuple(pair[2] for pair in pairs)

        def extract(context: dict[str, object]) -> tuple[object, ...]:
            values = []
            for first, second, plain in zip(primary, fallback, bare, strict=True):
                value = context.get(first, _MISSING)
                if value is _MISSING:
                    value = context.get(second, _MISSING)
                if value is _MISSING:
                    # HOW：逗号连接的连接列常不带限定名，此时上下文里只有裸列名。
                    value = context.get(plain, _MISSING)
                values.append(value)
            return tuple(values)

        return extract

    def _choose_join_strategy(
        self,
        pairs: list[tuple[str | None, str, str]],
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
        left_contexts: Iterable[dict[str, object]],
        right: list[dict[str, object]],
        *,
        pairs: list[tuple[str | None, str, str]],
        residual: Callable[[dict[str, object]], object] | None,
        left_only: Callable[[dict[str, object]], object] | None,
        join_type: str,
        join_table: TableRef,
        from_table: TableRef,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> Iterable[dict[str, object]]:
        """哈希连接：右表（建侧）建哈希表，左表流式探测；NULL 键永不匹配。"""

        right_key = self._compile_key_extractor(tuple(pairs), join_table, side="right")
        left_key = (
            self._compile_key_extractor(tuple(pairs), from_table, side="left")
            if len(pairs)
            else None
        )
        buckets: dict[tuple[object, ...], list[tuple[int, dict[str, object]]]] = {}
        for index, context in enumerate(right):
            key = right_key(context)
            if not _key_is_usable(key):
                continue
            buckets.setdefault(key, []).append((index, context))
        matched_right: set[int] = set()
        null_right: list[dict[str, object]] | None = None
        for left_context in left_contexts:
            if left_only is not None and not sql_truth(left_only(left_context)):
                continue
            key = left_key(left_context) if left_key is not None else ()
            matched = False
            if _key_is_usable(key):
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
            null_left: dict[str, object] | None = None
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
        left_contexts: Iterable[dict[str, object]],
        *,
        pairs: list[tuple[str | None, str, str]],
        metadata: IndexMetadata,
        join_reference: TableRef,
        from_table: TableRef,
        table: TableMetadata,
        residual: Callable[[dict[str, object]], object] | None,
        left_only: Callable[[dict[str, object]], object] | None,
        join_type: str,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> Iterable[dict[str, object]]:
        """索引嵌套循环：左表每行用连接键去右表索引上等值查找。

        HOW：仅支持 INNER/LEFT（RIGHT/FULL 需要知道哪些右行未匹配，交给哈希连接或嵌套循环）。
        """

        left_key = self._compile_key_extractor(tuple(pairs), from_table, side="left")
        columns = [name.lower() for name in metadata.columns[: len(pairs)]]
        right_template = self._context_template(
            join_reference, table, needed, row_order
        )
        heap = self._heap(table)
        null_right: dict[str, object] | None = None
        for left_context in left_contexts:
            if left_only is not None and not sql_truth(left_only(left_context)):
                continue
            key = left_key(left_context)
            matched = False
            if _key_is_usable(key):
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
        left_contexts: Iterable[dict[str, object]],
        right: list[dict[str, object]],
        *,
        join_type: str,
        join_table: TableRef,
        from_table: TableRef,
        condition: Callable[[dict[str, object]], object] | None,
        needed: frozenset[str] | None,
        row_order: bool,
        trace: ExecutionTrace | None,
    ) -> Iterable[dict[str, object]]:
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
    ) -> tuple[Callable[[_RowLookup], object] | None, frozenset[str]]:
        """抽出只引用单表的 AND 原子，编译成该表扫描用的下推过滤。

        WHY：带 JOIN 时原实现只在连接后过滤 WHERE，导致左表全量参与嵌套循环
        （实测 20 客户 × 15,000 订单的聚合要 234 s）；按表下推后只剩真正需要的行。
        """

        try:
            relation = self.catalog.get_relation(reference.name)
        except CatalogError:
            return None, frozenset()
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
            return None, frozenset()
        predicate: Expr = picked[0]
        for extra in picked[1:]:
            predicate = BinaryOp(predicate, "AND", extra)
        return self._compile_expr(self._fold_constants(predicate)), frozenset(needed)

    def _scan_contexts(
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
    ) -> Iterable[dict[str, object]]:
        """扫描一张表或视图，产出逐行上下文。

        HOW：传入 `prefilter` 时先用只读行视图过滤（WHERE 只涉及本表的情况），
        不通过的行根本不会构建完整上下文，全表扫描的分配成本随之下降。
        """
        trace = current_trace.get()
        if reference.is_derived:
            yield from self._derived_contexts(
                reference,
                allow_system_tables=allow_system_tables,
                needed=needed,
                row_order=row_order,
                prefilter=prefilter,
                prefilter_needed=prefilter_needed,
            )
            return
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
        if (
            needed is not None
            and not needed
            and not row_order
            and effective_predicate is None
            and prefilter is None
        ):
            # HOW：既不需要任何列值、也没有谓词时，逐行 JSON 解码与上下文构造全是纯开销；
            # 改为按页槽目录统计活槽个数，再用同一个只读上下文重复产出。
            # WHY：`SELECT COUNT(*) FROM lineitem` / `SELECT 1 FROM lineitem` 这类只关心
            # 行数的全表查询，60,175 行实测 571 ms → 66 ms（口径见
            # benchmarks/bench_scan_paths.py）；需要列值的查询撞的是解码下限，不走这里。
            total = heap.count()
            self._last_scan_kind = "SeqScan"
            if trace is not None:
                trace.scans.append(
                    {
                        "table": table.name,
                        "operator": "SeqScan",
                        "candidate_rows": total,
                    }
                )
            alias = (reference.alias or reference.name).lower()
            yield from repeat(
                {"__row_ids__": {}, "__schemas__": {alias: table.schema}}, total
            )
            return
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
    def _plan_for_reference(
        scan_plans: list[_PlanNodeLike], reference: TableRef | None
    ) -> _PlanNodeLike | None:
        """按表名/别名找扫描节点。

        WHY：原来按下标取 ``scan_plans[join_index]``。一旦 FROM 里出现派生表（它不出现在
        扫描节点列表里），下标就会整体错位，可能把另一张表的下推谓词套到错误的扫描上。
        """

        if reference is None or reference.is_derived or not scan_plans:
            return None
        wanted = {
            reference.name.lower(),
            (reference.alias or "").lower(),
            reference.effective_name.lower(),
        } - {""}
        for node in scan_plans:
            table = node.properties.get("table")
            alias = node.properties.get("alias")
            candidates = {
                str(table).lower() if table is not None else "",
                str(alias).lower() if alias is not None else "",
            } - {""}
            if candidates & wanted:
                return node
        return None

    @staticmethod
    def _plan_contains_kind(plan: _PlanNodeLike | None, kind: str) -> bool:
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
        if scan_plan is not None:
            pushed = scan_plan.properties.get("pushed_predicate")
            if isinstance(pushed, Expr):
                return pushed
        return fallback

    @staticmethod
    def _plan_uses_index(plan: _PlanNodeLike) -> bool:
        return any(
            node.kind == "IndexScan" for node in QueryExecutionMixin._scan_plans(plan)
        )

    # ----- 索引候选集与覆盖索引 -----
    def _candidate_row_ids(
        self, table: TableMetadata, reference: TableRef, predicate: Expr | None
    ) -> tuple[RowId, ...] | None:
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

        per_index: list[tuple[tuple[object, ...], tuple[RowId, ...] | None]] = []
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id:
                continue
            constraints = self._constraints_for_atoms(reference, atoms)
            signature = self._index_constraint_signature(metadata, constraints)
            per_index.append(
                (
                    signature,
                    self._index_candidates_for_atoms(table, reference, metadata, atoms),
                )
            )
        if not any(rows is not None for _signature, rows in per_index):
            return None
        # HOW：交集按“参与索引的约束组合”缓存；任一写操作都会清空整个缓存。
        intersection_key = ("intersection",) + tuple(
            sorted(signature for signature, _rows in per_index)
        )
        if intersection_key in self._candidate_cache:
            return self._candidate_cache[intersection_key]
        candidates = [rows for _signature, rows in per_index if rows is not None]
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
            column, incoming = parsed
            current = constraints.setdefault(column, _IndexConstraint())
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
    def _constant_expression(expression: Expr) -> tuple[bool, object]:
        """提取索引边界所需的常量，也覆盖负数等一元字面量。"""

        return constant_value(expression)

    @staticmethod
    def _same_index_value(left: object, right: object) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return compare_values(left, right, "=") is True

    @classmethod
    def _compare_index_values(cls, left: object, right: object) -> int:
        if cls._same_index_value(left, right):
            return 0
        return -1 if compare_values(left, right, "<") is True else 1

    @staticmethod
    def _merge_allowed(
        existing: list[object] | None, incoming: list[object]
    ) -> list[object]:
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
        existing: tuple[object, bool] | None,
        incoming: tuple[object, bool],
    ) -> tuple[object, bool]:
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming[0], existing[0])
        if comparison > 0:
            return incoming
        if comparison < 0:
            return existing
        return incoming if not incoming[1] else existing

    @classmethod
    def _merge_upper(
        cls,
        existing: tuple[object, bool] | None,
        incoming: tuple[object, bool],
    ) -> tuple[object, bool]:
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming[0], existing[0])
        if comparison < 0:
            return incoming
        if comparison > 0:
            return existing
        return incoming if not incoming[1] else existing

    @staticmethod
    def _index_column(column: ColumnRef, reference: TableRef) -> str | None:
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
    ) -> tuple[str, _IndexConstraint] | None:
        """把一个谓词转换成单列约束；无法安全定位时返回 None。"""

        if isinstance(atom, BinaryOp):
            operator = atom.operator.upper()
            left_column = atom.left if isinstance(atom.left, ColumnRef) else None
            right_column = atom.right if isinstance(atom.right, ColumnRef) else None
            if left_column is not None and right_column is None:
                column = QueryExecutionMixin._index_column(left_column, reference)
                found, value = QueryExecutionMixin._constant_expression(atom.right)
            elif right_column is not None and left_column is None:
                column = QueryExecutionMixin._index_column(right_column, reference)
                found, value = QueryExecutionMixin._constant_expression(atom.left)
                if operator in {"<", "<=", ">", ">="}:
                    operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[operator]
            else:
                return None
            if column is None or not found:
                return None
            if operator == "LIKE":
                if not isinstance(value, str) or any(
                    marker in value for marker in ("%", "_")
                ):
                    return None
                operator = "="
            if operator == "=":
                return column, _IndexConstraint(allowed=[value])
            if operator in {"<", "<=", ">", ">="}:
                boundary = (value, operator in {">=", "<="})
                return column, _IndexConstraint(
                    lower=boundary if operator in {">", ">="} else None,
                    upper=boundary if operator in {"<", "<="} else None,
                )
            return None

        if isinstance(atom, IsNull) and isinstance(atom.expression, ColumnRef):
            column = QueryExecutionMixin._index_column(atom.expression, reference)
            if column is None:
                return None
            return column, _IndexConstraint(
                allowed=None if atom.negated else [None], not_null=atom.negated
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
                found, value = QueryExecutionMixin._constant_expression(expression)
                if not found:
                    return None
                values.append(value)
            return column, _IndexConstraint(allowed=values)

        if (
            isinstance(atom, BetweenPredicate)
            and not atom.negated
            and isinstance(atom.expression, ColumnRef)
        ):
            column = QueryExecutionMixin._index_column(atom.expression, reference)
            if column is None:
                return None
            lower_found, lower = QueryExecutionMixin._constant_expression(atom.lower)
            upper_found, upper = QueryExecutionMixin._constant_expression(atom.upper)
            if not lower_found or not upper_found:
                return None
            return column, _IndexConstraint(lower=(lower, True), upper=(upper, True))
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
            if position >= len(columns):
                return tree.search(prefix)
            constraint = constraints.get(columns[position])
            if constraint is None:
                entries = tree.prefix_scan(prefix) if prefix else ()
                return tuple(row_id for _key, row_id in entries) if prefix else None

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
                            value, constraint.lower[0]
                        )
                        if comparison < 0 or (
                            comparison == 0 and not constraint.lower[1]
                        ):
                            continue
                    if constraint.upper is not None:
                        comparison = self._compare_index_values(
                            value, constraint.upper[0]
                        )
                        if comparison > 0 or (
                            comparison == 0 and not constraint.upper[1]
                        ):
                            continue
                    nested = scan(position + 1, (*prefix, value))
                    if nested is None:
                        entries = tree.prefix_scan((*prefix, value))
                        return tuple(row_id for _key, row_id in entries)
                    result.update(nested)
                return tuple(sorted(result))

            if (
                constraint.lower is None
                and constraint.upper is None
                and not constraint.not_null
            ):
                return tree.prefix_scan(prefix) if prefix else None
            if constraint.lower is not None and constraint.lower[0] is None:
                return ()
            if constraint.upper is not None and constraint.upper[0] is None:
                return ()
            entries = tree.range_scan_prefix(
                prefix,
                constraint.lower[0] if constraint.lower is not None else None,
                constraint.upper[0] if constraint.upper is not None else None,
                include_low=constraint.lower[1]
                if constraint.lower is not None
                else True,
                include_high=constraint.upper[1]
                if constraint.upper is not None
                else True,
            )
            if constraint.not_null:
                entries = tuple(
                    (key, row_id)
                    for key, row_id in entries
                    if len(key) > position and key[position] is not None
                )
            return tuple(row_id for _key, row_id in entries)

        has_leading_constraint = bool(columns) and columns[0] in constraints
        if not has_leading_constraint:
            return None
        return scan(0, ())

    def _index_only_contexts(
        self,
        reference: TableRef,
        where: Expr | None,
        needed: frozenset[str] | None,
    ) -> list[dict[str, object]] | None:
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
            low, high, include_low, include_high = bounds
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
            contexts: list[dict[str, object]] = []
            for key, row_id, payload in entries:
                values: list[object] = [None] * len(relation.schema)
                for value, position in zip(key, key_positions, strict=True):
                    values[position] = value
                for value, position in zip(payload, payload_positions, strict=True):
                    values[position] = value
                contexts.append(
                    self._table_context(
                        reference, tuple(values), row_id, relation, template=template
                    )
                )
            return contexts
        return None

    @classmethod
    def _leading_probe_bounds(
        cls,
        constraint: _IndexConstraint | None,
    ) -> tuple[object | None, object | None, bool, bool] | None:
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
            return ordered[0], ordered[-1], True, True
        low = constraint.lower[0] if constraint.lower is not None else None
        high = constraint.upper[0] if constraint.upper is not None else None
        if low is None and high is None:
            return None
        include_low = constraint.lower[1] if constraint.lower is not None else True
        include_high = constraint.upper[1] if constraint.upper is not None else True
        return low, high, include_low, include_high

    # ----- 行上下文裁剪与结果列展开 -----
    def _needed_context_columns(
        self, statement: Select
    ) -> tuple[frozenset[str] | None, bool]:
        """收集语句引用到的列名，作为逐行上下文的裁剪依据。

        HOW：`needed=None` 表示退回全列（遇到 `*` 时）；第二个返回值表示是否必须构建 `__row_order__`。
        """

        if any(isinstance(item.expression, Star) for item in statement.items):
            return None, True
        sink: set[str] = set()
        found_star = False
        # HOW：投影与 HAVING 走"忽略聚合参数里的 `*`"的收集器——`COUNT(*)` 因此不再被
        # 判成"需要全部列"，列裁剪才会真正收敛到空集；其余子句不可能合法出现聚合。
        for item in statement.items:
            found_star |= _collect_projection_needs(item.expression, sink)
        for expression in statement.group_by:
            found_star |= _collect_column_refs(expression, sink)
        for clause in statement.joins:
            found_star |= _collect_column_refs(clause.on, sink)
        if statement.where is not None:
            found_star |= _collect_column_refs(statement.where, sink)
        if statement.having is not None:
            found_star |= _collect_projection_needs(statement.having, sink)
        for order_item in statement.order_by:
            found_star |= _collect_column_refs(order_item.expression, sink)
        if found_star:
            # 仍遇到定位不到具名列的展开（例如子查询内部的 `*`）：保守退回全列。
            return None, False
        return frozenset(sink), False

    def _context_template(
        self,
        reference: TableRef,
        table: TableMetadata | ViewMetadata,
        needed: frozenset[str] | None,
        row_order: bool,
    ) -> _RowContextTemplate:
        """获取（或建立）行上下文模板；限定名与裸列名去重后保持 schema 顺序。

        HOW：表被起了别名时只登记别名，不再登记原表名。标准 SQL 里别名会让原表名
        在该层不可见；如果两个都登记，相关子查询的 ``t.id``（指外层 t）会被内层
        ``t AS u`` 自己的行覆盖，把相关谓词算成恒真/恒假。
        """

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
            for candidate in (f"{alias}.{column_name}", column_name):
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
            {alias: table.schema},
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
    ) -> dict[str, object]:
        """构造一行上下文；needed 不为 None 时只把被引用的列放进上下文。"""

        if template is None:
            template = self._context_template(reference, table, needed, row_order)
        context: dict[str, object] = {}
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
                (template.alias, column.name, value)
                for column, value in zip(template.schema, row, strict=True)
            ]
        return context

    def _merge_context(
        self, left: dict[str, object], right: dict[str, object]
    ) -> dict[str, object]:
        merged = {
            key: value
            for key, value in left.items()
            if key not in {"__row_ids__", "__schemas__", "__row_order__"}
        }
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
        self, context: dict[str, object], table_name: str | None
    ) -> list[object]:
        result: list[object] = []
        row_order = context.get("__row_order__")
        if not isinstance(row_order, list):
            return result
        for alias, column, value in row_order:
            if table_name is None or str(table_name).lower() in {str(alias).lower()}:
                result.append(value)
        return result

    def _output_names(self, statement: Select) -> list[str]:
        names: list[str] = []
        for item in statement.items:
            if isinstance(item.expression, Star):
                selected = self._source_refs(statement)
                if item.expression.table:
                    selected = tuple(
                        ref
                        for ref in selected
                        if ref.effective_name.lower() == item.expression.table.lower()
                    )
                for ref in selected:
                    try:
                        schema = self._relation_schema_of(ref)
                    except (CatalogError, ExecutionError, BinderError):
                        continue
                    names.extend(column.name for column in schema)
            elif item.alias:
                names.append(item.alias)
            elif isinstance(item.expression, ColumnRef):
                names.append(item.expression.name)
            else:
                names.append(self._expression_output_name(item.expression))
        return names

    def _uses_index(self, statement: Select) -> bool:
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

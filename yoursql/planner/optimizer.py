"""统计、代价估算、计划缓存和具名重写规则。"""

from __future__ import annotations

import hashlib
import operator as py_operator
import re
from collections.abc import Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from threading import RLock

from yoursql.common.types import TableStats, compare_values
from yoursql.sql.ast import (
    BetweenPredicate,
    BinaryOp,
    ColumnRef,
    CreateView,
    Delete,
    Expr,
    Explain,
    FunctionCall,
    InPredicate,
    Insert,
    IsNull,
    JoinClause,
    Literal,
    Node,
    OrderItem,
    Parameter,
    Select,
    SelectItem,
    Star,
    Statement,
    Subquery,
    UnaryOp,
    Update,
)
from yoursql.sql.lexer import tokenize
from yoursql.planner.cost import (
    DECODE_ROW_COST,
    INDEX_ENTRY_COST,
    INDEX_ONLY_ENTRY_COST,
    RANDOM_PAGE_COST,
    SEQ_PAGE_COST,
    CostEstimate,
)
from yoursql.planner.logical import plan_from_statement
from yoursql.planner.physical import PlanNode, PhysicalPlanNode, as_physical


# HOW：小表直接走索引的收益有限，但不值得为其引入额外的计划探测逻辑。
_SMALL_TABLE_ROW_THRESHOLD = 128
# WHY：索引命中大量记录时还要逐行回表；超过该比例时顺序扫描通常更稳定。
_INDEX_SELECTIVITY_THRESHOLD = 0.20


@dataclass(frozen=True)
class RewriteRule:
    """一条具名优化规则：可枚举、可单独关闭，命中情况会写进计划。

    HOW：规则本体仍是 ``Optimizer`` 上的方法（它们要共享统计信息与索引元数据），
    这里登记的是**规则清单**——名字、说明、执行阶段。这样做换来三件事：
    1. "可优化（≥2 条规则）"有了可枚举的证据，不再是散落在代码里的隐式改写；
    2. ``optimize()`` 能把"这条语句实际命中了哪些规则"记进计划，EXPLAIN 可解释；
    3. 可以按名字关掉某条规则，现场演示"关掉它计划会变成什么样"。
    """

    name: str
    summary: str
    stage: str  # expression / predicate / access-path / join / cardinality
    togglable: bool = True


# HOW：规则清单就是优化器的能力边界——按语句 → 谓词 → 访问路径的顺序执行。
DEFAULT_RULES: tuple[RewriteRule, ...] = (
    RewriteRule(
        "constant_folding",
        "编译期折叠常量表达式：1 + 2 → 3、'a' || 'b' → 'ab'、CAST 常量直接求值",
        "expression",
    ),
    RewriteRule(
        "boolean_simplification",
        "布尔恒等式化简：x AND TRUE → x、x OR FALSE → x、NOT TRUE → FALSE",
        "expression",
    ),
    RewriteRule(
        "predicate_elimination",
        "消除恒真/恒假过滤：Filter(TRUE) → 子节点，Filter(FALSE) → EmptyScan",
        "predicate",
    ),
    RewriteRule(
        "predicate_pushdown",
        "单表谓词下推到扫描节点，跨表谓词保留在 JOIN 上方",
        "predicate",
    ),
    RewriteRule(
        "index_selection",
        "按谓词列与索引元数据选择 IndexScan（覆盖索引走只读路径）",
        "access-path",
    ),
    RewriteRule(
        "join_reordering",
        "等值键贪心重排连接顺序：先取最小的表，再逐张接入有等值键的表，避免首层退化成笛卡尔积",
        "join",
    ),
    RewriteRule(
        "limit_pushdown",
        "限行下推：LIMIT 越过不改变基数的投影贴近扫描；排序只需前 k 行时标注 top_n 交给有界堆",
        "cardinality",
    ),
)

DEFAULT_RULE_NAMES: frozenset[str] = frozenset(rule.name for rule in DEFAULT_RULES)

# HOW：用 ContextVar 而不是实例属性，既能被 @classmethod 的规则方法读到，
# 又天然按调用栈隔离（多线程 / 嵌套 optimize 都不会互相污染）。
_DISABLED_RULES: ContextVar[frozenset[str]] = ContextVar(
    "yoursql_disabled_rules", default=frozenset()
)
_FIRED_RULES: ContextVar[set[str] | None] = ContextVar(
    "yoursql_fired_rules", default=None
)


def _rule_enabled(name: str) -> bool:
    """当前作用域内这条规则是否启用。"""

    return name not in _DISABLED_RULES.get()


def _fire(name: str) -> None:
    """登记一条规则被命中；不在 ``optimize()`` 的追踪作用域内时为无操作。"""

    fired = _FIRED_RULES.get()
    if fired is not None:
        fired.add(name)


@dataclass
class StatisticsStore:
    tables: dict[str, TableStats] = field(default_factory=dict)

    def update(self, table_name: str, stats: TableStats) -> None:
        """更新数据并维护相关索引或页。"""
        self.tables[table_name.lower()] = stats

    def get(self, table_name: str) -> TableStats:
        """按名称或键获取对象；找不到时遵循调用方约定返回默认值。"""
        return self.tables.get(table_name.lower(), TableStats())


class PlanCache:
    """按词法规范化 SQL 缓存计划，并以容量淘汰最早项目。"""

    def __init__(self, capacity: int = 256) -> None:
        """初始化实例所需的状态和依赖。"""
        self.capacity = max(1, capacity)
        self._items: dict[str, object] = {}
        self._lock = RLock()

    @staticmethod
    def key(sql: str) -> str:
        # WHY：不能直接把整段 SQL 转小写，否则字符串字面量大小写变化会
        # 命中同一个计划缓存项，进而复用错误的谓词常量。
        """根据输入构造稳定的键值。"""
        try:
            tokens = tokenize(sql)
            normalized = " ".join(
                f"{token.kind.value}:{token.value!r}"
                if token.kind.name in {"STRING", "QUOTED_IDENTIFIER"}
                else f"{token.kind.value}:{str(token.value).lower()}"
                for token in tokens
            )
        except Exception:
            normalized = re.sub(r"\s+", " ", sql.strip())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get(self, sql: str) -> object | None:
        """按名称或键获取对象；找不到时遵循调用方约定返回默认值。"""
        with self._lock:
            return self._items.get(self.key(sql))

    def put(self, sql: str, plan: object) -> None:
        """将计划写入缓存，并按容量淘汰旧项目。"""
        with self._lock:
            if len(self._items) >= self.capacity:
                self._items.pop(next(iter(self._items)))
            self._items[self.key(sql)] = plan

    def invalidate(self) -> None:
        """使相关缓存或计划失效。"""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        """返回对象包含的元素数量。"""
        return len(self._items)


class Optimizer:
    """负责表达式重写、谓词下推、扫描方式选择和计划缓存。"""

    def __init__(
        self,
        statistics: StatisticsStore | None = None,
        cache: PlanCache | None = None,
        *,
        buffer_pool_pages: int = 64,
        disabled_rules: Collection[str] = (),
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.statistics = statistics or StatisticsStore()
        self.cache = cache or PlanCache()
        # HOW：随机回表成本取决于“表页数是否超出缓存”，真实池容量由 Database 注入。
        self.buffer_pool_pages = max(1, int(buffer_pool_pages))
        unknown = set(disabled_rules) - DEFAULT_RULE_NAMES
        if unknown:
            raise ValueError(
                "未知的优化规则 %s；可用规则：%s"
                % (
                    ", ".join(sorted(unknown)),
                    ", ".join(sorted(DEFAULT_RULE_NAMES)),
                )
            )
        self.disabled_rules: frozenset[str] = frozenset(disabled_rules)
        # HOW：最近一次 optimize() 命中的规则，便于调用方（EXPLAIN / 测试）直接读取。
        self.last_fired_rules: tuple[str, ...] = ()

    @classmethod
    def rule_catalogue(cls) -> tuple[RewriteRule, ...]:
        """全部内置规则（含说明与执行阶段），用于文档、演示和校验。"""

        return DEFAULT_RULES

    @property
    def enabled_rules(self) -> tuple[RewriteRule, ...]:
        """当前实例实际启用的规则子集。"""

        return tuple(
            rule for rule in DEFAULT_RULES if rule.name not in self.disabled_rules
        )

    def explain_rules(self) -> str:
        """把规则清单渲染成可读文本（现场讲解 / 文档直接引用）。"""

        lines = [f"优化规则 {len(self.enabled_rules)}/{len(DEFAULT_RULES)} 条启用："]
        for rule in DEFAULT_RULES:
            state = "关闭" if rule.name in self.disabled_rules else "启用"
            lines.append(f"  [{state}] {rule.name:<24}{rule.stage:<12}{rule.summary}")
        return "\n".join(lines)

    def _random_row_cost(self, page_count: int) -> float:
        """随机回表单行成本：记录解码 + 未命中缓存部分的随机取页。"""

        pages = max(1, page_count)
        miss_ratio = (
            0.0
            if pages <= self.buffer_pool_pages
            else 1.0 - self.buffer_pool_pages / pages
        )
        return DECODE_ROW_COST + RANDOM_PAGE_COST * miss_ratio

    def estimate_seq_scan(self, table_name: str) -> CostEstimate:
        """顺序扫描成本：读全部页 + 解码全部行。"""

        stats = self.statistics.get(table_name)
        rows = max(0, stats.row_count)
        pages = max(1, stats.page_count or 1)
        total = pages * SEQ_PAGE_COST + rows * DECODE_ROW_COST
        return CostEstimate(total, max(1.0, rows / 100.0), rows)

    def estimate_index_scan(
        self,
        table_name: str,
        *,
        selectivity: float = 0.1,
        index_cached: bool = False,
    ) -> CostEstimate:
        """索引扫描成本：索引项遍历 + 按候选行回表（回表单价随缓存命中情况变化）。"""

        stats = self.statistics.get(table_name)
        rows = max(0, stats.row_count)
        pages = max(1, stats.page_count or 1)
        selected = max(1, int(rows * selectivity)) if rows else 0
        entry_cost = 0.0 if index_cached else INDEX_ENTRY_COST
        total = selected * (entry_cost + self._random_row_cost(pages))
        return CostEstimate(total, max(0.2, selected / 200.0), selected)

    def estimate_plan(self, plan: PlanNode) -> CostEstimate:
        """汇总计划中的扫描代价，供工作台展示优化器估算。"""

        if plan.kind == "SeqScan":
            table = plan.properties.get("table")
            return (
                self.estimate_seq_scan(table)
                if isinstance(table, str)
                else CostEstimate(0.0, 0.0, 0)
            )
        if plan.kind == "IndexScan":
            table = plan.properties.get("table")
            if not isinstance(table, str):
                return CostEstimate(0.0, 0.0, 0)
            stats = self.statistics.get(table)
            candidate_rows = plan.properties.get("candidate_rows")
            selectivity = 0.1
            if isinstance(candidate_rows, int) and stats.row_count:
                selectivity = max(0.0, min(1.0, candidate_rows / stats.row_count))
            return self.estimate_index_scan(table, selectivity=selectivity)
        if plan.kind == "EmptyScan":
            return CostEstimate(0.0, 0.0, 0)

        children = tuple(self.estimate_plan(child) for child in plan.children)
        if not children:
            # HOW：无扫描节点的常量、DDL 计划不引入虚假的扫描成本，但保留一行结果估算。
            return CostEstimate(0.0, 0.0, 1)
        startup_cost = sum(child.startup_cost for child in children)
        total_cost = sum(child.total_cost for child in children)
        if plan.kind == "Join" and len(children) == 2:
            rows = children[0].rows * children[1].rows
        elif len(children) == 1:
            rows = children[0].rows
        else:
            rows = sum(child.rows for child in children)
        return CostEstimate(startup_cost, total_cost, rows)

    def choose_scan(
        self, table_name: str, *, has_usable_index: bool, selectivity: float = 0.1
    ) -> str:
        """根据索引可用性和选择率选择扫描方式。"""
        seq = self.estimate_seq_scan(table_name)
        idx = self.estimate_index_scan(table_name, selectivity=selectivity)
        if not has_usable_index:
            return "SeqScan"
        rows = self.statistics.get(table_name).row_count
        # HOW：先用固定选择性做保守决策；大表的宽索引扫描不应仅因存在索引就被选中。
        if (
            rows > _SMALL_TABLE_ROW_THRESHOLD
            and selectivity > _INDEX_SELECTIVITY_THRESHOLD
        ):
            return "SeqScan"
        # HOW：小表或高选择性场景再用页级代价（顺序页/解码行/随机回表）做最终判断。
        candidate_rows = max(1, int(rows * selectivity)) if rows else 1
        if rows > _SMALL_TABLE_ROW_THRESHOLD and not self.should_use_index(
            table_name, candidate_rows
        ):
            return "SeqScan"
        return "IndexScan" if idx.total_cost < seq.total_cost else "SeqScan"

    def should_use_index(
        self, table_name: str, candidate_rows: int, *, index_cached: bool = False
    ) -> bool:
        """按页级代价判断是否值得走索引回表。

        WHY：顺序扫描与随机回表的单行成本差一个数量级（实测 4.2 µs/行 vs 98.6 µs/行），
        只看“候选行占比”会把 18% 候选的查询错判给索引（实测慢 3 倍）。这里直接比较
        两条路线的预计耗时；候选集命中缓存时索引项遍历成本已摊薄。
        """

        stats = self.statistics.get(table_name)
        rows = max(0, stats.row_count)
        if candidate_rows < 0:
            raise ValueError("候选行数不能为负数")
        if candidate_rows == 0 or rows <= _SMALL_TABLE_ROW_THRESHOLD:
            return True
        pages = max(1, stats.page_count or 1)
        cost_seq = pages * SEQ_PAGE_COST + rows * DECODE_ROW_COST
        entry_cost = 0.0 if index_cached else INDEX_ENTRY_COST
        cost_index = candidate_rows * (entry_cost + self._random_row_cost(pages))
        return cost_index < cost_seq

    def should_use_index_only(self, table_name: str, candidate_rows: int) -> bool:
        """判断覆盖索引直读是否比顺序扫描便宜。

        WHY：覆盖索引不回表，因此不能沿用含随机回表代价的 should_use_index（实测会误判）。
        这里只比较“索引条目解析”与“全表顺序扫描”。
        """

        stats = self.statistics.get(table_name)
        rows = max(0, stats.row_count)
        if candidate_rows < 0:
            raise ValueError("候选行数不能为负数")
        if candidate_rows == 0:
            return True
        pages = max(1, stats.page_count or 1)
        cost_seq = pages * SEQ_PAGE_COST + rows * DECODE_ROW_COST
        return candidate_rows * INDEX_ONLY_ENTRY_COST < cost_seq

    def optimize(
        self,
        plan: PlanNode,
        *,
        sql: str | None = None,
        index_columns: Mapping[str, Collection[str]] | None = None,
    ) -> PhysicalPlanNode:
        """按当前统计信息和索引元数据改写计划。"""

        if sql is not None:
            cached = self.cache.get(sql)
            if cached is not None:
                if not isinstance(cached, PhysicalPlanNode):
                    raise TypeError("计划缓存包含非物理计划")
                self.last_fired_rules = self._rules_of(cached)
                return cached
        statement = plan.statement
        # HOW：把"本实例关掉了哪些规则"和"本次命中了哪些规则"放进 ContextVar，
        # 让 @classmethod 的规则方法也能读到，同时按调用栈天然隔离。
        disabled_token = _DISABLED_RULES.set(self.disabled_rules)
        fired: set[str] = set()
        fired_token = _FIRED_RULES.set(fired)
        try:
            rewritten_statement = (
                self._rewrite_statement(statement) if statement is not None else None
            )
            # HOW：连接重排要读表行数，而 _rewrite_statement 是无状态的类方法，
            # 所以放在这里（持有 statistics 的实例方法）执行。
            rewritten_statement = self._reorder_joins_in_statement(rewritten_statement)
            base_plan = plan
            if rewritten_statement is not None and rewritten_statement != statement:
                # HOW：先用折叠后的 AST 重建计划，再做访问路径和谓词下推，确保
                # 计划属性、实际执行语句和索引边界使用同一份表达式。
                base_plan = plan_from_statement(rewritten_statement)
            optimized = self._rewrite(base_plan, index_columns=index_columns or {})
            if not isinstance(optimized, PlanNode):
                raise TypeError("优化器未返回计划节点")
            if rewritten_statement is not None:
                optimized = replace(optimized, statement=rewritten_statement)
            optimized = as_physical(optimized)
        finally:
            _FIRED_RULES.reset(fired_token)
            _DISABLED_RULES.reset(disabled_token)
        self.last_fired_rules = tuple(sorted(fired))
        if fired:
            # HOW：命中规则写进根节点属性，EXPLAIN 就能回答"这条语句被优化了什么"。
            properties = dict(optimized.properties)
            properties["rules"] = self.last_fired_rules
            optimized = replace(optimized, properties=properties)
        if sql is not None:
            self.cache.put(sql, optimized)
        return optimized

    @staticmethod
    def _rules_of(plan: PlanNode) -> tuple[str, ...]:
        value = plan.properties.get("rules")
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return ()

    def _reorder_joins_in_statement(self, statement: Statement | None) -> Statement | None:
        """把连接重排应用到语句里的 SELECT，能穿透 ``EXPLAIN`` 包裹。

        WHY：重排要改的是 AST（在重建计划之前），而 ``EXPLAIN`` 是独立语句类型，
        真正的 SELECT 在 ``Explain.statement`` 里。只认顶层 ``Select`` 会让
        ``EXPLAIN SELECT ...`` 整条跳过重排——于是 EXPLAIN 显示 ``FROM`` 的书写顺序、
        实际执行却按重排后的顺序跑，**计划与执行不一致**，现场用它演示也看不到规则生效。
        这里与 ``_rewrite_statement`` 对 ``Explain`` 的处理保持一致。
        """

        if statement is None:
            return None
        if isinstance(statement, Select):
            return self._reorder_joins(statement)
        if isinstance(statement, Explain):
            inner = statement.statement
            if isinstance(inner, Select):
                reordered = self._reorder_joins(inner)
                if reordered is not inner:
                    return replace(statement, statement=reordered)
        return statement

    def _reorder_joins(self, statement: Select) -> Select:
        """按等值键贪心重排连接顺序（``join_reordering`` 规则）。

        WHY：执行层只按 ``FROM`` 的书写顺序逐层连接。若第一层两张表在条件里没有可用的
        等值键，第一层就退化成笛卡尔积，后面怎么连都救不回来——实测同一逻辑查询只改
        ``FROM`` 的书写顺序，最坏差 50 倍以上（lineitem ⋈ orders ⋈ customer 的 6 种写法
        里有 2 种 >45s 超时，因为 lineitem 与 customer 之间没有直接连接列）。

        HOW：只在**全 INNER/CROSS** 时重排。INNER 下 ``ON`` 与 ``WHERE`` 等价，所以把
        ``ON`` 合并进 ``WHERE`` 之后可以自由重排；外连接顺序敏感，直接不动。代价只用现成
        的表行数（``TableStats.row_count``），不引入新统计。
        """

        if not _rule_enabled("join_reordering"):
            return statement
        if statement.from_table is None or not statement.joins:
            return statement
        if any(join.join_type not in {"INNER", "CROSS"} for join in statement.joins):
            return statement
        tables = [statement.from_table, *(join.table for join in statement.joins)]
        if any(table.is_derived for table in tables):
            # 派生表没有统计信息，排序没有依据。
            return statement
        names = [table.effective_name.lower() for table in tables]
        if len(set(names)) != len(names):
            return statement
        by_name = {table.effective_name.lower(): table for table in tables}
        for table in tables:
            # HOW：统计按**表名**索引，别名不在其中；视图也没有统计。
            # 拿不准行数就不重排——否则会把"没统计"误当成"0 行"而把大表排到最前面。
            if table.name.lower() not in self.statistics.tables:
                return statement

        # 收集等值连接边（INNER 下 ON 与 WHERE 等价，合并起来一起看）。
        atoms: list[Expr] = []
        if statement.where is not None:
            atoms.extend(self._split_conjunction(statement.where))
        for join in statement.joins:
            if join.on is not None:
                atoms.extend(self._split_conjunction(join.on))
        known = set(names)
        edges: set[frozenset[str]] = set()
        for atom in atoms:
            pair = self._equi_join_pair(atom)
            if pair is not None and pair[0] in known and pair[1] in known:
                edges.add(frozenset(pair))
        if not edges:
            # 一张表都连不上——本来就是笛卡尔积，换顺序没有意义。
            return statement

        def cost(name: str) -> tuple[int, str]:
            # HOW：按真实表名取行数（别名取不到统计）；行数并列时按别名定序，
            # 保证同一个查询的不同书写顺序都收敛到同一个执行顺序。
            return (max(0, self.statistics.get(by_name[name].name).row_count), name)

        remaining = sorted(names, key=cost)
        chosen = [remaining.pop(0)]
        while remaining:
            connected = [
                name
                for name in remaining
                if any(frozenset((name, picked)) in edges for picked in chosen)
            ]
            # HOW：与已连接集合有等值键的优先；一张都接不上的（必须做笛卡尔积）排最后。
            pool = sorted(connected or remaining, key=cost)
            chosen.append(pool[0])
            remaining.remove(pool[0])
        if chosen == names:
            return statement

        _fire("join_reordering")
        return replace(
            statement,
            from_table=by_name[chosen[0]],
            joins=tuple(
                JoinClause(join_type="CROSS", table=by_name[name], on=None)
                for name in chosen[1:]
            ),
            # HOW：原来挂在各级 INNER JOIN 上的 ON 合并进 WHERE——对 INNER 二者等价，
            # 且执行层本来就会从 WHERE 为 INNER/CROSS 推断连接键。
            where=self._combine_conjunction(atoms),
        )

    @classmethod
    def _equi_join_pair(cls, atom: Expr) -> tuple[str, str] | None:
        """识别 ``a.x = b.y`` 形式的等值连接谓词，返回两侧的归属限定符。"""

        if not isinstance(atom, BinaryOp) or atom.operator != "=":
            return None
        left, right = atom.left, atom.right
        if not isinstance(left, ColumnRef) or not isinstance(right, ColumnRef):
            return None
        if not left.table or not right.table:
            return None
        first, second = left.table.lower(), right.table.lower()
        if first == second:
            return None
        return first, second

    def _rewrite(
        self, plan: object, *, index_columns: Mapping[str, Collection[str]]
    ) -> object:
        """递归重写计划或计划值。"""
        if isinstance(plan, PlanNode):
            return self._rewrite_plan_node(plan, index_columns=index_columns)
        children = getattr(plan, "children", None)
        if isinstance(children, tuple):
            rewritten = tuple(
                self._rewrite(child, index_columns=index_columns) for child in children
            )
            if rewritten != children:
                try:
                    plan = replace(plan, children=rewritten)
                except (TypeError, ValueError):
                    return plan
        return plan

    def _rewrite_plan_node(
        self, plan: PlanNode, *, index_columns: Mapping[str, Collection[str]]
    ) -> PlanNode:
        """递归重写计划，并把单表过滤条件下推到扫描节点。"""

        if plan.kind == "Filter" and len(plan.children) == 1:
            child = self._rewrite(plan.children[0], index_columns=index_columns)
            predicate = self._rewrite_expr(plan.properties.get("predicate"))
            if not isinstance(predicate, Expr):
                return (
                    replace(plan, children=(child,))
                    if child != plan.children[0]
                    else plan
                )
            if _rule_enabled("predicate_elimination") and self._is_false_predicate(
                predicate
            ):
                _fire("predicate_elimination")
                return PlanNode(
                    "EmptyScan",
                    {"reason": "过滤条件恒假", "predicate": predicate},
                    (),
                    plan.statement,
                )
            if _rule_enabled("predicate_elimination") and self._is_true_predicate(
                predicate
            ):
                _fire("predicate_elimination")
                return child
            if not _rule_enabled("predicate_pushdown"):
                properties = dict(plan.properties)
                properties["predicate"] = predicate
                return replace(plan, properties=properties, children=(child,))
            pushed, residual = self._push_predicates(
                child, predicate, index_columns=index_columns
            )
            if residual is None:
                return pushed
            properties = dict(plan.properties)
            properties["predicate"] = residual
            return replace(plan, properties=properties, children=(pushed,))

        if plan.kind == "Limit" and len(plan.children) == 1:
            child = self._rewrite(plan.children[0], index_columns=index_columns)
            limit_value = plan.properties.get("limit")
            offset_value = plan.properties.get("offset") or 0
            usable = (
                _rule_enabled("limit_pushdown")
                and isinstance(limit_value, int)
                and limit_value >= 0
            )
            if usable:
                needed = limit_value + int(offset_value)
                # HOW（a）排序只需要前 k 行：把 k 标在 Sort 上，运行时据此用大小为 k 的
                # 有界堆，不必物化全部行再全量排序。k 不小于预计行数时标了也没收益。
                if child.kind == "Sort" and needed < self.estimate_plan(child).rows:
                    _fire("limit_pushdown")
                    properties = dict(child.properties)
                    properties["top_n"] = needed
                    return replace(
                        plan, children=(replace(child, properties=properties),)
                    )
                # HOW（b）投影不改变基数（非 DISTINCT）时，让 LIMIT 越过投影贴近扫描，
                # 这样"限行尽量早生效"这件事在计划里是看得见的，而不是只发生在运行时。
                if child.kind == "Project" and not child.properties.get("distinct"):
                    _fire("limit_pushdown")
                    pushed = PlanNode(
                        "Limit",
                        {
                            "limit": limit_value,
                            "offset": int(offset_value),
                            "pushed": True,
                        },
                        child.children,
                        plan.statement,
                    )
                    return replace(child, children=(pushed,))
            return (
                replace(plan, children=(child,))
                if child != plan.children[0]
                else plan
            )

        properties = {
            key: self._rewrite_plan_value(value)
            for key, value in dict(plan.properties).items()
        }
        children = tuple(
            self._rewrite(child, index_columns=index_columns) for child in plan.children
        )
        if properties == dict(plan.properties) and children == plan.children:
            return plan
        return replace(plan, properties=properties, children=children)

    def _choose_scan(
        self,
        plan: PlanNode,
        predicate: object,
        index_columns: Mapping[str, Collection[str]],
    ) -> PlanNode:
        """为顺序扫描节点选择顺序或索引访问。"""
        if plan.kind != "SeqScan":
            return plan
        if not _rule_enabled("index_selection"):
            return plan
        table = plan.properties.get("table")
        if not isinstance(table, str):
            return plan
        alias = plan.properties.get("alias")
        available = {name.lower() for name in index_columns.get(table.lower(), ())}
        if not self._predicate_uses_index(predicate, table, alias, available):
            return plan
        columns = self._indexable_columns(predicate, table, alias)
        column = next(
            (item for item in columns if item.name.lower() in available), None
        )
        if column is None:
            return plan
        if self.choose_scan(table, has_usable_index=True) != "IndexScan":
            return plan
        _fire("index_selection")
        properties = dict(plan.properties)
        properties["index_column"] = column.name
        return PlanNode("IndexScan", properties, plan.children, plan.statement)

    @staticmethod
    def _constant_expression(expression: object) -> bool:
        """判断表达式是否能在编译期折叠为字面量。"""

        if not isinstance(expression, Expr):
            return False
        return isinstance(Optimizer._rewrite_expr(expression), Literal)

    @classmethod
    def constant_value(cls, expression: Expr) -> tuple[bool, object]:
        """返回可预计算表达式的值，供索引候选集复用同一套规则。"""

        folded = cls._rewrite_expr(expression)
        if isinstance(folded, Literal):
            return True, folded.value
        return False, None

    @classmethod
    def _indexable_columns(
        cls, predicate: object, table: str, alias: object
    ) -> tuple[ColumnRef, ...]:
        """提取谓词中可用于索引访问的列引用。"""
        qualifiers = {table.lower()}
        if isinstance(alias, str):
            qualifiers.add(alias.lower())
        if isinstance(predicate, BinaryOp):
            operator = predicate.operator.upper()
            if operator in {"AND", "OR"}:
                return (
                    *cls._indexable_columns(predicate.left, table, alias),
                    *cls._indexable_columns(predicate.right, table, alias),
                )
            if operator not in {"=", "<", "<=", ">", ">=", "LIKE"}:
                return ()
            for column, other in (
                (predicate.left, predicate.right),
                (predicate.right, predicate.left),
            ):
                if isinstance(column, ColumnRef) and cls._constant_expression(other):
                    if not column.table or column.table.lower() in qualifiers:
                        if operator != "LIKE" or (
                            isinstance(other, Literal)
                            and isinstance(other.value, str)
                            and not any(marker in other.value for marker in ("%", "_"))
                        ):
                            return (column,)
            return ()
        if isinstance(predicate, IsNull) and isinstance(
            predicate.expression, ColumnRef
        ):
            column = predicate.expression
            return (
                (column,)
                if not column.table or column.table.lower() in qualifiers
                else ()
            )
        if (
            isinstance(predicate, InPredicate)
            and not predicate.negated
            and isinstance(predicate.expression, ColumnRef)
        ):
            column = predicate.expression
            if column.table and column.table.lower() not in qualifiers:
                return ()
            return (
                (column,)
                if all(cls._constant_expression(value) for value in predicate.values)
                else ()
            )
        if isinstance(predicate, BetweenPredicate) and isinstance(
            predicate.expression, ColumnRef
        ):
            column = predicate.expression
            if column.table and column.table.lower() not in qualifiers:
                return ()
            return (
                (column,)
                if cls._constant_expression(predicate.lower)
                and cls._constant_expression(predicate.upper)
                else ()
            )
        return ()

    @classmethod
    def _predicate_uses_index(
        cls,
        predicate: object,
        table: str,
        alias: object,
        available: set[str],
    ) -> bool:
        """判断谓词是否能使用给定索引列。"""
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return cls._predicate_uses_index(
                predicate.left, table, alias, available
            ) or cls._predicate_uses_index(predicate.right, table, alias, available)
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "OR":
            return cls._predicate_uses_index(
                predicate.left, table, alias, available
            ) and cls._predicate_uses_index(predicate.right, table, alias, available)
        return any(
            column.name.lower() in available
            for column in cls._indexable_columns(predicate, table, alias)
        )

    @classmethod
    def _rewrite_expr(cls, expression: object) -> object:
        """递归折叠表达式，并应用安全的布尔恒等式。"""

        if expression is None or isinstance(
            expression, (Literal, ColumnRef, Parameter, Star)
        ):
            return expression
        if isinstance(expression, UnaryOp):
            operand = cls._rewrite_expr(expression.operand)
            rewritten = cls._replace_node(expression, operand=operand)
            if not _rule_enabled("constant_folding"):
                return rewritten
            if not isinstance(operand, Literal):
                return rewritten
            if expression.operator.upper() == "NOT":
                value = None if operand.value is None else not bool(operand.value)
                _fire("constant_folding")
                return cls._literal(value, expression)
            if operand.value is None:
                _fire("constant_folding")
                return cls._literal(None, expression)
            try:
                if expression.operator == "+":
                    _fire("constant_folding")
                    return cls._literal(+operand.value, expression)
                if expression.operator == "-":
                    _fire("constant_folding")
                    return cls._literal(-operand.value, expression)
            except (TypeError, ValueError, OverflowError):
                return rewritten
            return rewritten
        if isinstance(expression, BinaryOp):
            left = cls._rewrite_expr(expression.left)
            right = cls._rewrite_expr(expression.right)
            operator = expression.operator.upper()
            if _rule_enabled("boolean_simplification"):
                simplified = cls._simplify_boolean(operator, left, right, expression)
                if simplified is not None:
                    _fire("boolean_simplification")
                    return simplified
            if (
                _rule_enabled("constant_folding")
                and isinstance(left, Literal)
                and isinstance(right, Literal)
            ):
                folded, value = cls._fold_binary(operator, left.value, right.value)
                if folded:
                    _fire("constant_folding")
                    return cls._literal(value, expression)
            return cls._replace_node(expression, left=left, right=right)
        if isinstance(expression, IsNull):
            child = cls._rewrite_expr(expression.expression)
            if isinstance(child, Literal):
                if not _rule_enabled("constant_folding"):
                    return cls._replace_node(expression, expression=child)
                value = child.value is None
                _fire("constant_folding")
                return cls._literal(
                    not value if expression.negated else value, expression
                )
            return cls._replace_node(expression, expression=child)
        if isinstance(expression, InPredicate):
            child = cls._rewrite_expr(expression.expression)
            values = tuple(cls._rewrite_expr(value) for value in expression.values)
            rewritten = cls._replace_node(expression, expression=child, values=values)
            if not _rule_enabled("constant_folding"):
                return rewritten
            if isinstance(child, Literal) and all(
                isinstance(value, Literal) for value in values
            ):
                result: bool | None = False
                for value in values:
                    comparison = compare_values(child.value, value.value, "=")
                    if comparison is True:
                        result = True
                        break
                    if comparison is None:
                        result = None
                if expression.negated and result is not None:
                    result = not result
                _fire("constant_folding")
                return cls._literal(result, expression)
            return rewritten
        if isinstance(expression, BetweenPredicate):
            child = cls._rewrite_expr(expression.expression)
            lower = cls._rewrite_expr(expression.lower)
            upper = cls._rewrite_expr(expression.upper)
            rewritten = cls._replace_node(
                expression, expression=child, lower=lower, upper=upper
            )
            if not _rule_enabled("constant_folding"):
                return rewritten
            if all(isinstance(value, Literal) for value in (child, lower, upper)):
                result = cls._and_truth(
                    compare_values(child.value, lower.value, ">="),
                    compare_values(child.value, upper.value, "<="),
                )
                if expression.negated and result is not None:
                    result = not result
                _fire("constant_folding")
                return cls._literal(result, expression)
            return rewritten
        if isinstance(expression, FunctionCall):
            return cls._replace_node(
                expression,
                args=tuple(cls._rewrite_expr(arg) for arg in expression.args),
            )
        if isinstance(expression, Subquery):
            query = cls._rewrite_statement(expression.query)
            return cls._replace_node(expression, query=query)
        return expression

    @classmethod
    def _rewrite_statement(cls, statement: Statement | None) -> Statement | None:
        """递归重写语句中的表达式和子查询。"""
        if statement is None:
            return None
        if isinstance(statement, Select):
            return cls._replace_node(
                statement,
                items=tuple(
                    cls._replace_node(
                        item, expression=cls._rewrite_expr(item.expression)
                    )
                    for item in statement.items
                ),
                joins=tuple(
                    cls._replace_node(join, on=cls._rewrite_expr(join.on))
                    for join in statement.joins
                ),
                where=cls._rewrite_expr(statement.where),
                group_by=tuple(cls._rewrite_expr(item) for item in statement.group_by),
                having=cls._rewrite_expr(statement.having),
                order_by=tuple(
                    cls._replace_node(
                        item, expression=cls._rewrite_expr(item.expression)
                    )
                    for item in statement.order_by
                ),
                union=cls._rewrite_statement(statement.union),
            )
        if isinstance(statement, Explain):
            return cls._replace_node(
                statement, statement=cls._rewrite_statement(statement.statement)
            )
        if isinstance(statement, Update):
            return cls._replace_node(
                statement,
                assignments=tuple(
                    (column, cls._rewrite_expr(value))
                    for column, value in statement.assignments
                ),
                where=cls._rewrite_expr(statement.where),
            )
        if isinstance(statement, Delete):
            return cls._replace_node(
                statement, where=cls._rewrite_expr(statement.where)
            )
        if isinstance(statement, Insert):
            return cls._replace_node(
                statement,
                values=tuple(
                    tuple(cls._rewrite_expr(value) for value in row)
                    for row in statement.values
                ),
            )
        if isinstance(statement, CreateView):
            return cls._replace_node(
                statement, query=cls._rewrite_statement(statement.query)
            )
        return statement

    @classmethod
    def _rewrite_plan_value(cls, value: object) -> object:
        """递归重写计划属性中的 AST 或计划值。"""
        if isinstance(value, Expr):
            return cls._rewrite_expr(value)
        if isinstance(value, SelectItem):
            return cls._replace_node(
                value, expression=cls._rewrite_expr(value.expression)
            )
        if isinstance(value, OrderItem):
            return cls._replace_node(
                value, expression=cls._rewrite_expr(value.expression)
            )
        if isinstance(value, JoinClause):
            return cls._replace_node(value, on=cls._rewrite_expr(value.on))
        if isinstance(value, Statement):
            return cls._rewrite_statement(value)
        if isinstance(value, tuple):
            return tuple(cls._rewrite_plan_value(item) for item in value)
        if isinstance(value, list):
            return [cls._rewrite_plan_value(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._rewrite_plan_value(item) for key, item in value.items()}
        return value

    @classmethod
    def _replace_node(cls, source: Node, **changes: object) -> Node:
        """复制节点并替换指定字段，同时保留源码位置。"""
        return replace(source, **changes).copy_source_metadata_from(source)

    @classmethod
    def _literal(cls, value: object, source: Node) -> Literal:
        """创建带源码位置的字面量节点。"""
        return Literal(value).copy_source_metadata_from(source)

    @staticmethod
    def _and_truth(left: bool | None, right: bool | None) -> bool | None:
        """按 SQL 三值逻辑计算 AND 的结果。"""
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return None

    @classmethod
    def _simplify_boolean(
        cls, operator: str, left: object, right: object, source: Node
    ) -> Expr | None:
        """应用安全的布尔恒等式和常量折叠。"""
        if operator not in {"AND", "OR"}:
            return None
        if isinstance(left, Literal) and isinstance(right, Literal):
            folded, value = cls._fold_binary(operator, left.value, right.value)
            return cls._literal(value, source) if folded else None
        if operator == "AND":
            if (
                isinstance(left, Literal)
                and left.value is False
                or isinstance(right, Literal)
                and right.value is False
            ):
                return cls._literal(False, source)
            if isinstance(left, Literal) and left.value is True:
                return right if isinstance(right, Expr) else None
            if isinstance(right, Literal) and right.value is True:
                return left if isinstance(left, Expr) else None
        else:
            if (
                isinstance(left, Literal)
                and left.value is True
                or isinstance(right, Literal)
                and right.value is True
            ):
                return cls._literal(True, source)
            if isinstance(left, Literal) and left.value is False:
                return right if isinstance(right, Expr) else None
            if isinstance(right, Literal) and right.value is False:
                return left if isinstance(left, Expr) else None
        if left == right and isinstance(left, Expr):
            return left
        return None

    @classmethod
    def _fold_binary(
        cls, operator: str, left: object, right: object
    ) -> tuple[bool, object]:
        """预计算可折叠的二元表达式。"""
        if operator == "AND":
            return True, cls._and_truth(
                left if isinstance(left, bool) else None,
                right if isinstance(right, bool) else None,
            )
        if operator == "OR":
            if left is True or right is True:
                return True, True
            if left is False and right is False:
                return True, False
            return True, None
        if operator in {"=", "==", "!=", "<>", "<", "<=", ">", ">="}:
            return True, compare_values(left, right, operator)
        if left is None or right is None:
            return True, None
        if operator in {"LIKE", "NOT LIKE"}:
            pattern = (
                "^" + re.escape(str(right)).replace(r"%", ".*").replace(r"_", ".") + "$"
            )
            matched = re.match(pattern, str(left), flags=re.DOTALL) is not None
            return True, not matched if operator == "NOT LIKE" else matched
        if operator == "||":
            return True, str(left) + str(right)
        arithmetic = {
            "+": py_operator.add,
            "-": py_operator.sub,
            "*": py_operator.mul,
            "/": py_operator.truediv,
            "%": py_operator.mod,
        }.get(operator)
        if arithmetic is None:
            return False, None
        try:
            if operator in {"/", "%"} and right == 0:
                return False, None
            # WHY：这里保留 Python 动态运算，让不兼容的 SQL 值统一落入异常分支，
            # 由常量折叠按“不可折叠”处理，而不是改变原有错误语义。
            return True, arithmetic(left, right)
        except (TypeError, ValueError, OverflowError):
            return False, None

    @classmethod
    def _is_true_predicate(cls, expression: Expr) -> bool:
        """判断表达式是否恒为 TRUE。"""
        return isinstance(expression, Literal) and expression.value is True

    @classmethod
    def _is_false_predicate(cls, expression: Expr) -> bool:
        # WHERE 中 UNKNOWN 与 FALSE 一样不会保留记录。
        """判断表达式是否恒为 FALSE 或 UNKNOWN。"""
        return isinstance(expression, Literal) and expression.value is not True

    @classmethod
    def _split_conjunction(cls, predicate: Expr) -> tuple[Expr, ...]:
        """将 AND 谓词展开为原子条件。"""
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return (
                *cls._split_conjunction(predicate.left),
                *cls._split_conjunction(predicate.right),
            )
        return (predicate,)

    @staticmethod
    def _combine_conjunction(predicates: Collection[Expr]) -> Expr | None:
        """将多个原子条件组合为 AND 表达式。"""
        items = tuple(predicates)
        if not items:
            return None
        result = items[0]
        for predicate in items[1:]:
            result = BinaryOp(result, "AND", predicate)
        return result

    @classmethod
    def _predicate_relations(cls, predicate: Expr) -> set[str] | None:
        """收集谓词引用的关系名称。"""
        if isinstance(predicate, ColumnRef):
            return {predicate.table.lower()} if predicate.table else set()
        if isinstance(predicate, Subquery):
            return None
        if isinstance(predicate, BinaryOp):
            left = cls._predicate_relations(predicate.left)
            right = cls._predicate_relations(predicate.right)
            if left is None or right is None:
                return None
            return left | right
        if isinstance(predicate, UnaryOp):
            return cls._predicate_relations(predicate.operand)
        if isinstance(predicate, FunctionCall):
            result: set[str] = set()
            for argument in predicate.args:
                relations = cls._predicate_relations(argument)
                if relations is None:
                    return None
                result.update(relations)
            return result
        if isinstance(predicate, IsNull):
            return cls._predicate_relations(predicate.expression)
        if isinstance(predicate, InPredicate):
            result = cls._predicate_relations(predicate.expression)
            if result is None:
                return None
            for value in predicate.values:
                relations = cls._predicate_relations(value)
                if relations is None:
                    return None
                result.update(relations)
            return result
        if isinstance(predicate, BetweenPredicate):
            result = cls._predicate_relations(predicate.expression)
            if result is None:
                return None
            for value in (predicate.lower, predicate.upper):
                relations = cls._predicate_relations(value)
                if relations is None:
                    return None
                result.update(relations)
            return result
        return set()

    @classmethod
    def _plan_relations(cls, plan: PlanNode) -> set[str]:
        """收集计划子树扫描的关系名称。"""
        if plan.kind in {"SeqScan", "IndexScan"}:
            result: set[str] = set()
            table = plan.properties.get("table")
            alias = plan.properties.get("alias")
            if isinstance(table, str):
                result.add(table.lower())
            if isinstance(alias, str):
                result.add(alias.lower())
            return result
        result: set[str] = set()
        for child in plan.children:
            result.update(cls._plan_relations(child))
        return result

    @classmethod
    def _can_push_to(cls, predicate: Expr, relations: set[str]) -> bool:
        """判断谓词是否只引用指定关系并可下推。"""
        referenced = cls._predicate_relations(predicate)
        if referenced is None:
            return False
        if not referenced:
            return len(relations) == 1
        return referenced.issubset(relations)

    def _push_predicates(
        self,
        child: PlanNode,
        predicate: Expr,
        *,
        index_columns: Mapping[str, Collection[str]],
    ) -> tuple[PlanNode, Expr | None]:
        """把单表 AND 条件下推，跨表条件保留在 JOIN 上方。"""

        atoms = self._split_conjunction(predicate)
        pushed, residual = self._push_into_subtree(
            child, atoms, index_columns=index_columns
        )
        if residual != atoms or pushed != child:
            _fire("predicate_pushdown")
        return pushed, self._combine_conjunction(residual)

    def _push_into_subtree(
        self,
        plan: PlanNode,
        predicates: Collection[Expr],
        *,
        index_columns: Mapping[str, Collection[str]],
    ) -> tuple[PlanNode, tuple[Expr, ...]]:
        """将可下推谓词递归放入计划子树。"""
        items = tuple(predicates)
        if not items:
            return plan, ()
        if (
            plan.kind == "Join"
            and len(plan.children) == 2
            and str(plan.properties.get("join_type", "")).upper() in {"INNER", "CROSS"}
        ):
            left, right = plan.children
            left_relations = self._plan_relations(left)
            right_relations = self._plan_relations(right)
            left_items = tuple(
                item for item in items if self._can_push_to(item, left_relations)
            )
            right_items = tuple(
                item
                for item in items
                if item not in left_items and self._can_push_to(item, right_relations)
            )
            residual = tuple(
                item
                for item in items
                if item not in left_items and item not in right_items
            )
            left, left_residual = self._push_into_subtree(
                left, left_items, index_columns=index_columns
            )
            right, right_residual = self._push_into_subtree(
                right, right_items, index_columns=index_columns
            )
            rewritten = replace(plan, children=(left, right))
            return rewritten, (*left_residual, *right_residual, *residual)
        # HOW：单个扫描节点可能同时包含真实表名和别名，不能用关系名集合
        # 的长度判断是否为单表；只要节点本身是扫描，就可以接收单表谓词。
        if plan.kind in {"SeqScan", "IndexScan"}:
            predicate = self._combine_conjunction(items)
            if predicate is not None:
                return self._wrap_pushed_filter(
                    plan, predicate, index_columns=index_columns
                ), ()
        return plan, items

    def _wrap_pushed_filter(
        self,
        plan: PlanNode,
        predicate: Expr,
        *,
        index_columns: Mapping[str, Collection[str]],
    ) -> PlanNode:
        """在扫描节点上保留可观察的下推 Filter，并记录扫描谓词。"""

        if plan.kind in {"SeqScan", "IndexScan"}:
            properties = dict(plan.properties)
            properties["pushed_predicate"] = predicate
            scan = replace(plan, properties=properties)
            scan = self._choose_scan(scan, predicate, index_columns)
            return PlanNode(
                "Filter",
                {"predicate": predicate, "pushed": True},
                (scan,),
                plan.statement,
            )
        return PlanNode("Filter", {"predicate": predicate}, (plan,), plan.statement)


__all__ = [
    "DEFAULT_RULES",
    "CostEstimate",
    "Optimizer",
    "PlanCache",
    "RewriteRule",
    "StatisticsStore",
]

"""统计、代价估算、计划缓存和基础递归规则。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from threading import RLock

from ..common.types import TableStats, compare_values
from ..sql.ast import (
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
from ..sql.lexer import tokenize
from ..sql.plan import PlanNode, plan_from_statement


# HOW：小表直接走索引的收益有限，但不值得为其引入额外的计划探测逻辑。
_SMALL_TABLE_ROW_THRESHOLD = 128
# WHY：索引命中大量记录时还要逐行回表；超过该比例时顺序扫描通常更稳定。
_INDEX_SELECTIVITY_THRESHOLD = 0.20


@dataclass
class StatisticsStore:
    tables: dict[str, TableStats] = field(default_factory=dict)

    def update(self, table_name: str, stats: TableStats) -> None:
        self.tables[table_name.lower()] = stats

    def get(self, table_name: str) -> TableStats:
        return self.tables.get(table_name.lower(), TableStats())


@dataclass(frozen=True)
class CostEstimate:
    startup_cost: float
    total_cost: float
    rows: int


class PlanCache:
    """按词法规范化 SQL 缓存计划，并以容量淘汰最早项目。"""

    def __init__(self, capacity: int = 256) -> None:
        self.capacity = max(1, capacity)
        self._items: dict[str, object] = {}
        self._lock = RLock()

    @staticmethod
    def key(sql: str) -> str:
        # WHY：不能直接把整段 SQL 转小写，否则字符串字面量大小写变化会
        # 命中同一个计划缓存项，进而复用错误的谓词常量。
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
        with self._lock:
            return self._items.get(self.key(sql))

    def put(self, sql: str, plan: object) -> None:
        with self._lock:
            if len(self._items) >= self.capacity:
                self._items.pop(next(iter(self._items)))
            self._items[self.key(sql)] = plan

    def invalidate(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class Optimizer:
    """负责表达式重写、谓词下推、扫描方式选择和计划缓存。"""

    def __init__(self, statistics: StatisticsStore | None = None, cache: PlanCache | None = None) -> None:
        self.statistics = statistics or StatisticsStore()
        self.cache = cache or PlanCache()

    def estimate_seq_scan(self, table_name: str) -> CostEstimate:
        rows = self.statistics.get(table_name).row_count
        return CostEstimate(0.1, max(1.0, rows / 100.0), rows)

    def estimate_index_scan(self, table_name: str, *, selectivity: float = 0.1) -> CostEstimate:
        rows = self.statistics.get(table_name).row_count
        selected = max(1, int(rows * selectivity)) if rows else 0
        return CostEstimate(0.2, max(0.2, selected / 200.0), selected)

    def estimate_plan(self, plan: PlanNode) -> CostEstimate:
        """汇总计划中的扫描代价，供工作台展示优化器估算。"""

        if plan.kind == "SeqScan":
            table = plan.properties.get("table")
            return self.estimate_seq_scan(table) if isinstance(table, str) else CostEstimate(0.0, 0.0, 0)
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

    def choose_scan(self, table_name: str, *, has_usable_index: bool, selectivity: float = 0.1) -> str:
        seq = self.estimate_seq_scan(table_name)
        idx = self.estimate_index_scan(table_name, selectivity=selectivity)
        if not has_usable_index:
            return "SeqScan"
        rows = self.statistics.get(table_name).row_count
        # HOW：先用固定选择性做保守决策；大表的宽索引扫描不应仅因存在索引就被选中。
        if rows > _SMALL_TABLE_ROW_THRESHOLD and selectivity > _INDEX_SELECTIVITY_THRESHOLD:
            return "SeqScan"
        return "IndexScan" if idx.total_cost < seq.total_cost else "SeqScan"

    def should_use_index(self, table_name: str, candidate_rows: int) -> bool:
        """按实际候选行数判断是否值得回表。"""

        rows = self.statistics.get(table_name).row_count
        if candidate_rows < 0:
            raise ValueError("候选行数不能为负数")
        if candidate_rows == 0 or rows <= _SMALL_TABLE_ROW_THRESHOLD:
            return True
        return candidate_rows / rows <= _INDEX_SELECTIVITY_THRESHOLD

    def optimize(
        self,
        plan: object,
        *,
        sql: str | None = None,
        index_columns: Mapping[str, Collection[str]] | None = None,
    ) -> object:
        """按当前统计信息和索引元数据改写计划。"""

        if sql is not None:
            cached = self.cache.get(sql)
            if cached is not None:
                return cached
        statement = plan.statement if isinstance(plan, PlanNode) else None
        rewritten_statement = self._rewrite_statement(statement) if statement is not None else None
        base_plan = plan
        if rewritten_statement is not None and rewritten_statement != statement:
            # HOW：先用折叠后的 AST 重建计划，再做访问路径和谓词下推，确保
            # 计划属性、实际执行语句和索引边界使用同一份表达式。
            base_plan = plan_from_statement(rewritten_statement)
        optimized = self._rewrite(base_plan, index_columns=index_columns or {})
        if isinstance(optimized, PlanNode) and rewritten_statement is not None:
            optimized = replace(optimized, statement=rewritten_statement)
        if sql is not None:
            self.cache.put(sql, optimized)
        return optimized

    def _rewrite(self, plan: object, *, index_columns: Mapping[str, Collection[str]]) -> object:
        if isinstance(plan, PlanNode):
            return self._rewrite_plan_node(plan, index_columns=index_columns)
        children = getattr(plan, "children", None)
        if isinstance(children, tuple):
            rewritten = tuple(self._rewrite(child, index_columns=index_columns) for child in children)
            if rewritten != children:
                try:
                    plan = replace(plan, children=rewritten)
                except (TypeError, ValueError):
                    return plan
        return plan

    def _rewrite_plan_node(self, plan: PlanNode, *, index_columns: Mapping[str, Collection[str]]) -> PlanNode:
        """递归重写计划，并把单表过滤条件下推到扫描节点。"""

        if plan.kind == "Filter" and len(plan.children) == 1:
            child = self._rewrite(plan.children[0], index_columns=index_columns)
            predicate = self._rewrite_expr(plan.properties.get("predicate"))
            if not isinstance(predicate, Expr):
                return replace(plan, children=(child,)) if child != plan.children[0] else plan
            if self._is_false_predicate(predicate):
                return PlanNode("EmptyScan", {"reason": "过滤条件恒假", "predicate": predicate}, (), plan.statement)
            if self._is_true_predicate(predicate):
                return child
            pushed, residual = self._push_predicates(child, predicate, index_columns=index_columns)
            if residual is None:
                return pushed
            properties = dict(plan.properties)
            properties["predicate"] = residual
            return replace(plan, properties=properties, children=(pushed,))

        properties = {
            key: self._rewrite_plan_value(value)
            for key, value in dict(plan.properties).items()
        }
        children = tuple(self._rewrite(child, index_columns=index_columns) for child in plan.children)
        if properties == dict(plan.properties) and children == plan.children:
            return plan
        return replace(plan, properties=properties, children=children)

    def _choose_scan(
        self,
        plan: PlanNode,
        predicate: object,
        index_columns: Mapping[str, Collection[str]],
    ) -> PlanNode:
        if plan.kind != "SeqScan":
            return plan
        table = plan.properties.get("table")
        if not isinstance(table, str):
            return plan
        alias = plan.properties.get("alias")
        available = {name.lower() for name in index_columns.get(table.lower(), ())}
        if not self._predicate_uses_index(predicate, table, alias, available):
            return plan
        columns = self._indexable_columns(predicate, table, alias)
        column = next((item for item in columns if item.name.lower() in available), None)
        if column is None:
            return plan
        if self.choose_scan(table, has_usable_index=True) != "IndexScan":
            return plan
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
    def _indexable_columns(cls, predicate: object, table: str, alias: object) -> tuple[ColumnRef, ...]:
        qualifiers = {table.lower()}
        if isinstance(alias, str):
            qualifiers.add(alias.lower())
        if isinstance(predicate, BinaryOp):
            operator = predicate.operator.upper()
            if operator in {"AND", "OR"}:
                return (*cls._indexable_columns(predicate.left, table, alias),
                        *cls._indexable_columns(predicate.right, table, alias))
            if operator not in {"=", "<", "<=", ">", ">=", "LIKE"}:
                return ()
            for column, other in ((predicate.left, predicate.right), (predicate.right, predicate.left)):
                if isinstance(column, ColumnRef) and cls._constant_expression(other):
                    if not column.table or column.table.lower() in qualifiers:
                        if operator != "LIKE" or (isinstance(other, Literal) and isinstance(other.value, str)
                                                   and not any(marker in other.value for marker in ("%", "_"))):
                            return (column,)
            return ()
        if isinstance(predicate, IsNull) and isinstance(predicate.expression, ColumnRef):
            column = predicate.expression
            return (column,) if not column.table or column.table.lower() in qualifiers else ()
        if isinstance(predicate, InPredicate) and not predicate.negated and isinstance(predicate.expression, ColumnRef):
            column = predicate.expression
            if column.table and column.table.lower() not in qualifiers:
                return ()
            return (column,) if all(cls._constant_expression(value) for value in predicate.values) else ()
        if isinstance(predicate, BetweenPredicate) and isinstance(predicate.expression, ColumnRef):
            column = predicate.expression
            if column.table and column.table.lower() not in qualifiers:
                return ()
            return (column,) if cls._constant_expression(predicate.lower) and cls._constant_expression(predicate.upper) else ()
        return ()

    @classmethod
    def _predicate_uses_index(
        cls,
        predicate: object,
        table: str,
        alias: object,
        available: set[str],
    ) -> bool:
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return (cls._predicate_uses_index(predicate.left, table, alias, available)
                    or cls._predicate_uses_index(predicate.right, table, alias, available))
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "OR":
            return (cls._predicate_uses_index(predicate.left, table, alias, available)
                    and cls._predicate_uses_index(predicate.right, table, alias, available))
        return any(column.name.lower() in available for column in cls._indexable_columns(predicate, table, alias))

    @classmethod
    def _rewrite_expr(cls, expression: object) -> object:
        """递归折叠表达式，并应用安全的布尔恒等式。"""

        if expression is None or isinstance(expression, (Literal, ColumnRef, Parameter, Star)):
            return expression
        if isinstance(expression, UnaryOp):
            operand = cls._rewrite_expr(expression.operand)
            rewritten = cls._replace_node(expression, operand=operand)
            if not isinstance(operand, Literal):
                return rewritten
            if expression.operator.upper() == "NOT":
                value = None if operand.value is None else not bool(operand.value)
                return cls._literal(value, expression)
            if operand.value is None:
                return cls._literal(None, expression)
            try:
                if expression.operator == "+":
                    return cls._literal(+operand.value, expression)
                if expression.operator == "-":
                    return cls._literal(-operand.value, expression)
            except (TypeError, ValueError, OverflowError):
                return rewritten
            return rewritten
        if isinstance(expression, BinaryOp):
            left = cls._rewrite_expr(expression.left)
            right = cls._rewrite_expr(expression.right)
            operator = expression.operator.upper()
            simplified = cls._simplify_boolean(operator, left, right, expression)
            if simplified is not None:
                return simplified
            if isinstance(left, Literal) and isinstance(right, Literal):
                folded, value = cls._fold_binary(operator, left.value, right.value)
                if folded:
                    return cls._literal(value, expression)
            return cls._replace_node(expression, left=left, right=right)
        if isinstance(expression, IsNull):
            child = cls._rewrite_expr(expression.expression)
            if isinstance(child, Literal):
                value = child.value is None
                return cls._literal(not value if expression.negated else value, expression)
            return cls._replace_node(expression, expression=child)
        if isinstance(expression, InPredicate):
            child = cls._rewrite_expr(expression.expression)
            values = tuple(cls._rewrite_expr(value) for value in expression.values)
            rewritten = cls._replace_node(expression, expression=child, values=values)
            if isinstance(child, Literal) and all(isinstance(value, Literal) for value in values):
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
                return cls._literal(result, expression)
            return rewritten
        if isinstance(expression, BetweenPredicate):
            child = cls._rewrite_expr(expression.expression)
            lower = cls._rewrite_expr(expression.lower)
            upper = cls._rewrite_expr(expression.upper)
            rewritten = cls._replace_node(expression, expression=child, lower=lower, upper=upper)
            if all(isinstance(value, Literal) for value in (child, lower, upper)):
                result = cls._and_truth(
                    compare_values(child.value, lower.value, ">="),
                    compare_values(child.value, upper.value, "<="),
                )
                if expression.negated and result is not None:
                    result = not result
                return cls._literal(result, expression)
            return rewritten
        if isinstance(expression, FunctionCall):
            return cls._replace_node(expression, args=tuple(cls._rewrite_expr(arg) for arg in expression.args))
        if isinstance(expression, Subquery):
            query = cls._rewrite_statement(expression.query)
            return cls._replace_node(expression, query=query)
        return expression

    @classmethod
    def _rewrite_statement(cls, statement: Statement | None) -> Statement | None:
        if statement is None:
            return None
        if isinstance(statement, Select):
            return cls._replace_node(
                statement,
                items=tuple(
                    cls._replace_node(item, expression=cls._rewrite_expr(item.expression))
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
                    cls._replace_node(item, expression=cls._rewrite_expr(item.expression))
                    for item in statement.order_by
                ),
                union=cls._rewrite_statement(statement.union),
            )
        if isinstance(statement, Explain):
            return cls._replace_node(statement, statement=cls._rewrite_statement(statement.statement))
        if isinstance(statement, Update):
            return cls._replace_node(
                statement,
                assignments=tuple((column, cls._rewrite_expr(value)) for column, value in statement.assignments),
                where=cls._rewrite_expr(statement.where),
            )
        if isinstance(statement, Delete):
            return cls._replace_node(statement, where=cls._rewrite_expr(statement.where))
        if isinstance(statement, Insert):
            return cls._replace_node(
                statement,
                values=tuple(tuple(cls._rewrite_expr(value) for value in row) for row in statement.values),
            )
        if isinstance(statement, CreateView):
            return cls._replace_node(statement, query=cls._rewrite_statement(statement.query))
        return statement

    @classmethod
    def _rewrite_plan_value(cls, value: object) -> object:
        if isinstance(value, Expr):
            return cls._rewrite_expr(value)
        if isinstance(value, SelectItem):
            return cls._replace_node(value, expression=cls._rewrite_expr(value.expression))
        if isinstance(value, OrderItem):
            return cls._replace_node(value, expression=cls._rewrite_expr(value.expression))
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
        return replace(source, **changes).copy_source_metadata_from(source)

    @classmethod
    def _literal(cls, value: object, source: Node) -> Literal:
        return Literal(value).copy_source_metadata_from(source)

    @staticmethod
    def _and_truth(left: bool | None, right: bool | None) -> bool | None:
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return None

    @classmethod
    def _simplify_boolean(cls, operator: str, left: object, right: object, source: Node) -> Expr | None:
        if operator not in {"AND", "OR"}:
            return None
        if isinstance(left, Literal) and isinstance(right, Literal):
            folded, value = cls._fold_binary(operator, left.value, right.value)
            return cls._literal(value, source) if folded else None
        if operator == "AND":
            if isinstance(left, Literal) and left.value is False or isinstance(right, Literal) and right.value is False:
                return cls._literal(False, source)
            if isinstance(left, Literal) and left.value is True:
                return right if isinstance(right, Expr) else None
            if isinstance(right, Literal) and right.value is True:
                return left if isinstance(left, Expr) else None
        else:
            if isinstance(left, Literal) and left.value is True or isinstance(right, Literal) and right.value is True:
                return cls._literal(True, source)
            if isinstance(left, Literal) and left.value is False:
                return right if isinstance(right, Expr) else None
            if isinstance(right, Literal) and right.value is False:
                return left if isinstance(left, Expr) else None
        if left == right and isinstance(left, Expr):
            return left
        return None

    @classmethod
    def _fold_binary(cls, operator: str, left: object, right: object) -> tuple[bool, object]:
        if operator == "AND":
            return True, cls._and_truth(left if isinstance(left, bool) else None, right if isinstance(right, bool) else None)
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
            pattern = "^" + re.escape(str(right)).replace(r"%", ".*").replace(r"_", ".") + "$"
            matched = re.match(pattern, str(left), flags=re.DOTALL) is not None
            return True, not matched if operator == "NOT LIKE" else matched
        try:
            if operator == "+":
                return True, left + right  # type: ignore[operator]
            if operator == "-":
                return True, left - right  # type: ignore[operator]
            if operator == "*":
                return True, left * right  # type: ignore[operator]
            if operator == "/":
                if right == 0:
                    return False, None
                return True, left / right  # type: ignore[operator]
            if operator == "%":
                if right == 0:
                    return False, None
                return True, left % right  # type: ignore[operator]
            if operator == "||":
                return True, str(left) + str(right)
        except (TypeError, ValueError, OverflowError):
            return False, None
        return False, None

    @classmethod
    def _is_true_predicate(cls, expression: Expr) -> bool:
        return isinstance(expression, Literal) and expression.value is True

    @classmethod
    def _is_false_predicate(cls, expression: Expr) -> bool:
        # WHERE 中 UNKNOWN 与 FALSE 一样不会保留记录。
        return isinstance(expression, Literal) and expression.value is not True

    @classmethod
    def _split_conjunction(cls, predicate: Expr) -> tuple[Expr, ...]:
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return (*cls._split_conjunction(predicate.left), *cls._split_conjunction(predicate.right))
        return (predicate,)

    @staticmethod
    def _combine_conjunction(predicates: Collection[Expr]) -> Expr | None:
        items = tuple(predicates)
        if not items:
            return None
        result = items[0]
        for predicate in items[1:]:
            result = BinaryOp(result, "AND", predicate)
        return result

    @classmethod
    def _predicate_relations(cls, predicate: Expr) -> set[str] | None:
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
        pushed, residual = self._push_into_subtree(child, atoms, index_columns=index_columns)
        return pushed, self._combine_conjunction(residual)

    def _push_into_subtree(
        self,
        plan: PlanNode,
        predicates: Collection[Expr],
        *,
        index_columns: Mapping[str, Collection[str]],
    ) -> tuple[PlanNode, tuple[Expr, ...]]:
        items = tuple(predicates)
        if not items:
            return plan, ()
        if plan.kind == "Join" and len(plan.children) == 2 and str(plan.properties.get("join_type", "")).upper() in {"INNER", "CROSS"}:
            left, right = plan.children
            left_relations = self._plan_relations(left)
            right_relations = self._plan_relations(right)
            left_items = tuple(item for item in items if self._can_push_to(item, left_relations))
            right_items = tuple(
                item for item in items
                if item not in left_items and self._can_push_to(item, right_relations)
            )
            residual = tuple(item for item in items if item not in left_items and item not in right_items)
            left, left_residual = self._push_into_subtree(left, left_items, index_columns=index_columns)
            right, right_residual = self._push_into_subtree(right, right_items, index_columns=index_columns)
            rewritten = replace(plan, children=(left, right))
            return rewritten, (*left_residual, *right_residual, *residual)
        # HOW：单个扫描节点可能同时包含真实表名和别名，不能用关系名集合
        # 的长度判断是否为单表；只要节点本身是扫描，就可以接收单表谓词。
        if plan.kind in {"SeqScan", "IndexScan"}:
            predicate = self._combine_conjunction(items)
            if predicate is not None:
                return self._wrap_pushed_filter(plan, predicate, index_columns=index_columns), ()
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
            return PlanNode("Filter", {"predicate": predicate, "pushed": True}, (scan,), plan.statement)
        return PlanNode("Filter", {"predicate": predicate}, (plan,), plan.statement)


__all__ = ["CostEstimate", "Optimizer", "PlanCache", "StatisticsStore"]

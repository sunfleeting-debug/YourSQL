"""阶段 4（续）：规则式优化器。

实现的 6 条规则：
  1. 常量折叠 ConstantFolding        : age > 10 + 8  ->  age > 18
  2. 布尔化简 BooleanSimplification  : x AND TRUE -> x；NOT NOT x -> x
  3. 谓词分解 PredicateSplitting     : a AND b -> Filter(a) 之上再 Filter(b)
  4. 谓词下推 PredicatePushdown      : Filter(Project(X)) -> Project(Filter(X))
  5. 冗余节点消除 RedundantNodeElim  : Filter(TRUE) / 恒等 Project 直接删除
  6. 投影裁剪 ProjectionPruning      : SeqScan 只读取真正需要的列

优化前后结构可通过 Planner 与 Optimizer 的输出直接对比（见 EXPLAIN）。
"""

from __future__ import annotations

from database_system.sql_compiler import ast_nodes as ast
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
    collect_columns,
    is_false_literal,
    is_true_literal,
)
from database_system.utils.constants import (
    ARITHMETIC_OPS,
    BOOL_TYPE,
    COMPARISON_OPS,
    INT_TYPE,
    NULL_TYPE,
)

RULE_FOLD = "常量折叠 ConstantFolding"
RULE_BOOL = "布尔化简 BooleanSimplification"
RULE_SPLIT = "谓词分解 PredicateSplitting"
RULE_PUSHDOWN = "谓词下推 PredicatePushdown"
RULE_REDUNDANT = "冗余节点消除 RedundantNodeElimination"
RULE_PRUNING = "投影裁剪 ProjectionPruning"


def _trunc_div(a: int, b: int) -> int:
    """C 语义的整数除法（向零取整）。"""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _is_literal(node) -> bool:
    return isinstance(node, ast.Literal)


class Optimizer:
    def __init__(self):
        self.applied: list = []

    def _rule(self, name: str) -> None:
        if name not in self.applied:
            self.applied.append(name)

    # ------------------------------ 入口 ------------------------------

    def optimize(self, plan: PlanNode):
        """返回 (优化后的计划, 生效的规则列表)。"""
        self.applied = []
        if isinstance(plan, (CreateTablePlan, DropTablePlan, InsertPlan)):
            return plan, list(self.applied)

        node = self._rewrite_exprs(plan)          # 规则 1、2
        for _ in range(3):
            node = self._restructure(node)        # 规则 3、4、5
        self._prune(node, None)                   # 规则 6（原地修改 SeqScan.projection）
        return node, list(self.applied)

    # ------------------------------ 规则 1 / 2：表达式改写 ------------------------------

    def _rewrite_exprs(self, node: PlanNode) -> PlanNode:
        if isinstance(node, FilterPlan):
            node.predicate = self._rewrite_expr(node.predicate)
        elif isinstance(node, ProjectPlan):
            for item in node.items:
                item.expr = self._rewrite_expr(item.expr)
        elif isinstance(node, OrderByPlan):
            node.keys = [(self._rewrite_expr(e), d) for e, d in node.keys]
        elif isinstance(node, UpdatePlan):
            node.assignments = [(c, self._rewrite_expr(e)) for c, e in node.assignments]
        for child in node.children():
            if child is not None:
                self._rewrite_exprs(child)
        return node

    def _rewrite_expr(self, e):
        if e is None:
            return None
        if isinstance(e, ast.Binary):
            e.left = self._rewrite_expr(e.left)
            e.right = self._rewrite_expr(e.right)
            simplified = self._simplify_bool(e)
            if simplified is not e:
                self._rule(RULE_BOOL)
                return simplified
            folded = self._fold_binary(e)
            if folded is not None:
                self._rule(RULE_FOLD)
                return folded
            return e
        if isinstance(e, ast.Unary):
            e.operand = self._rewrite_expr(e.operand)
            simplified = self._simplify_unary(e)
            if simplified is not e:
                self._rule(RULE_BOOL)
                return simplified
            folded = self._fold_unary(e)
            if folded is not None:
                self._rule(RULE_FOLD)
                return folded
            return e
        if isinstance(e, ast.IsNull):
            e.operand = self._rewrite_expr(e.operand)
            if _is_literal(e.operand):
                self._rule(RULE_FOLD)
                value = e.operand.value is None
                if e.negated:
                    value = not value
                return ast.Literal(value, BOOL_TYPE, e.line, e.column)
            return e
        return e

    # ---- 布尔化简 ----

    def _simplify_bool(self, e: ast.Binary):
        op = e.op.upper()
        left, right = e.left, e.right
        if op == "AND":
            if is_true_literal(right):
                return left
            if is_true_literal(left):
                return right
            if is_false_literal(left) or is_false_literal(right):
                return ast.Literal(False, BOOL_TYPE, e.line, e.column)
        elif op == "OR":
            if is_false_literal(right):
                return left
            if is_false_literal(left):
                return right
            if is_true_literal(left) or is_true_literal(right):
                return ast.Literal(True, BOOL_TYPE, e.line, e.column)
        return e

    def _simplify_unary(self, e: ast.Unary):
        if e.op.upper() == "NOT":
            inner = e.operand
            if isinstance(inner, ast.Unary) and inner.op.upper() == "NOT":
                return inner.operand
        return e

    # ---- 常量折叠 ----

    def _fold_binary(self, e: ast.Binary):
        left, right = e.left, e.right
        if not (_is_literal(left) and _is_literal(right)):
            return None
        lv, rv = left.value, right.value
        op = e.op
        line, column = e.line, e.column

        if op in ARITHMETIC_OPS:
            if lv is None or rv is None:
                return ast.Literal(None, INT_TYPE, line, column)
            if op == "/":
                if rv == 0:
                    return None  # 除零交给执行期报错，不在此折叠
                return ast.Literal(_trunc_div(lv, rv), INT_TYPE, line, column)
            value = {
                "+": lv + rv,
                "-": lv - rv,
                "*": lv * rv,
            }[op]
            return ast.Literal(value, INT_TYPE, line, column)

        if op in COMPARISON_OPS:
            if lv is None or rv is None:
                return ast.Literal(None, BOOL_TYPE, line, column)
            value = {
                "=": lv == rv,
                "!=": lv != rv,
                ">": lv > rv,
                ">=": lv >= rv,
                "<": lv < rv,
                "<=": lv <= rv,
            }[op]
            return ast.Literal(value, BOOL_TYPE, line, column)

        if op.upper() in ("AND", "OR"):
            is_and = op.upper() == "AND"
            if is_and:
                if lv is False or rv is False:
                    return ast.Literal(False, BOOL_TYPE, line, column)
                if lv is None or rv is None:
                    return ast.Literal(None, BOOL_TYPE, line, column)
                return ast.Literal(True, BOOL_TYPE, line, column)
            if lv is True or rv is True:
                return ast.Literal(True, BOOL_TYPE, line, column)
            if lv is None or rv is None:
                return ast.Literal(None, BOOL_TYPE, line, column)
            return ast.Literal(False, BOOL_TYPE, line, column)
        return None

    def _fold_unary(self, e: ast.Unary):
        if not _is_literal(e.operand):
            return None
        v = e.operand.value
        op = e.op.upper()
        if op == "NOT":
            return ast.Literal(None if v is None else (not v), BOOL_TYPE, e.line, e.column)
        if v is None:
            return ast.Literal(None, INT_TYPE, e.line, e.column)
        if op == "-":
            return ast.Literal(-v, INT_TYPE, e.line, e.column)
        if op == "+":
            return ast.Literal(+v, INT_TYPE, e.line, e.column)
        return None

    # ------------------------------ 规则 3 / 4 / 5：结构改写 ------------------------------

    def _restructure(self, node: PlanNode) -> PlanNode:
        if node is None:
            return None
        kids = [self._restructure(c) for c in node.children() if c is not None]
        if kids and hasattr(node, "with_children"):
            try:
                node = node.with_children(kids)
            except NotImplementedError:
                pass

        # 规则 5：Filter(TRUE) 直接删除
        if isinstance(node, FilterPlan) and is_true_literal(node.predicate):
            self._rule(RULE_REDUNDANT)
            return node.child

        # 规则 5：恒等 Project 删除
        if isinstance(node, ProjectPlan) and not node.distinct and self._is_identity(node):
            self._rule(RULE_REDUNDANT)
            return node.child

        # 规则 3：谓词分解 a AND b -> Filter(a) 之下 Filter(b)
        if (
            isinstance(node, FilterPlan)
            and isinstance(node.predicate, ast.Binary)
            and node.predicate.op.upper() == "AND"
            and not is_true_literal(node.predicate.left)
            and not is_true_literal(node.predicate.right)
        ):
            self._rule(RULE_SPLIT)
            inner = FilterPlan(node.predicate.right, node.child)
            return FilterPlan(node.predicate.left, inner)

        # 规则 4：谓词下推 Filter(Project(X)) -> Project(Filter(X))
        if isinstance(node, FilterPlan) and isinstance(node.child, ProjectPlan):
            project = node.child
            grand = project.child
            if grand is not None:
                available = {c.lower() for c in grand.output_columns()}
                needed = collect_columns(node.predicate)
                if needed <= available:
                    self._rule(RULE_PUSHDOWN)
                    return ProjectPlan(
                        project.items,
                        project.distinct,
                        FilterPlan(node.predicate, grand),
                    )
        return node

    @staticmethod
    def _is_identity(node: ProjectPlan) -> bool:
        """Project 是否为恒等投影：输出列与子算子输出列完全一致且顺序相同。"""
        child = node.child
        if child is None:
            return False
        child_cols = [c.lower() for c in child.output_columns()]
        if len(child_cols) != len(node.items):
            return False
        for item, name in zip(node.items, child_cols):
            if not isinstance(item.expr, ast.Identifier):
                return False
            if (item.expr.ref.column_name if item.expr.ref else item.expr.name).lower() != name:
                return False
            if item.name.lower() != name:
                return False
        return True

    # ------------------------------ 规则 6：投影裁剪 ------------------------------

    def _prune(self, node: PlanNode, required) -> None:
        """自顶向下计算每层的必备列，下推到 SeqScan 的 projection。

        required 为 None 表示「上一层需要下层的全部列」，
        只有 Project 才是真正的收窄点，这样即使恒等 Project 被冗余消除，
        也不会误把列裁剪掉。
        """
        if node is None:
            return
        if isinstance(node, SeqScanPlan):
            if required is None:
                node.projection = None
                return
            idx = sorted({c.ordinal for c in node.columns if c.name.lower() in required})
            if len(idx) == len(node.columns):
                node.projection = None
            else:
                node.projection = idx
                if idx:
                    self._rule(RULE_PRUNING)
            return

        if isinstance(node, ProjectPlan):
            # 投影裁剪的收窄点：只保留 SELECT 列表真正引用的列
            req = set()
            for item in node.items:
                req |= collect_columns(item.expr)
            if required:
                req |= set(required) & {i.name.lower() for i in node.items}
            self._prune(node.child, req)
        elif isinstance(node, FilterPlan):
            if required is None:
                self._prune(node.child, None)
            else:
                self._prune(node.child, set(required) | collect_columns(node.predicate))
        elif isinstance(node, OrderByPlan):
            if required is None:
                self._prune(node.child, None)
            else:
                req = set(required)
                for e, _ in node.keys:
                    req |= collect_columns(e)
                self._prune(node.child, req)
        elif isinstance(node, LimitPlan):
            self._prune(node.child, required)
        elif isinstance(node, DeletePlan):
            # 删除只需要 rowid
            self._prune(node.child, set())
        elif isinstance(node, UpdatePlan):
            req = {c.name.lower() for c in node.columns}
            for _, e in node.assignments:
                req |= collect_columns(e)
            self._prune(node.child, req)
        else:
            for child in node.children():
                self._prune(child, None)

"""SQL 表达式的编译、常量折叠与运行时求值。"""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass, replace
from datetime import date, timedelta
from typing import Callable, Iterator

from ..common import (
    DataType,
    ExecutionError,
    YourSQLError,
    compare_values,
    sql_truth,
    to_decimal,
)
from ..sql.ast import (
    BetweenPredicate,
    BinaryOp,
    CaseExpression,
    CastExpression,
    ColumnRef,
    ExistsPredicate,
    Expr,
    FunctionCall,
    InPredicate,
    IsNull,
    Literal,
    Node,
    Parameter,
    Select,
    Star,
    Subquery,
    UnaryOp,
)


_MISSING = object()
_AMBIGUOUS = object()
_AGGREGATE_NAMES = {"count", "sum", "avg", "min", "max"}


def _walk_expressions(value: object) -> Iterator[Node]:
    """自顶向下遍历表达式里的所有节点；不进入子查询内部的 Select。

    WHY：``CaseExpression.branches`` 是「元组的元组」，只按 dataclass 字段浅层遍历会
    整块跳过 WHEN/THEN 里的子表达式，导致聚合检测与按需列裁剪漏掉它们。
    """

    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_expressions(item)
        return
    if is_dataclass(value):
        yield value
        if isinstance(value, (Subquery, ExistsPredicate)):
            return
        for field in fields(value):
            yield from _walk_expressions(getattr(value, field.name))


def _cast_value(value: object, target: DataType) -> object:
    """执行 CAST 的取值转换。

    HOW：不走 ``Value.coerce``——那里的转换规则同时被 INSERT 使用，放宽它会顺带
    改变「字符串能不能写进数值列」的既有语义。
    """

    if value is None:
        return None
    if target is DataType.VARCHAR:
        return str(value)
    if target is DataType.DECIMAL:
        return to_decimal(value)
    if isinstance(value, bool):
        if target is DataType.BOOLEAN:
            return value
        value = int(value)
    if target is DataType.INT:
        if isinstance(value, str):
            return int(to_decimal(value.strip()))
        return int(value)  # type: ignore[arg-type]
    if target is DataType.FLOAT:
        return float(value)  # type: ignore[arg-type]
    if target is DataType.BOOLEAN:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1"}
        return bool(value)
    raise ExecutionError(f"CAST 不支持目标类型 {target.value}")


def _with_location(
    error: YourSQLError, location: tuple[int, int] | None
) -> YourSQLError:
    """把表达式异常补到 SQL 源码位置。"""

    if location is None or error.line is not None:
        return error
    if type(error) is YourSQLError:
        return YourSQLError(
            error.message, error.code, location[0], location[1], dict(error.details)
        )
    return type(error)(
        error.message, line=location[0], column=location[1], **error.details
    )


def _with_node_location(error: YourSQLError, node: Node | None) -> YourSQLError:
    """把表达式异常补到 AST 节点。"""

    return _with_location(error, node.source_location if node is not None else None)


def _logical_and(left: object, right: object) -> object:
    """执行 SQL 三值逻辑 AND。"""

    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def _logical_or(left: object, right: object) -> object:
    """执行 SQL 三值逻辑 OR。"""

    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return None


def _literal_pattern(expression: Expr) -> str | None:
    """返回常量 LIKE 模式；动态模式交给逐行求值。"""

    if isinstance(expression, Literal) and isinstance(expression.value, str):
        return expression.value
    return None


def _compile_like(pattern: object) -> Callable[[str], bool]:
    """把 LIKE 模式预编译成正则匹配器。"""

    regex = re.compile(
        "^" + re.escape(str(pattern)).replace("%", ".*").replace("_", ".") + "$",
        re.DOTALL,
    )
    return lambda text: regex.match(text) is not None


def _eval_date_function(values: list[object], source: Node | None = None) -> str | None:
    """执行 TPC-H/SQLite 风格的 DATE 文本函数。"""

    if not values or values[0] is None:
        return None
    try:
        current = date.fromisoformat(str(values[0]))
    except ValueError as exc:
        raise _with_node_location(
            ExecutionError(f"DATE 参数必须是 YYYY-MM-DD: {values[0]!r}"), source
        ) from exc
    for modifier in values[1:]:
        if modifier is None:
            return None
        match = re.fullmatch(
            r"([+-])(\d+)\s+(day|days|month|months|year|years)",
            str(modifier).strip().lower(),
        )
        if match is None:
            raise _with_node_location(
                ExecutionError(f"不支持 DATE 修饰符 {modifier!r}"), source
            )
        sign = 1 if match.group(1) == "+" else -1
        amount = sign * int(match.group(2))
        unit = match.group(3)
        if unit.startswith("day"):
            current += timedelta(days=amount)
            continue
        if unit.startswith("month"):
            month_index = current.year * 12 + current.month - 1 + amount
            year, month_index = divmod(month_index, 12)
            month = month_index + 1
            day = min(current.day, _days_in_month(year, month))
            current = date(year, month, day)
            continue
        year = current.year + amount
        day = min(current.day, _days_in_month(year, current.month))
        current = date(year, current.month, day)
    return current.isoformat()


def _eval_substring_function(
    values: list[object], source: Node | None = None
) -> str | None:
    """``SUBSTRING(text, start[, length])`` / ``SUBSTR``，沿用 SQLite 的 1 起算语义。

    WHY：TPC-H 的 sqlite 方言用 ``SUBSTRING(c_phone, 1, 2)`` 取国家码（Q22）。
    ``start`` 为负时从末尾倒数；越界一律截断而不是报错，与 SQLite 一致。
    """

    if len(values) < 2 or values[0] is None or values[1] is None:
        return None
    text = str(values[0])
    try:
        start = int(values[1])
    except (TypeError, ValueError) as exc:
        raise _with_node_location(
            ExecutionError(f"SUBSTRING 的起始位置必须是整数: {values[1]!r}"), source
        ) from exc
    length: int | None = None
    if len(values) > 2 and values[2] is not None:
        try:
            length = int(values[2])
        except (TypeError, ValueError) as exc:
            raise _with_node_location(
                ExecutionError(f"SUBSTRING 的长度必须是整数: {values[2]!r}"), source
            ) from exc
    index = start - 1 if start > 0 else len(text) + start
    if index < 0:
        if length is None:
            index = 0
        else:
            length += index
            index = 0
    if index > len(text):
        return ""
    if length is None:
        return text[index:]
    return text[index : index + max(0, length)]


def _eval_strftime_function(
    values: list[object], source: Node | None = None
) -> str | None:
    """SQLite 风格的日期格式化：``STRFTIME('%Y', '1996-01-02')`` → ``'1996'``。

    WHY：TPC-H 的 sqlite 方言用 ``CAST(STRFTIME('%Y', o_orderdate) AS INTEGER)``
    取年份（Q7/Q8/Q9）。只做格式化，不引入新的日期语义：入参必须能按
    ``YYYY-MM-DD`` 解析（``DATE()`` 的输出与之同构）。
    """

    if len(values) < 2 or values[0] is None or values[1] is None:
        return None
    pattern = str(values[0])
    text = str(values[1])
    try:
        current = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise _with_node_location(
            ExecutionError(f"STRFTIME 的日期参数必须是 YYYY-MM-DD: {values[1]!r}"),
            source,
        ) from exc
    try:
        return current.strftime(pattern)
    except ValueError as exc:
        raise _with_node_location(
            ExecutionError(f"不支持的 STRFTIME 格式 {pattern!r}"), source
        ) from exc


def _days_in_month(year: int, month: int) -> int:
    """返回指定月份的天数。"""

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


class ExpressionEvaluator:
    """负责 AST 表达式求值与常量折叠，不负责扫描、连接或结果编排。"""

    def _fold_statement(self, statement: Select) -> Select:
        """执行前把常量表达式预求值；返回新对象，不改动原始 AST。

        WHY：`DATE('1994-01-01')`、`0.06 - 0.01` 这类常量原来每行重算一次。
        """

        return replace(
            statement,
            items=tuple(
                replace(item, expression=self._fold_constants(item.expression))
                for item in statement.items
            ),
            joins=tuple(
                replace(clause, on=self._fold_constants(clause.on))
                for clause in statement.joins
            ),
            where=self._fold_constants(statement.where),
            group_by=tuple(
                self._fold_constants(expression) for expression in statement.group_by
            ),
            having=self._fold_constants(statement.having),
            order_by=tuple(
                replace(item, expression=self._fold_constants(item.expression))
                for item in statement.order_by
            ),
        )

    def _fold_constants(self, expression: Expr | None) -> Expr | None:
        if expression is None:
            return None
        folded, _constant, _value = self._fold_node(expression)
        return folded

    def _fold_node(self, expression: Expr) -> tuple[Expr, bool, object]:
        """自下而上折叠常量子树，返回（节点, 是否常量, 常量值）。

        HOW：只在求值成功时替换为 Literal；失败（如常量除零）保留原节点，
        使错误时机与折叠前一致——例如 `WHERE FALSE AND 1/0 = 1` 仍由短路决定是否报错。
        """

        if isinstance(expression, Literal):
            return expression, True, expression.value
        if isinstance(expression, (ColumnRef, Parameter, Star)):
            return expression, False, None
        if isinstance(expression, (Subquery, ExistsPredicate)):
            return expression, False, None
        if (
            isinstance(expression, FunctionCall)
            and expression.name.lower() in _AGGREGATE_NAMES
        ):
            return expression, False, None
        updates: dict[str, object] = {}
        constant = True
        for field in fields(expression):
            current = getattr(expression, field.name)
            if isinstance(current, Expr):
                child, child_constant, _child_value = self._fold_node(current)
                constant &= child_constant
                if child is not current:
                    updates[field.name] = child
            elif isinstance(current, (tuple, list)):
                items = list(current)
                changed = False
                for position, item in enumerate(items):
                    # HOW：递归进嵌套序列，CASE 的 branches 是「元组的元组」也要能折叠到。
                    child, child_constant = self._fold_children(item)
                    constant &= child_constant
                    if child is not item:
                        items[position] = child
                        changed = True
                # WHY：只在真的变化时重建节点，否则会丢掉节点上的 source_location，错误行列会不准。
                if changed:
                    updates[field.name] = tuple(items)
            elif is_dataclass(current) and not isinstance(current, Expr):
                # 嵌套的非表达式节点（如子查询里的 Select）不参与折叠。
                constant = False
        rebuilt = replace(expression, **updates) if updates else expression
        if not constant:
            return rebuilt, False, None
        try:
            value = self._eval_expr(rebuilt, {})
        except Exception:  # noqa: BLE001 - 折叠失败时保留原节点，不影响运行时语义
            return rebuilt, False, None
        return Literal(value), True, value

    def _fold_children(self, value: object) -> tuple[object, bool]:
        """折叠一个子结构；返回（折叠后结构, 是否全为常量）。"""

        if isinstance(value, Expr):
            node, constant, _unused = self._fold_node(value)
            return node, constant
        if isinstance(value, (tuple, list)):
            items = list(value)
            changed = False
            constant = True
            for position, item in enumerate(items):
                child, child_constant = self._fold_children(item)
                constant &= child_constant
                if child is not item:
                    items[position] = child
                    changed = True
            return (tuple(items) if changed else value), constant
        return value, True

    def _compile_expr(
        self, expression: Expr | None
    ) -> Callable[[dict[str, object]], object]:
        """把表达式编译成闭包，避免逐行 isinstance 派发。"""

        if expression is None:
            return lambda _context: None
        if isinstance(expression, Literal):
            value = expression.value
            return lambda _context: value
        if isinstance(expression, ColumnRef):
            key = (
                f"{expression.table.lower()}.{expression.name.lower()}"
                if expression.table
                else expression.name.lower()
            )
            qualified = expression.qualified_name

            def evaluate_column(context: dict[str, object]) -> object:
                value = context.get(key, _MISSING)
                if value is _MISSING or value is _AMBIGUOUS:
                    raise _with_node_location(
                        ExecutionError(f"执行时找不到列 {qualified}"), expression
                    )
                return value

            return evaluate_column
        if isinstance(expression, UnaryOp):
            operand = self._compile_expr(expression.operand)
            operator = expression.operator
            if operator == "NOT":
                return lambda context: (
                    None if (value := operand(context)) is None else not bool(value)
                )

            def evaluate_unary(context: dict[str, object]) -> object:
                value = operand(context)
                if value is None:
                    return None
                try:
                    return +value if operator == "+" else -value
                except (TypeError, ValueError, OverflowError) as exc:
                    raise _with_node_location(
                        ExecutionError(f"一元运算失败: {exc}"), expression
                    ) from exc

            return evaluate_unary
        if isinstance(expression, BinaryOp):
            left = self._compile_expr(expression.left)
            right = self._compile_expr(expression.right)
            operator = expression.operator.upper()
            if operator == "AND":
                return lambda context: _logical_and(left(context), right(context))
            if operator == "OR":
                return lambda context: _logical_or(left(context), right(context))
            if operator in {"=", "==", "!=", "<>", "<", "<=", ">", ">="}:
                return lambda context: compare_values(
                    left(context), right(context), operator
                )
            if operator in {"LIKE", "NOT LIKE"}:
                fixed = _literal_pattern(expression.right)
                pattern = _compile_like(fixed) if fixed is not None else None

                def evaluate_like(context: dict[str, object]) -> object:
                    left_value = left(context)
                    right_value = right(context)
                    if left_value is None or right_value is None:
                        return None
                    matcher = (
                        pattern if pattern is not None else _compile_like(right_value)
                    )
                    matched = matcher(str(left_value))
                    return not matched if operator == "NOT LIKE" else matched

                return evaluate_like

            def evaluate_binary(context: dict[str, object]) -> object:
                left_value = left(context)
                right_value = right(context)
                if left_value is None or right_value is None:
                    return None
                try:
                    if operator == "+":
                        return left_value + right_value
                    if operator == "-":
                        return left_value - right_value
                    if operator == "*":
                        return left_value * right_value
                    if operator == "/":
                        if right_value == 0:
                            raise _with_node_location(
                                ExecutionError("除数不能为零"), expression
                            )
                        return left_value / right_value
                    if operator == "%":
                        return left_value % right_value
                    return str(left_value) + str(right_value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise _with_node_location(
                        ExecutionError(f"算术运算失败: {exc}"), expression
                    ) from exc

            return evaluate_binary
        if isinstance(expression, IsNull):
            inner = self._compile_expr(expression.expression)
            negated = expression.negated
            return lambda context: (
                (inner(context) is not None) if negated else (inner(context) is None)
            )
        if isinstance(expression, BetweenPredicate):
            value = self._compile_expr(expression.expression)
            lower = self._compile_expr(expression.lower)
            upper = self._compile_expr(expression.upper)
            negated = expression.negated

            def evaluate_between(context: dict[str, object]) -> object:
                result = compare_values(
                    value(context), lower(context), ">="
                ) and compare_values(value(context), upper(context), "<=")
                if result is None:
                    return None
                return not result if negated else result

            return evaluate_between
        if isinstance(expression, InPredicate):
            inner = self._compile_expr(expression.expression)
            negated = expression.negated
            static: list[object] = []
            dynamic: list[Callable[[dict[str, object]], object]] = []
            reused = False
            for value in expression.values:
                if isinstance(value, Subquery) and not self._subquery_is_correlated(
                    value.query
                ):
                    # WHY：不相关子查询的结果与行无关；避免每行重复执行。
                    result = self._execute_select(value.query)
                    static.extend(row[0] for row in result.rows if row)
                    reused = True
                else:
                    dynamic.append(self._compile_expr(value))

            def evaluate_in(context: dict[str, object]) -> object:
                left_value = inner(context)
                result: bool | None = False
                candidates = static if reused else ()
                for candidate in candidates:
                    compared = compare_values(left_value, candidate, "=")
                    if compared is True:
                        return not negated
                    if compared is None:
                        result = None
                for evaluator in dynamic:
                    compared = compare_values(left_value, evaluator(context), "=")
                    if compared is True:
                        return not negated
                    if compared is None:
                        result = None
                if negated:
                    return None if result is None else not result
                return result

            return evaluate_in
        if isinstance(expression, CaseExpression):
            operand = (
                self._compile_expr(expression.operand)
                if expression.operand is not None
                else None
            )
            branches = tuple(
                (self._compile_expr(condition), self._compile_expr(result))
                for condition, result in expression.branches
            )
            otherwise = (
                self._compile_expr(expression.otherwise)
                if expression.otherwise is not None
                else None
            )

            def evaluate_case(context: dict[str, object]) -> object:
                if operand is None:
                    for condition, result in branches:
                        if sql_truth(condition(context)):
                            return result(context)
                else:
                    subject = operand(context)
                    for condition, result in branches:
                        if compare_values(subject, condition(context), "=") is True:
                            return result(context)
                return otherwise(context) if otherwise is not None else None

            return evaluate_case
        if isinstance(expression, CastExpression):
            inner = self._compile_expr(expression.expression)
            target = expression.data_type

            def evaluate_cast(context: dict[str, object]) -> object:
                try:
                    return _cast_value(inner(context), target)
                except (TypeError, ValueError, ArithmeticError, YourSQLError) as exc:
                    raise _with_node_location(
                        ExecutionError(f"CAST 失败: {exc}"), expression
                    ) from exc

            return evaluate_cast
        if isinstance(expression, ExistsPredicate):
            if not self._subquery_is_correlated(expression.query):
                present = bool(self._execute_select(expression.query).rows)
                if expression.negated:
                    return lambda _context: not present
                return lambda _context: present

            def evaluate_exists(context: dict[str, object]) -> object:
                present = self._eval_exists(expression, context)
                return not present if expression.negated else present

            return evaluate_exists
        if isinstance(expression, Subquery):
            if not self._subquery_is_correlated(expression.query):
                # WHY：不相关子查询的结果与行无关；在编译期求值一次即可，避免逐行重跑。
                rows = self._execute_select(expression.query).rows
                if len(rows) > 1:
                    raise _with_node_location(
                        ExecutionError("标量子查询返回了多行"), expression
                    )
                value = rows[0][0] if rows and rows[0] else None
                return lambda _context: value
            return lambda context: self._eval_scalar_subquery(expression, context)
        return lambda context: self._eval_expr(expression, context)

    def _eval_scalar_subquery(
        self, expression: Subquery, context: dict[str, object]
    ) -> object:
        """执行标量子查询；把当前行上下文作为外层作用域传入以支持相关子查询。"""

        result = self._execute_select(expression.query, outer=context)
        if not result.rows:
            return None
        if len(result.rows) > 1:
            raise _with_node_location(
                ExecutionError("标量子查询返回了多行"), expression
            )
        return result.rows[0][0]

    def _eval_exists(
        self, expression: ExistsPredicate, context: dict[str, object]
    ) -> bool:
        """执行 EXISTS 子查询；只要有一行即成立。"""

        result = self._execute_select(expression.query, outer=context)
        return bool(result.rows)

    def _eval_expr(self, expression: Expr | None, context: dict[str, object]) -> object:
        """在一行查询上下文中求值；SQL 三值逻辑由本类统一处理。"""

        if expression is None:
            return None
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Parameter):
            return None
        if isinstance(expression, Star):
            return self._expand_star(context, expression.table)
        if isinstance(expression, ColumnRef):
            key = (
                f"{expression.table.lower()}.{expression.name.lower()}"
                if expression.table
                else expression.name.lower()
            )
            value = context.get(key, _MISSING)
            if value is _MISSING or value is _AMBIGUOUS:
                raise _with_node_location(
                    ExecutionError(f"执行时找不到列 {expression.qualified_name}"),
                    expression,
                )
            return value
        if isinstance(expression, UnaryOp):
            value = self._eval_expr(expression.operand, context)
            if expression.operator == "NOT":
                return None if value is None else not bool(value)
            if value is None:
                return None
            try:
                if expression.operator == "+":
                    return +value
                if expression.operator == "-":
                    return -value
            except (TypeError, ValueError, OverflowError) as exc:
                raise _with_node_location(
                    ExecutionError(f"一元运算失败: {exc}"), expression
                ) from exc
        if isinstance(expression, BinaryOp):
            left = self._eval_expr(expression.left, context)
            right = self._eval_expr(expression.right, context)
            operator = expression.operator.upper()
            if operator == "AND":
                return _logical_and(left, right)
            if operator == "OR":
                return _logical_or(left, right)
            if operator in {"=", "!=", "<>", "<", "<=", ">", ">="}:
                return compare_values(left, right, operator)
            if operator in {"LIKE", "NOT LIKE"}:
                if left is None or right is None:
                    return None
                matched = _compile_like(right)(str(left))
                return not matched if operator == "NOT LIKE" else matched
            if left is None or right is None:
                return None
            try:
                if operator == "+":
                    return left + right
                if operator == "-":
                    return left - right
                if operator == "*":
                    return left * right
                if operator == "/":
                    if right == 0:
                        raise _with_node_location(
                            ExecutionError("除数不能为零"), expression
                        )
                    return left / right
                if operator == "%":
                    return left % right
                if operator == "||":
                    return str(left) + str(right)
            except (TypeError, ValueError, OverflowError) as exc:
                raise _with_node_location(
                    ExecutionError(f"算术运算失败: {exc}"), expression
                ) from exc
        if isinstance(expression, IsNull):
            result = self._eval_expr(expression.expression, context) is None
            return not result if expression.negated else result
        if isinstance(expression, InPredicate):
            left = self._eval_expr(expression.expression, context)
            values: list[object] = []
            for value in expression.values:
                if isinstance(value, Subquery):
                    subquery = self._execute_select(value.query)
                    values.extend(row[0] for row in subquery.rows if row)
                else:
                    values.append(self._eval_expr(value, context))
            result: bool | None = False
            for value in values:
                compared = compare_values(left, value, "=")
                if compared is True:
                    result = True
                    break
                if compared is None:
                    result = None
            if expression.negated:
                return None if result is None else not result
            return result
        if isinstance(expression, BetweenPredicate):
            value = self._eval_expr(expression.expression, context)
            lower = self._eval_expr(expression.lower, context)
            upper = self._eval_expr(expression.upper, context)
            result = compare_values(value, lower, ">=") and compare_values(
                value, upper, "<="
            )
            if result is None:
                return None
            return not result if expression.negated else result
        if isinstance(expression, FunctionCall):
            try:
                return self._eval_function(expression, context)
            except (TypeError, ValueError, IndexError, OverflowError) as exc:
                raise _with_node_location(
                    ExecutionError(f"函数 {expression.name} 执行失败: {exc}"),
                    expression,
                ) from exc
        if isinstance(expression, CaseExpression):
            if expression.operand is None:
                for condition, result in expression.branches:
                    if sql_truth(self._eval_expr(condition, context)):
                        return self._eval_expr(result, context)
            else:
                subject = self._eval_expr(expression.operand, context)
                for condition, result in expression.branches:
                    compared = compare_values(
                        subject, self._eval_expr(condition, context), "="
                    )
                    if compared is True:
                        return self._eval_expr(result, context)
            if expression.otherwise is None:
                return None
            return self._eval_expr(expression.otherwise, context)
        if isinstance(expression, CastExpression):
            try:
                return _cast_value(
                    self._eval_expr(expression.expression, context), expression.data_type
                )
            except (TypeError, ValueError, ArithmeticError, YourSQLError) as exc:
                raise _with_node_location(
                    ExecutionError(f"CAST 失败: {exc}"), expression
                ) from exc
        if isinstance(expression, Subquery):
            return self._eval_scalar_subquery(expression, context)
        if isinstance(expression, ExistsPredicate):
            present = self._eval_exists(expression, context)
            return not present if expression.negated else present
        raise _with_node_location(
            ExecutionError(f"不支持表达式 {type(expression).__name__}"), expression
        )

    def _eval_function(
        self, function: FunctionCall, context: dict[str, object]
    ) -> object:
        """执行内置标量/聚合函数；聚合输入由查询层放入 ``__group__``。"""

        name = function.name.lower()
        group = context.get("__group__")
        if name in _AGGREGATE_NAMES and isinstance(group, list):
            if name == "count":
                if not function.args or isinstance(function.args[0], Star):
                    return len(group)
                values = [self._eval_expr(function.args[0], item) for item in group]
                return (
                    len({value for value in values if value is not None})
                    if function.distinct
                    else sum(value is not None for value in values)
                )
            values = (
                [self._eval_expr(function.args[0], item) for item in group]
                if function.args
                else []
            )
            values = [value for value in values if value is not None]
            if function.distinct:
                values = list(dict.fromkeys(values))
            if not values:
                return None
            if name == "sum":
                return sum(values)
            if name == "avg":
                return sum(values) / len(values)
            if name == "min":
                return min(values)
            return max(values)
        values = [self._eval_expr(argument, context) for argument in function.args]
        if name in {"lower", "upper", "length", "len", "abs"} and not values:
            raise _with_node_location(
                ExecutionError(f"函数 {function.name} 缺少参数"), function
            )
        if name == "lower":
            return str(values[0]).lower() if values and values[0] is not None else None
        if name == "upper":
            return str(values[0]).upper() if values and values[0] is not None else None
        if name in {"length", "len"}:
            return len(str(values[0])) if values and values[0] is not None else None
        if name == "abs":
            return abs(values[0]) if values and values[0] is not None else None
        if name == "coalesce":
            return next((value for value in values if value is not None), None)
        if name == "date":
            return _eval_date_function(values, function)
        if name == "strftime":
            return _eval_strftime_function(values, function)
        if name in {"substring", "substr"}:
            return _eval_substring_function(values, function)
        raise _with_node_location(
            ExecutionError(f"不支持函数 {function.name}"), function
        )

    def _contains_aggregate(self, expression: Expr | None) -> bool:
        """判断表达式树是否包含需要分组输入的聚合函数。

        HOW：按 dataclass 字段泛化遍历（含 CASE 的嵌套分支），新增表达式节点无需同步修改；
        子查询内部的聚合属于子查询自己的分组，不在此处计入。
        """

        if expression is None:
            return False
        return any(
            isinstance(node, FunctionCall) and node.name.lower() in _AGGREGATE_NAMES
            for node in _walk_expressions(expression)
        )


def constant_value(expression: Expr) -> tuple[bool, object]:
    """提取可静态确定的表达式值；返回（是否可确定, 值）。"""

    folded, constant, value = ExpressionEvaluator()._fold_node(expression)
    return (True, value) if constant and isinstance(folded, Literal) else (False, None)

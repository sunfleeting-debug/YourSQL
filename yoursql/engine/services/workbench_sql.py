"""工作台 SQL 边界、脱敏、阶段采集和结果列类型。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, TypeVar

from yoursql.common import ExecutionResult, JsonObject, YourSQLError, Value
from yoursql.sql.ast import ColumnRef, Explain, Select, Show, Star, Statement
from yoursql.sql.binder import Binder
from yoursql.sql.compiler import CompilationResult
from yoursql.sql.lexer import KEYWORDS, TokenKind, tokenize
from yoursql.sql.parser import Parser
from yoursql.planner.logical import plan_from_statement
from yoursql.engine.runtime.database import Database

T = TypeVar("T")
MAX_SQL = 64_000
MAX_STATEMENTS = 32

__all__ = [
    "MAX_SQL",
    "MAX_STATEMENTS",
    "SQLSlice",
    "compile_observed",
    "error_info",
    "redact_sql",
    "result_columns",
    "split_sql",
    "stage",
]


@dataclass(frozen=True)
class SQLSlice:
    sql: str
    start: int
    end: int
    line: int
    column: int

    def location(self) -> dict[str, int]:
        """返回对象记录的源码位置信息。"""
        return {
            "start": self.start,
            "end": self.end,
            "line": self.line,
            "column": self.column,
        }


def split_sql(sql: str, *, max_statements: int = MAX_STATEMENTS) -> list[SQLSlice]:
    """与 Lexer 一致地跳过注释、字符串和引用标识符中的分号。"""
    result: list[SQLSlice] = []
    start = index = 0
    quote = ""
    comment = ""
    meaningful = False
    while index < len(sql):
        char, pair = sql[index], sql[index : index + 2]
        if comment == "line":
            if char in "\r\n":
                comment = ""
        elif comment == "block":
            if pair == "*/":
                comment = ""
                index += 1
        elif quote:
            if char == "\\" and quote == "'":
                index += 1
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
        elif pair in {"--", "/*"}:
            comment = "line" if pair == "--" else "block"
            index += 1
        elif char in "'\"`":
            quote = char
            meaningful = True
        elif char == ";":
            if meaningful:
                result.append(_slice(sql, start, index + 1))
            start, meaningful = index + 1, False
        elif not char.isspace():
            meaningful = True
        index += 1
    if meaningful or comment == "block":
        result.append(_slice(sql, start, len(sql)))
    if len(result) > max_statements:
        raise YourSQLError(f"一次最多执行 {max_statements} 条 SQL", "BAD_REQUEST")
    return result


def _slice(sql: str, start: int, end: int) -> SQLSlice:
    """根据字符范围构造带位置的 SQL 片段。"""
    while start < end and sql[start].isspace():
        start += 1
    prefix = sql[:start].replace("\r\n", "\n").replace("\r", "\n")
    return SQLSlice(
        sql[start:end],
        start,
        end,
        prefix.count("\n") + 1,
        len(prefix.rsplit("\n", 1)[-1]) + 1,
    )


def redact_sql(sql: str) -> str:
    """历史移除注释和全部字面量；无法分词时不保留源码。"""
    try:
        literals = {TokenKind.STRING, TokenKind.INTEGER, TokenKind.FLOAT}
        return " ".join(
            "?" if token.kind in literals else token.lexeme
            for token in tokenize(sql)
            if token.kind != TokenKind.EOF
        )
    except YourSQLError:
        return "[无法安全分词，SQL 未记录]"


def _statement_anchor(source: SQLSlice) -> tuple[int, int]:
    """找到语句首个 Token；含前置注释时不把注释首字符当作语句起点。"""

    try:
        token = next(
            (item for item in tokenize(source.sql) if item.kind is not TokenKind.EOF),
            None,
        )
    except YourSQLError:
        return source.line, source.column
    if token is None:
        return source.line, source.column
    return (
        token.line + source.line - 1,
        token.column + (source.column - 1 if token.line == 1 else 0),
    )


def _edit_distance(left: str, right: str) -> int:
    """计算短关键字的编辑距离，用于给出保守的拼写提示。"""
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _keyword_suggestion(
    source: SQLSlice, error: YourSQLError
) -> tuple[str, int, int, str] | None:
    """从语法错误附近的完整标识符中寻找可能的 SQL 关键字拼写。"""
    if error.code != "PARSER_ERROR" or error.line is None or error.column is None:
        return None
    try:
        tokens = tokenize(source.sql)
    except YourSQLError:
        return None

    expected = str(error.details.get("expected", ""))
    expected_words = {
        item.upper() for item in expected.replace("'", "").split("/") if item.isalpha()
    }
    keywords = set(KEYWORDS)
    candidates: list[tuple[int, int, str, int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.kind is not TokenKind.IDENTIFIER:
            continue
        word = token.lexeme.upper()
        suggestion = min(keywords, key=lambda item: _edit_distance(word, item))
        distance = _edit_distance(word, suggestion)
        # 短词只接受一个编辑差异，避免把合法的表名误报成关键字。
        if distance > max(1, min(2, len(word) // 3)) or suggestion == word:
            continue
        expected_rank = 0 if suggestion in expected_words else 1
        # 两三个字符的列名（如 id）与短关键字距离很近，只有解析器明确期待该词时才提示。
        if len(word) < 4 and expected_rank != 0:
            continue
        if token.lexeme == str(error.details.get("found", "")) and expected_rank != 0:
            continue
        position_distance = abs(token.line - error.line) * 1000 + abs(
            token.column - error.column
        )
        # 只看错误点之前的词，覆盖 “FROM t wher id = 1” 这种别名歧义场景。
        if token.line > error.line or (
            token.line == error.line and token.column > error.column
        ):
            continue
        candidates.append(
            (
                expected_rank,
                position_distance,
                suggestion,
                token.line,
                token.column,
                token.lexeme,
            )
        )
        if index > 0 and expected_rank == 0 and position_distance == 0:
            break
    if not candidates:
        return None
    _rank, _distance, suggestion, line, column, typed_word = min(candidates)
    return suggestion, line, column, typed_word


def error_info(
    error: Exception, source: SQLSlice, *, sensitive: bool = False
) -> JsonObject:
    """返回带完整词素和轻量关键字建议的工作台诊断。"""
    if isinstance(error, YourSQLError):
        precise = error.line is not None
        if precise:
            line = (error.line or 1) + source.line - 1
            column = (error.column or 1) + (
                source.column - 1 if (error.line or 1) == 1 else 0
            )
        else:
            line, column = _statement_anchor(source)
        message = "敏感语句执行失败，详细值已隐藏" if sensitive else error.message
        diagnostic: JsonObject = {
            "code": error.code,
            "message": message,
            "line": line,
            "column": column,
            "position_accuracy": "token" if precise else "statement",
            "position_note": None
            if precise
            else "当前 Binder/执行器没有节点区间，定位到语句起点。",
        }
        if not sensitive:
            expected = error.details.get("expected")
            found = error.details.get("found")
            suggestion = _keyword_suggestion(source, error)
            if suggestion is not None:
                word, suggestion_line, suggestion_column, typed_word = suggestion
                expected_hint = f"，此处需要 {expected}" if expected else ""
                message = f"{message}（发现 `{typed_word}`{expected_hint}；你是想输入 {word} 吗？）"
                diagnostic.update(
                    {
                        "line": suggestion_line + source.line - 1,
                        "column": suggestion_column
                        + (source.column - 1 if suggestion_line == 1 else 0),
                        "suggestion": {"kind": "keyword", "replacement": word},
                    }
                )
            elif expected:
                shown_found = str(found) if found else "语句结尾"
                message = f"{message}（发现 `{shown_found}`，此处需要 {expected}）"
            diagnostic["message"] = message
        return diagnostic
    line, column = _statement_anchor(source)
    return {
        "code": "INTERNAL_ERROR",
        "message": "服务执行异常，请按请求 ID 检查服务日志",
        "line": line,
        "column": column,
        "position_accuracy": "statement",
    }


def stage(
    name: str,
    status: str,
    source: SQLSlice,
    *,
    data: object = None,
    duration_ms: float | None = None,
    reason: str | None = None,
    error: JsonObject | None = None,
) -> JsonObject:
    """记录或构造一个执行阶段。"""
    return {
        "name": name,
        "status": status,
        "data": data,
        "text": json.dumps(data, ensure_ascii=False, indent=2)
        if data is not None
        else reason or "",
        "duration_ms": duration_ms,
        "reason": reason,
        "error": error,
        "source": {**source.location(), "accuracy": "statement"},
    }


def compile_observed(
    database: Database, source: SQLSlice, stages: list[JsonObject]
) -> CompilationResult:
    """逐阶段调用原编译器组件，实际执行复用同一 CompilationResult。"""
    sensitive = "IDENTIFIED" in source.sql.upper()

    def capture(
        name: str, operation: Callable[[], T], serialize: Callable[[T], object]
    ) -> T:
        """采集当前阶段的执行信息。"""
        started = perf_counter()
        try:
            value = operation()
        except Exception as exc:
            stages.append(
                stage(
                    name,
                    "error",
                    source,
                    duration_ms=(perf_counter() - started) * 1000,
                    error=error_info(exc, source, sensitive=sensitive),
                )
            )
            raise
        elapsed = (perf_counter() - started) * 1000
        stages.append(
            stage(
                name,
                "success",
                source,
                data={"redacted": True, "reason": "凭据语句产物已隐藏"}
                if sensitive
                else serialize(value),
                duration_ms=elapsed,
            )
        )
        return value

    tokens = capture(
        "tokens",
        lambda: tuple(tokenize(source.sql)),
        lambda items: [item.as_dict() for item in items],
    )
    statement = capture(
        "ast", lambda: Parser(tokens).parse_one(), lambda node: node.to_dict()
    )
    # WHY：绑定器会读取 Catalog；先检查对象权限，避免用编译接口探测无权表结构。
    database._authorize_statement(statement, database._action_for(statement))
    bound = capture(
        "binding",
        lambda: Binder(database.catalog).bind(statement),
        lambda node: node.to_dict(),
    )
    plan = capture(
        "logical_plan",
        lambda: plan_from_statement(statement),
        lambda node: node.to_dict(),
    )
    cache_sql = source.sql if isinstance(statement, (Select, Explain)) else None
    optimized_plan = capture(
        "optimized_plan",
        lambda: database.optimize_plan(plan, sql=cache_sql),
        lambda node: node.to_dict(),
    )
    # 当前 Database 仍通过内部 evaluator 消费物理计划，Volcano 算子树尚未接管主路径。
    physical_started = perf_counter()
    physical_data = (
        {"redacted": True, "reason": "凭据语句产物已隐藏"}
        if sensitive
        else optimized_plan.to_dict()
    )
    stages.append(
        stage(
            "physical_plan",
            "partial",
            source,
            data=physical_data,
            duration_ms=(perf_counter() - physical_started) * 1000,
            reason="物理计划已独立建模；Volcano 执行算子尚未接管 Database 主执行路径。",
        )
    )
    return CompilationResult(tokens, statement, bound, plan, optimized_plan)


def result_columns(
    database: Database, statement: Statement, result: ExecutionResult
) -> list[JsonObject]:
    """直列引用来自 Schema；表达式列使用运行时类型，空列明确标为 UNKNOWN。"""
    declared: list[str | None] = []
    if isinstance(statement, Select):
        references = ([statement.from_table] if statement.from_table else []) + [
            join.table for join in statement.joins
        ]
        for item in statement.items:
            expression = item.expression
            if isinstance(expression, Star):
                for ref in references:
                    if not expression.table or expression.table.lower() in {
                        ref.name.lower(),
                        (ref.alias or "").lower(),
                    }:
                        declared.extend(
                            column.data_type.value
                            for column in database.catalog.get_relation(ref.name).schema
                        )
            elif isinstance(expression, ColumnRef):
                matches = [
                    column.data_type.value
                    for ref in references
                    if not expression.table
                    or expression.table.lower()
                    in {ref.name.lower(), (ref.alias or "").lower()}
                    for column in database.catalog.get_relation(ref.name).schema
                    if column.name.lower() == expression.name.lower()
                ]
                declared.append(matches[0] if len(matches) == 1 else None)
            else:
                declared.append(None)
    elif isinstance(statement, Explain):
        declared = ["VARCHAR"]
    elif isinstance(statement, Show):
        declared = {
            "COLUMNS": ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", None],
            "INDEX": ["VARCHAR", "VARCHAR", "BOOLEAN", "VARCHAR", "VARCHAR"],
        }.get(statement.target, ["VARCHAR"] * len(result.columns))
    columns = []
    for index, name in enumerate(result.columns):
        data_type = declared[index] if index < len(declared) else None
        type_source = "schema" if data_type else "runtime"
        if not data_type:
            value = next(
                (row[index] for row in result.rows if row[index] is not None), None
            )
            data_type = (
                Value.infer(value).data_type.value if value is not None else "UNKNOWN"
            )
        columns.append({"name": name, "type": data_type, "type_source": type_source})
    return columns

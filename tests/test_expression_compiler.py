"""表达式编译闭包与逐行解释器的等价性回归。

WHY：`_compile_expr` 是 SELECT 热路径的替代实现，必须与 `_eval_expr` 在三值逻辑、
NULL 传播和错误信息（含行列位置）上完全一致，这里逐例对照两者结果或异常。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.common import YourSQLError
from yoursql.engine.database import Database
from yoursql.sql.ast import BinaryOp, ColumnRef, Literal
from yoursql.sql.lexer import tokenize
from yoursql.sql.parser import Parser


def _parse_expression(source: str):
    """借用 SELECT 语法解析一个表达式，保持与真实执行相同的 AST 构造路径。"""

    return Parser(tokenize(f"SELECT {source} FROM t;")).parse_script()[0].items[0].expression


CONTEXTS: list[dict[str, object]] = [
    {"id": 1, "score": 3.5, "name": "Alice", "active": True, "ratio": 0.07, "note": None, "t.id": 1, "t.name": "Alice"},
    {"id": 2, "score": None, "name": "Bob", "active": False, "ratio": 0.05, "note": "x", "t.id": 2, "t.name": "Bob"},
    {"id": 3, "score": 10, "name": None, "active": None, "ratio": 0.02, "note": "ab", "t.id": 3, "t.name": None},
]

EXPRESSIONS = [
    "1 + 2 * 3",
    "id",
    "t.id",
    "score",
    "note",
    "-score",
    "+id",
    "NOT active",
    "id > 1",
    "score >= 3.5",
    "name = 'Alice'",
    "note <> 'x'",
    "active AND id = 1",
    "active OR id = 3",
    "score AND active",
    "score OR active",
    "note IS NULL",
    "note IS NOT NULL",
    "id BETWEEN 1 AND 2",
    "id NOT BETWEEN 1 AND 2",
    "score BETWEEN 3 AND 4",
    "name LIKE 'A%'",
    "name NOT LIKE '%o%'",
    "id / (id - 1)",
    "id / (id - 3)",
    "score + 1",
    "name || note",
    "id % 2",
    "ratio BETWEEN 0.06 - 0.01 AND 0.06 + 0.01",
]


def _outcome(evaluate) -> tuple[str, object]:
    """把一次求值压缩成可比较的结果：正常值或（异常类型, 消息, 行列）。"""

    try:
        return "value", evaluate()
    except YourSQLError as error:
        return "error", (type(error).__name__, str(error), getattr(error, "line", None), getattr(error, "column", None))


@pytest.mark.parametrize("source", EXPRESSIONS)
def test_compiled_expression_matches_interpreter(tmp_path: Path, source: str) -> None:
    with Database(tmp_path / "expr.db") as database:
        database.execute("CREATE TABLE t(id INT, score FLOAT, name VARCHAR, active BOOLEAN, ratio FLOAT, note VARCHAR);")
        expression = _parse_expression(source)
        compiled = database._compile_expr(expression)
        for context in CONTEXTS:
            assert _outcome(lambda: compiled(context)) == _outcome(lambda: database._eval_expr(expression, context)), (
                f"{source} 在 {context} 下编译/解释结果不一致"
            )


def test_compiled_expression_reports_unknown_column_like_interpreter(tmp_path: Path) -> None:
    with Database(tmp_path / "missing.db") as database:
        database.execute("CREATE TABLE t(id INT);")
        expression = _parse_expression("missing_column + 1")
        compiled = database._compile_expr(expression)
        assert _outcome(lambda: compiled({"id": 1})) == _outcome(lambda: database._eval_expr(expression, {"id": 1}))


def test_statement_folding_keeps_constant_and_column_semantics(tmp_path: Path) -> None:
    """常量折叠只预求值常量子树，列相关部分仍由运行时决定。"""

    with Database(tmp_path / "fold.db") as database:
        database.execute("CREATE TABLE t(id INT, score FLOAT);")
        database.execute("INSERT INTO t VALUES (1, 0.07), (2, 0.06), (3, 0.05);")
        rows = database.execute("SELECT id FROM t WHERE score BETWEEN 0.06 - 0.01 AND 0.06 + 0.01 ORDER BY id;").rows
        assert rows == [(2,), (3,)]  # double 语义下 0.07 被排除，与 SQLite 一致
        statement = Parser(tokenize("SELECT id + 1 FROM t WHERE score > 0.01 + 0.04;")).parse_script()[0]
        folded = database._fold_statement(statement)
        # 常量右子树被折叠为 Literal，列引用不变
        item = folded.items[0].expression
        assert isinstance(item, BinaryOp) and isinstance(item.left, ColumnRef) and isinstance(item.right, Literal)
        assert folded.where is not None and isinstance(folded.where.right, Literal)
        assert folded.where.right.value == pytest.approx(0.05)
        assert isinstance(folded.where.left, ColumnRef)
        # 原语句未被就地修改（仍是折叠前的结构）
        assert isinstance(statement.items[0].expression.right, Literal)
        assert isinstance(statement.where.right, BinaryOp)  # type: ignore[union-attr]

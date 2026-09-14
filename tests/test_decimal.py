"""DECIMAL 定点化的回归：字面量精度、落盘往返、索引范围扫描与聚合。

WHY：TPC-H Q6 的 ``l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01`` 在浮点实现下
会漏掉 ``l_discount = 0.07`` 的行，与官方答案（DuckDB 值 734493.7281）不一致。
这组用例把这个正确性缺口钉死；同时确认 FLOAT 列的 IEEE-754 语义没有被改变。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from yoursql.common import DataType, Value, compare_values, to_decimal
from yoursql.engine.runtime.database import Database
from yoursql.sql.lexer import Lexer, TokenKind


def test_decimal_literal_keeps_exact_spelling() -> None:
    """词法阶段的定点字面量必须与书写完全一致。"""

    tokens = Lexer("SELECT 0.07, 0.06 - 0.01, 1.5e2;").tokenize()
    floats = [token for token in tokens if token.kind is TokenKind.FLOAT]
    assert floats[0].literal == Decimal("0.07")
    assert str(floats[0].literal) == "0.07"
    # Decimal(0.07) 会带上二进制噪声；to_decimal 对 float 也必须走 repr
    assert to_decimal(0.07) == Decimal("0.07")
    assert to_decimal(7) == Decimal(7)


def test_decimal_type_coercion() -> None:
    """INT/FLOAT 可以进 DECIMAL，DECIMAL 也能按需退回。"""

    assert Value.infer(Decimal("1.25")).data_type is DataType.DECIMAL
    assert Value(DataType.INT, 3).coerce(DataType.DECIMAL).unwrap() == Decimal(3)
    assert Value(DataType.FLOAT, 0.5).coerce(DataType.DECIMAL).unwrap() == Decimal(
        "0.5"
    )
    assert Value(DataType.DECIMAL, Decimal("2.50")).coerce(DataType.FLOAT).unwrap() == 2.5
    assert Value(DataType.DECIMAL, Decimal("2.00")).coerce(DataType.INT).unwrap() == 2
    assert Value(DataType.VARCHAR, "1.25").coerce(DataType.DECIMAL).unwrap() == Decimal(
        "1.25"
    )


def test_decimal_and_float_semantics_stay_separate() -> None:
    """DECIMAL 精确比较；FLOAT 仍是 IEEE-754。"""

    assert compare_values(Decimal("0.07"), Decimal("0.07"), "=") is True
    assert compare_values(Decimal("0.06") - Decimal("0.01"), Decimal("0.05"), "=") is True
    # 0.07 的 double 值严格大于精确的 0.07，这正是 Q6 漏行的根因
    assert compare_values(0.07, Decimal("0.07"), ">") is True
    assert compare_values(0.07, Decimal("0.07"), "<=") is False


def test_decimal_round_trip_through_storage(tmp_path: Path) -> None:
    """DECIMAL 列落盘后回读必须逐位一致（不能被 JSON 折成 float）。"""

    with Database(tmp_path / "decimal.db") as database:
        database.execute("CREATE TABLE price(id INT, amount DECIMAL(15,2));")
        database.execute("INSERT INTO price VALUES (1, 0.07), (2, 0.10), (3, 1234.56);")
        rows = database.execute("SELECT amount FROM price ORDER BY id;").rows
        assert [row[0] for row in rows] == [
            Decimal("0.07"),
            Decimal("0.10"),
            Decimal("1234.56"),
        ]

    # 重新打开：走的是页里的 JSON 记录，确认 DECIMAL 标记能还原
    with Database(tmp_path / "decimal.db") as database:
        rows = database.execute("SELECT amount FROM price ORDER BY id;").rows
        assert [row[0] for row in rows] == [
            Decimal("0.07"),
            Decimal("0.10"),
            Decimal("1234.56"),
        ]


def test_decimal_boundary_predicate_matches_official_answer(tmp_path: Path) -> None:
    """Q6 场景：0.06 ± 0.01 必须命中 0.05 与 0.07 两边端点。"""

    with Database(tmp_path / "q6.db") as database:
        database.execute("CREATE TABLE lineitem(l_discount DECIMAL(15,2));")
        database.execute(
            "INSERT INTO lineitem VALUES (0.04), (0.05), (0.06), (0.07), (0.08);"
        )
        rows = database.execute(
            "SELECT l_discount FROM lineitem "
            "WHERE l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01 "
            "ORDER BY l_discount;"
        ).rows
        assert [row[0] for row in rows] == [
            Decimal("0.05"),
            Decimal("0.06"),
            Decimal("0.07"),
        ]


def test_decimal_sum_keeps_scale(tmp_path: Path) -> None:
    """SUM 定点数保持标度，不做二进制近似（对应 734493.7281 这类答案）。"""

    with Database(tmp_path / "sum.db") as database:
        database.execute("CREATE TABLE t(price DECIMAL(15,2), discount DECIMAL(15,2));")
        database.insert_rows(
            "t",
            (
                (Decimal("1000.00"), Decimal("0.07")),
                (Decimal("2000.50"), Decimal("0.05")),
                (Decimal("0.28"), Decimal("0.04")),
            ),
        )
        row = database.execute("SELECT SUM(price * discount) FROM t;").rows[0]
        # 70.00 + 100.025 + 0.0112：标度按位保留，没有二进制近似
        assert row[0] == Decimal("170.0362")
        assert str(row[0]) == "170.0362"


def test_decimal_range_index_scan(tmp_path: Path) -> None:
    """索引键里的 DECIMAL 必须参与范围扫描并保持顺序正确。"""

    with Database(tmp_path / "index.db") as database:
        database.execute("CREATE TABLE t(id INT, amount DECIMAL(15,2));")
        database.execute(
            "CREATE INDEX idx_amount ON t(amount);"
        )
        database.insert_rows(
            "t",
            [(index, Decimal(index) / Decimal(100)) for index in range(200)],
        )
        result = database.execute(
            "SELECT COUNT(*) FROM t WHERE amount >= 1.50 AND amount < 1.60;"
        )
        assert result.rows == [(10,)]
        # WHY：被引用列只有 `amount`，索引 `idx_amount(amount)` 已完全覆盖，因此访问路径
        # 是覆盖索引直读而非"索引定位 + 回表"。COUNT(*) 早先会把聚合里的 `*` 误判成需要
        # 全部列，从而让该路径被拒——这里连同取值一起固定住，避免再退化。
        assert result.stats["operator"] == "IndexOnlyScan", result.stats

        rows = database.execute(
            "SELECT amount FROM t WHERE amount >= 1.50 AND amount < 1.60 ORDER BY amount;"
        ).rows
        # 下界闭、上界开：1.50 在内，1.60 不在内
        assert rows == [(Decimal(index) / Decimal(100),) for index in range(150, 160)]


def test_decimal_json_output_is_number(tmp_path: Path) -> None:
    """展示边界把 Decimal 落成 JSON 数字，内部精度不受影响。"""

    with Database(tmp_path / "json.db") as database:
        database.execute("CREATE TABLE t(amount DECIMAL(15,2));")
        database.execute("INSERT INTO t VALUES (734493.7281);")
        result = database.execute("SELECT amount FROM t;")
        assert result.rows[0][0] == Decimal("734493.7281")
        payload = result.as_dict()
        assert payload["rows"][0][0] == 734493.7281
        assert isinstance(payload["rows"][0][0], float)

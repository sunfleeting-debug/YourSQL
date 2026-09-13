"""连接执行策略（哈希连接 / 索引嵌套循环 / 嵌套循环）的正确性与选择回归。

WHY：连接是多表查询的唯一瓶颈；换策略后必须逐项固定住 SQL 语义（NULL 键、一对多、
外连接、非等值回退），否则会静默返回错误行。
"""

from __future__ import annotations

from pathlib import Path

from yoursql.engine.database import Database

LEFT_ROWS = [(index, f"c{index}") for index in range(40)]
RIGHT_ROWS = [
    # customer_id 与左表部分重合；包含一对多（客户 0 有 3 单）与 NULL 键（永不匹配）
    (100, 0), (101, 0), (102, 0), (103, 1), (104, 2), (105, None), (106, 99),
]


def _seed(database: Database) -> None:
    database.execute("CREATE TABLE customers(id INT, name VARCHAR);")
    database.insert_rows("customers", LEFT_ROWS)
    database.execute("CREATE TABLE orders(oid INT, customer_id INT);")
    database.insert_rows("orders", RIGHT_ROWS)


def test_hash_join_matches_nested_loop_results(tmp_path: Path) -> None:
    with Database(tmp_path / "join-hash.db") as db:
        _seed(db)
        sql = "SELECT c.name, o.oid FROM customers AS c JOIN orders AS o ON o.customer_id = c.id;"
        result = db.execute(sql)
        assert result.stats["joins"] == ["HashJoin"], result.stats
        pairs = sorted((row[0], row[1]) for row in result.rows)
        assert pairs == sorted(
            [(f"c{customer}", oid) for oid, customer in RIGHT_ROWS if customer is not None and customer < 40]
        )
        # NULL 键永不匹配：订单 oid=105 不应出现
        assert all(row[1] != 105 for row in result.rows)


def test_hash_join_preserves_outer_join_semantics(tmp_path: Path) -> None:
    with Database(tmp_path / "join-outer.db") as db:
        _seed(db)
        left = db.execute(
            "SELECT c.id, o.oid FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.id;"
        )
        matched = {row[0] for row in left.rows if row[1] is not None}
        assert matched == {0, 1, 2}
        # 客户 0 有 3 单（一对多），加上客户 1/2 各 1 单 → 5 行匹配
        assert len([row for row in left.rows if row[1] is not None]) == 5
        # 其余左行必须以 NULL 补右侧
        unmatched = [row[0] for row in left.rows if row[1] is None]
        assert sorted(unmatched) == sorted(row[0] for row in LEFT_ROWS if row[0] not in {0, 1, 2})

        right = db.execute(
            "SELECT c.id, o.oid FROM customers AS c RIGHT JOIN orders AS o ON o.customer_id = c.id;"
        )
        # 匹配的右行：100/101/102（客户 0）、103（客户 1）、104（客户 2）
        assert sorted(row[1] for row in right.rows if row[0] is not None) == [100, 101, 102, 103, 104]
        # 未匹配的右行：105（NULL 键）与 106（客户 99）
        assert sorted(row[1] for row in right.rows if row[0] is None) == [105, 106]


def test_non_equality_join_falls_back_to_nested_loop(tmp_path: Path) -> None:
    with Database(tmp_path / "join-range.db") as db:
        _seed(db)
        result = db.execute(
            "SELECT c.id, o.oid FROM customers AS c JOIN orders AS o ON o.customer_id < c.id;"
        )
        assert result.stats["joins"] == ["NestedLoop"], result.stats
        expected = {
            (target, oid)
            for oid, customer in RIGHT_ROWS
            if customer is not None
            for target in range(40)
            if customer < target
        }
        assert sorted((row[0], row[1]) for row in result.rows) == sorted(expected)


def test_composite_join_key_uses_hash_join(tmp_path: Path) -> None:
    with Database(tmp_path / "join-composite.db") as db:
        db.execute("CREATE TABLE a(k1 INT, k2 VARCHAR, label VARCHAR);")
        db.insert_rows("a", [(1, "x", "a1"), (1, "y", "a2"), (2, "x", "a3")])
        db.execute("CREATE TABLE b(k1 INT, k2 VARCHAR, value INT);")
        db.insert_rows("b", [(1, "x", 10), (1, "z", 20), (2, "x", 30)])
        result = db.execute(
            "SELECT a.label, b.value FROM a JOIN b ON b.k1 = a.k1 AND b.k2 = a.k2 ORDER BY b.value;"
        )
        assert result.stats["joins"] == ["HashJoin"], result.stats
        assert result.rows == [("a1", 10), ("a3", 30)]


def test_index_nested_loop_is_chosen_when_hash_build_does_not_fit(tmp_path: Path, monkeypatch) -> None:
    """建侧超内存预算且外层很小时退到索引连接，并验证结果与哈希连接一致。"""

    with Database(tmp_path / "join-index.db") as db:
        db.execute("CREATE TABLE small(id INT, name VARCHAR);")
        db.insert_rows("small", [(1, "one"), (2, "two")])
        db.execute("CREATE TABLE big(bid INT, small_id INT, payload VARCHAR);")
        db.insert_rows("big", [(index, index % 2 + 1, f"p{index}") for index in range(1000)])
        db.execute("CREATE INDEX idx_big_small ON big (small_id);")
        sql = "SELECT s.name, b.bid FROM small AS s JOIN big AS b ON b.small_id = s.id;"
        expected = sorted((row[0], row[1]) for row in db.execute(sql).rows)
        assert len(expected) == 1000

        # 把内存预算压到 1 行 → 哈希建侧（1,000 行）不可行；外层仅 2 行 → 索引连接应胜出
        monkeypatch.setattr("yoursql.engine.database._JOIN_HASH_MEMORY_BUDGET", 464)
        result = db.execute(sql)
        assert result.stats["joins"] == ["IndexNestedLoop"], result.stats
        assert sorted((row[0], row[1]) for row in result.rows) == expected

        # LEFT JOIN 同样走索引连接，且未匹配左行补 NULL
        monkeypatch.setattr("yoursql.engine.database._JOIN_HASH_MEMORY_BUDGET", 464)
        left = db.execute("SELECT s.id, b.bid FROM small AS s LEFT JOIN big AS b ON b.small_id = s.id;")
        assert left.stats["joins"] == ["IndexNestedLoop"], left.stats
        assert len(left.rows) == 1000


def test_join_predicate_outside_key_is_applied(tmp_path: Path) -> None:
    """连接键之外的 ON 条件（残余谓词）必须仍然生效。"""

    with Database(tmp_path / "join-residual.db") as db:
        _seed(db)
        result = db.execute(
            "SELECT c.name, o.oid FROM customers AS c JOIN orders AS o "
            "ON o.customer_id = c.id AND o.oid > 101;"
        )
        assert result.stats["joins"] == ["HashJoin"], result.stats
        assert sorted(row[1] for row in result.rows) == [102, 103, 104]


def test_comma_join_infers_hash_join_keys_from_where(tmp_path: Path) -> None:
    """逗号连接（条件写在 WHERE）必须推断出连接键并走哈希连接，而不是笛卡尔积。"""

    with Database(tmp_path / "join-comma.db") as db:
        _seed(db)
        comma = db.execute("SELECT c.name, o.oid FROM customers AS c, orders AS o WHERE c.id = o.customer_id;")
        assert comma.stats["joins"] == ["HashJoin"], comma.stats
        explicit = db.execute("SELECT c.name, o.oid FROM customers AS c JOIN orders AS o ON c.id = o.customer_id;")
        assert sorted(comma.rows) == sorted(explicit.rows)
        assert len(comma.rows) == sum(1 for _oid, customer in RIGHT_ROWS if customer is not None and customer < 40)


def test_comma_join_conditions_land_on_matching_level(tmp_path: Path) -> None:
    """三层逗号连接：每层只拿“已就绪”的条件，不能引用尚未连接的表。"""

    with Database(tmp_path / "join-comma3.db") as db:
        db.execute("CREATE TABLE t1(a INT, name VARCHAR);")
        db.insert_rows("t1", [(1, "x"), (2, "y")])
        db.execute("CREATE TABLE t2(a INT, b INT);")
        db.insert_rows("t2", [(1, 10), (2, 20), (3, 30)])
        db.execute("CREATE TABLE t3(b INT, tag VARCHAR);")
        db.insert_rows("t3", [(10, "p"), (20, "q"), (99, "z")])
        result = db.execute("SELECT name, tag FROM t1, t2, t3 WHERE t1.a = t2.a AND t2.b = t3.b;")
        assert result.stats["joins"] == ["HashJoin", "HashJoin"], result.stats
        assert sorted(result.rows) == [("x", "p"), ("y", "q")]


def test_or_branch_common_key_is_used_for_hash_join(tmp_path: Path) -> None:
    """连接键写在每个 OR 分支里时取共同等式建哈希表，OR 整体仍由残余谓词过滤。"""

    with Database(tmp_path / "join-or.db") as db:
        _seed(db)
        result = db.execute(
            "SELECT c.name, o.oid FROM customers AS c, orders AS o "
            "WHERE (c.id = o.customer_id AND o.oid = 100) OR (c.id = o.customer_id AND o.oid = 103);"
        )
        assert result.stats["joins"] == ["HashJoin"], result.stats
        assert sorted(result.rows) == [("c0", 100), ("c1", 103)]


def test_uncorrelated_in_subquery_is_evaluated_once(tmp_path: Path, monkeypatch) -> None:
    """不相关 IN 子查询只执行一次（原来逐行执行）。"""

    with Database(tmp_path / "join-in.db") as db:
        db.execute("CREATE TABLE fact(id INT, label VARCHAR);")
        db.insert_rows("fact", [(1, "a"), (2, "b"), (3, "c")])
        db.execute("CREATE TABLE allow(id INT);")
        db.insert_rows("allow", [(2,), (3,)])
        calls = {"count": 0}
        original = Database._execute_select

        def counting(self, statement, *args, **kwargs):
            calls["count"] += 1
            return original(self, statement, *args, **kwargs)

        monkeypatch.setattr(Database, "_execute_select", counting)
        result = db.execute("SELECT id FROM fact WHERE id IN (SELECT id FROM allow);")
        assert sorted(row[0] for row in result.rows) == [2, 3]
        # 主查询 1 次 + 子查询 1 次；若逐行执行会是 1 + 3 次
        assert calls["count"] == 2, calls


def test_uncorrelated_not_in_subquery_keeps_null_semantics(tmp_path: Path) -> None:
    """子查询结果含 NULL 时 IN/NOT IN 必须保持三值逻辑（集合查找不得改变语义）。"""

    with Database(tmp_path / "join-in-null.db") as db:
        db.execute("CREATE TABLE fact(id INT);")
        db.insert_rows("fact", [(1,), (2,)])
        db.execute("CREATE TABLE allow(id INT);")
        db.insert_rows("allow", [(2,), (None,)])
        assert db.execute("SELECT id FROM fact WHERE id IN (SELECT id FROM allow);").rows == [(2,)]
        # 1 与 NULL 比较得到 NULL（非 TRUE），所以 NOT IN 不返回任何行
        assert db.execute("SELECT id FROM fact WHERE id NOT IN (SELECT id FROM allow);").rows == []

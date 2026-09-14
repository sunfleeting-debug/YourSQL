"""索引候选集缓存的行为回归：命中、写入失效、结果随数据变化。

WHY：候选集缓存是纯性能优化，但一旦失效逻辑漏掉写路径就会返回陈旧候选集，
导致查询少扫/多扫行，因此这里固定住“写入即失效”的行为。
"""

from __future__ import annotations

from pathlib import Path

from yoursql.engine.runtime.database import Database
from yoursql.sql.lexer import tokenize
from yoursql.sql.parser import Parser


def _where(sql: str):
    return Parser(tokenize(sql)).parse_script()[0].where


def test_candidate_cache_hits_then_invalidates_on_write(tmp_path: Path) -> None:
    with Database(tmp_path / "cache.db") as database:
        database.execute("CREATE TABLE t(id INT, status VARCHAR);")
        database.insert_rows("t", [(index, "paid" if index % 2 == 0 else "rare") for index in range(200)])
        database.execute("CREATE INDEX idx_status ON t (status);")
        table = database.catalog.get_table("t")
        where = _where("SELECT id FROM t WHERE status = 'paid';")

        first = database._candidate_row_ids(table, Parser(tokenize("SELECT id FROM t;")).parse_script()[0].from_table, where)
        assert first is not None and len(first) == 100
        cache_after_first = dict(database._candidate_cache)
        assert cache_after_first, "首次查询应写入候选集缓存"

        second = database._candidate_row_ids(table, Parser(tokenize("SELECT id FROM t;")).parse_script()[0].from_table, where)
        assert second == first
        assert database._candidate_cache == cache_after_first, "重复查询应命中缓存而不是重算"

        database.execute("INSERT INTO t VALUES (999, 'paid');")
        assert database._candidate_cache == {}, "写入后候选集缓存必须整体失效"

        third = database._candidate_row_ids(table, Parser(tokenize("SELECT id FROM t;")).parse_script()[0].from_table, where)
        assert third is not None and len(third) == 101, "重算后的候选集应反映新写入的行"
        assert database.execute("SELECT id FROM t WHERE status = 'paid';").rows.__len__() == 101


def test_candidate_cache_is_per_constraint(tmp_path: Path) -> None:
    """不同约束签名不能互相命中。"""

    with Database(tmp_path / "cache2.db") as database:
        database.execute("CREATE TABLE t(id INT, status VARCHAR);")
        database.insert_rows("t", [(index, "paid" if index < 10 else "rare") for index in range(50)])
        database.execute("CREATE INDEX idx_status ON t (status);")
        table = database.catalog.get_table("t")
        from_table = Parser(tokenize("SELECT id FROM t;")).parse_script()[0].from_table

        paid = database._candidate_row_ids(table, from_table, _where("SELECT id FROM t WHERE status = 'paid';"))
        rare = database._candidate_row_ids(table, from_table, _where("SELECT id FROM t WHERE status = 'rare';"))
        assert paid is not None and rare is not None
        assert len(paid) == 10 and len(rare) == 40
        assert set(paid).isdisjoint(rare)

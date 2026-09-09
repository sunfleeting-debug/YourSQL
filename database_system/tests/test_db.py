"""阶段 6-7 测试：执行引擎 + 存储引擎 + 系统集成（端到端）。

运行：python -m unittest database_system.tests.test_db -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.engine.database import Database
from database_system.engine.storage_engine import TableHeap


class DatabaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minisql-db-")
        self.path = os.path.join(self.tmp, "mini.db")
        self.db = Database(self.path, pool_size=8)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def exec_one(self, sql: str):
        results = self.db.execute(sql)
        self.assertEqual(len(results), 1, f"期望 1 条语句，实际 {len(results)}")
        return results[0]

    def rows_of(self, sql: str):
        result = self.exec_one(sql)
        self.assertIsNone(result.error, f"意外错误: {result.error}")
        return result.rows


class CrudTest(DatabaseTestBase):
    def setUp(self):
        super().setUp()
        self.db.execute("CREATE TABLE student(id INT, name VARCHAR(20), age INT);")
        self.db.execute(
            "INSERT INTO student(id,name,age) VALUES "
            "(1,'Alice',20),(2,'Bob',17),(3,'Cindy',21),(4,'Dan',20);"
        )

    def test_create_and_insert(self):
        self.assertEqual(self.db.tables(), ["student"])
        self.assertIn("age INT", self.db.schema("student"))

    def test_select_all(self):
        rows = self.rows_of("SELECT * FROM student;")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], [1, "Alice", 20])

    def test_select_projection(self):
        result = self.exec_one("SELECT name, age FROM student WHERE age > 18;")
        self.assertEqual(result.columns, ["name", "age"])
        self.assertEqual(result.rows, [["Alice", 20], ["Cindy", 21], ["Dan", 20]])

    def test_where_and_or_not(self):
        self.assertEqual(
            self.rows_of("SELECT id FROM student WHERE age = 20 AND id != 1;"), [[4]]
        )
        self.assertEqual(
            self.rows_of("SELECT id FROM student WHERE age = 17 OR age = 21;"), [[2], [3]]
        )
        self.assertEqual(
            self.rows_of("SELECT id FROM student WHERE NOT age = 20;"), [[2], [3]]
        )

    def test_arithmetic_and_parenthesis(self):
        self.assertEqual(
            self.rows_of("SELECT id FROM student WHERE age > 10 + 8;"), [[1], [3], [4]]
        )
        self.assertEqual(
            self.rows_of("SELECT id FROM student WHERE (age = 17 OR age = 21) AND id > 2;"),
            [[3]],
        )

    def test_delete(self):
        result = self.exec_one("DELETE FROM student WHERE id = 2;")
        self.assertIn("1 row(s) deleted", result.message)
        self.assertEqual(self.rows_of("SELECT * FROM student;"), [
            [1, "Alice", 20], [3, "Cindy", 21], [4, "Dan", 20]
        ])

    def test_delete_all(self):
        self.exec_one("DELETE FROM student;")
        self.assertEqual(self.rows_of("SELECT * FROM student;"), [])

    def test_update(self):
        result = self.exec_one("UPDATE student SET age = age + 1 WHERE name = 'Bob';")
        self.assertIn("1 row(s) updated", result.message)
        self.assertEqual(self.rows_of("SELECT age FROM student WHERE id = 2;"), [[18]])

    def test_distinct_order_limit(self):
        self.assertEqual(
            self.rows_of("SELECT DISTINCT age FROM student ORDER BY age;"),
            [[17], [20], [21]],
        )
        self.assertEqual(
            self.rows_of("SELECT name FROM student ORDER BY age DESC LIMIT 2;"),
            [["Cindy"], ["Alice"]],
        )

    def test_order_by_unprojected_column(self):
        # ORDER BY 可以引用未被投影的列
        self.assertEqual(
            self.rows_of("SELECT name FROM student ORDER BY id DESC;"),
            [["Dan"], ["Cindy"], ["Bob"], ["Alice"]],
        )

    def test_alias(self):
        result = self.exec_one("SELECT s.name FROM student AS s WHERE s.age > 20;")
        self.assertEqual(result.columns, ["name"])
        self.assertEqual(result.rows, [["Cindy"]])

    def test_null(self):
        self.db.execute("CREATE TABLE t(id INT, v VARCHAR(10));")
        self.db.execute("INSERT INTO t(id, v) VALUES (1, NULL);")
        self.assertEqual(self.rows_of("SELECT id FROM t WHERE v IS NULL;"), [[1]])
        self.assertEqual(self.rows_of("SELECT id FROM t WHERE v IS NOT NULL;"), [])

    def test_explain_does_not_execute(self):
        result = self.exec_one("EXPLAIN DELETE FROM student;")
        self.assertIsNone(result.error)
        self.assertIn("SeqScan", result.plan_after)
        # 没有真正执行删除
        self.assertEqual(len(self.rows_of("SELECT * FROM student;")), 4)

    def test_drop_table(self):
        self.exec_one("DROP TABLE student;")
        self.assertEqual(self.db.tables(), [])


class PersistenceTest(DatabaseTestBase):
    def test_data_survives_restart(self):
        self.db.execute("CREATE TABLE t(id INT, name VARCHAR(20));")
        self.db.execute("INSERT INTO t VALUES (1,'a'),(2,'b');")
        self.db.close()

        self.db = Database(self.path, pool_size=8)
        self.assertEqual(self.db.tables(), ["t"])
        self.assertEqual(self.rows_of("SELECT * FROM t;"), [[1, "a"], [2, "b"]])
        self.assertEqual(self.db.schema("t"), "t(id INT, name VARCHAR(20))")

    def test_catalog_survives_restart(self):
        self.db.execute("CREATE TABLE a(id INT);")
        self.db.execute("CREATE TABLE b(name VARCHAR(5), flag BOOL);")
        self.db.close()
        self.db = Database(self.path, pool_size=8)
        self.assertEqual(sorted(self.db.tables()), ["a", "b"])
        self.assertIn("flag BOOL", self.db.schema("b"))


class ScaleTest(DatabaseTestBase):
    def test_multi_page_and_small_pool(self):
        db = Database(self.path, pool_size=3, policy="LRU")
        db.execute("CREATE TABLE big(id INT, name VARCHAR(50), age INT);")
        values = ",".join(f"({i},'name{i}',{i % 80})" for i in range(500))
        db.execute(f"INSERT INTO big(id,name,age) VALUES {values};")
        rows = db.execute("SELECT * FROM big;")[0].rows
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[499], [499, "name499", 19])
        table = db.catalog.get_table("big")
        self.assertGreater(TableHeap(db.buffer, table.root_page_id).page_count(), 1)
        self.assertEqual(len(db.execute("SELECT id FROM big WHERE age > 70;")[0].rows), 54)
        self.assertEqual(db.buffer.pinned_pages(), [])  # 没有 pin 泄漏
        self.assertGreater(db.buffer_stats()["hit_rate"], 0)
        db.close()

    def test_free_list_reuse_after_drop(self):
        self.db.execute("CREATE TABLE t(id INT);")
        self.db.execute("INSERT INTO t VALUES (1);")
        before = self.db.disk.page_count
        self.db.execute("DROP TABLE t;")
        self.db.execute("CREATE TABLE u(id INT);")
        # 释放的页被复用，文件不应无限增长
        self.assertLessEqual(self.db.disk.page_count, before + 1)


class ErrorStageTest(DatabaseTestBase):
    def setUp(self):
        super().setUp()
        self.db.execute("CREATE TABLE student(id INT, name VARCHAR(20), age INT);")

    def test_lexical_error(self):
        result = self.exec_one("SELECT @ FROM student;")
        self.assertEqual(result.stage, "Lexer")
        self.assertIsNotNone(result.error)
        self.assertIsNotNone(result.error.line)

    def test_syntax_error(self):
        result = self.exec_one("SELECT id FROM student WHERE;")
        self.assertEqual(result.stage, "Parser")

    def test_semantic_error(self):
        result = self.exec_one("SELECT nope FROM student;")
        self.assertEqual(result.stage, "Semantic")

    def test_error_has_position(self):
        result = self.exec_one("SELECT * FROM nosuchtable;")
        self.assertIsNotNone(result.error.line)
        self.assertIsNotNone(result.error.column)
        self.assertIn("SemanticError", str(result.error))

    def test_no_crash_on_garbage(self):
        # 各种非法输入都不得抛异常（只要求返回错误结果）
        for sql in ("", "   ", ";", ";;;", "SELECT", "****", "SELECT * FROM", "1", "'abc"):
            for result in self.db.execute(sql):
                self.assertIsInstance(result.ok, bool)

    def test_long_identifier(self):
        name = "c" * 300
        result = self.exec_one(f"CREATE TABLE {name}(id INT);")
        self.assertIsNone(result.error)
        self.assertIn(name, self.db.tables())

    def test_case_insensitive(self):
        self.db.execute("insert into STUDENT(ID,NAME,AGE) values (9,'Zed',30);")
        self.assertEqual(self.rows_of("select id from student where ID = 9;"), [[9]])

    def test_multi_statement_error_isolation(self):
        results = self.db.execute(
            "INSERT INTO student VALUES (1,'x',1); SELECT bad FROM student; INSERT INTO student VALUES (2,'y',2);"
        )
        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertTrue(results[2].ok)
        # 出错语句不影响后续语句：最终有 2 行
        self.assertEqual(len(self.rows_of("SELECT * FROM student;")), 2)


if __name__ == "__main__":
    unittest.main()

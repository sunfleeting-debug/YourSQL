"""扩展：Fuzz 测试。

思路（计划书「Fuzz 测试」扩展项）：
  1. 生成器：按文法随机生成「合法 SQL」，检查指标 Crash / Wrong Reject
  2. 变异器：对合法 SQL 做字符级变异（删除 / 插入 / 替换 / 截断 / 关键字大小写），
     检查指标 Crash / Wrong Accept，系统必须「拒绝且不崩溃」

判定标准：无论输入如何，`Database.execute` 都不得抛出非 MiniSQLError 的异常；
合法 SQL 必须全部成功；非法 SQL 必须被拒绝（错误来自四个阶段之一）。

运行：python -m unittest database_system.tests.test_fuzz -v
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.engine.database import Database
from database_system.utils.errors import MiniSQLError

COLUMNS = ["id", "age", "score"]
STR_COLUMNS = ["name"]
TABLES = ["student", "course"]
OPS = ["=", "!=", ">", ">=", "<", "<="]
KEYWORDS = ["SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "INSERT", "INTO",
            "VALUES", "DELETE", "UPDATE", "SET", "CREATE", "TABLE", "INT", "VARCHAR"]


def gen_int() -> int:
    return random.randint(-100, 100)


def gen_string() -> str:
    alphabet = "abcXY_0123 '"
    text = "".join(random.choice(alphabet) for _ in range(random.randint(0, 6)))
    return "'" + text.replace("'", "''") + "'"   # 转义后再用引号包裹


def gen_predicate(depth: int = 0) -> str:
    choice = random.random()
    if depth < 2 and choice < 0.25:
        left = gen_predicate(depth + 1)
        right = gen_predicate(depth + 1)
        return f"({left} {random.choice(['AND', 'OR'])} {right})"
    if depth < 2 and choice < 0.35:
        return f"NOT {gen_predicate(depth + 1)}"
    if random.random() < 0.5:
        return f"{random.choice(COLUMNS)} {random.choice(OPS)} {gen_int()}"
    return f"{random.choice(STR_COLUMNS)} {random.choice(['=', '!='])} {gen_string()}"


def gen_select() -> str:
    cols = random.sample(COLUMNS + STR_COLUMNS, random.randint(1, 3))
    sql = f"SELECT {', '.join(cols)} FROM {random.choice(TABLES)}"
    if random.random() < 0.7:
        sql += f" WHERE {gen_predicate()}"
    if random.random() < 0.3:
        sql += f" ORDER BY {random.choice(COLUMNS)} {'DESC' if random.random() < 0.5 else 'ASC'}"
    if random.random() < 0.2:
        sql += f" LIMIT {random.randint(0, 5)}"
    return sql + ";"


def gen_insert() -> str:
    table = random.choice(TABLES)
    return (
        f"INSERT INTO {table}(id, name, age, score) VALUES "
        f"({gen_int()}, {gen_string()}, {gen_int()}, {gen_int()});"
    )


def gen_delete() -> str:
    sql = f"DELETE FROM {random.choice(TABLES)}"
    if random.random() < 0.8:
        sql += f" WHERE {gen_predicate()}"
    return sql + ";"


def gen_update() -> str:
    column = random.choice(COLUMNS)
    sql = f"UPDATE {random.choice(TABLES)} SET {column} = {gen_int()}"
    if random.random() < 0.7:
        sql += f" WHERE {gen_predicate()}"
    return sql + ";"


def gen_valid_sql() -> str:
    return random.choice([gen_select, gen_insert, gen_delete, gen_update])()


def mutate(sql: str) -> str:
    """字符级 / 单词级变异。"""
    if not sql:
        return ";"
    kind = random.randint(0, 5)
    pos = random.randrange(len(sql))
    if kind == 0:                       # 删除字符
        return sql[:pos] + sql[pos + 1 :]
    if kind == 1:                       # 插入随机字符
        return sql[:pos] + random.choice("@#$%^&*()[]{}<>?/\\|~`\"'.,;:") + sql[pos:]
    if kind == 2:                       # 替换字符
        return sql[:pos] + random.choice("@#$%^&~`\"'") + sql[pos + 1 :]
    if kind == 3:                       # 截断
        return sql[:pos]
    if kind == 4:                       # 关键字改大小写 / 打乱
        upper = "".join(
            ch.upper() if random.random() < 0.5 else ch.lower() for ch in sql
        )
        return upper
    words = sql.split()                 # 随机删除一个单词
    if len(words) > 1:
        del words[random.randrange(len(words))]
    return " ".join(words)


class FuzzTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minisql-fuzz-")
        self.db = Database(os.path.join(self.tmp, "fuzz.db"), pool_size=6)
        self.db.execute(
            "CREATE TABLE student(id INT, name VARCHAR(20), age INT, score INT);"
        )
        self.db.execute(
            "CREATE TABLE course(id INT, name VARCHAR(20), age INT, score INT);"
        )

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_sql_never_rejected(self):
        random.seed(20260908)
        accepted = rejected = 0
        for _ in range(300):
            sql = gen_valid_sql()
            try:
                results = self.db.execute(sql)
            except MiniSQLError as err:      # 合法 SQL 被拒绝
                rejected += 1
                print(f"WRONG REJECT: {sql} -> {err}")
                continue
            except Exception as err:         # 崩溃
                self.fail(f"CRASH on {sql!r}: {type(err).__name__}: {err}")
            accepted += 1
            for result in results:
                self.assertIsNone(result.error, f"WRONG REJECT: {sql} -> {result.error}")
        self.assertEqual(rejected, 0)
        self.assertGreater(accepted, 250)

    def test_mutated_sql_never_crashes(self):
        random.seed(4242)
        crashes = 0
        accepted = rejected = 0
        for _ in range(600):
            sql = mutate(gen_valid_sql())
            try:
                results = self.db.execute(sql)
            except MiniSQLError:
                rejected += 1
                continue
            except Exception as err:
                crashes += 1
                print(f"CRASH: {sql!r} -> {type(err).__name__}: {err}")
                continue
            for result in results:
                if result.error is None:
                    accepted += 1
                else:
                    rejected += 1
                    self.assertIsNotNone(result.error.error_type)
        self.assertEqual(crashes, 0, f"{crashes} 次崩溃")
        self.assertGreater(rejected, 50)   # 变异后大部分应被拒绝

    def test_random_bytes_never_crash(self):
        random.seed(7)
        for _ in range(200):
            sql = "".join(
                chr(random.randint(32, 126)) for _ in range(random.randint(0, 40))
            )
            try:
                self.db.execute(sql)
            except MiniSQLError:
                pass
            except Exception as err:
                self.fail(f"CRASH on random bytes {sql!r}: {type(err).__name__}: {err}")


if __name__ == "__main__":
    unittest.main()

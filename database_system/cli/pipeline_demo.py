"""全链路输出演示：Token -> AST -> 语义检查 -> 逻辑计划 -> 优化后计划 -> 执行结果。

这是计划书中「输出」类交付物（Token → AST → Semantic → Plan → Optimized Plan
全链路输出示例）的可复现脚本。

运行：
    python -m database_system.cli.pipeline_demo
    python -m database_system.cli.pipeline_demo --sql "SELECT ..."
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.engine.database import Database
from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.lexer import format_tokens
from database_system.utils.helpers import format_table

DEMO = [
    "CREATE TABLE student(id INT, name VARCHAR(20), age INT, score INT);",
    "INSERT INTO student(id,name,age,score) VALUES "
    "(1,'Alice',20,95),(2,'Bob',17,88),(3,'Tom''s',21,95),(4,'Dan',20,71);",
    "SELECT name, score FROM student WHERE age > 10 + 8 AND score >= 90;",
    "SELECT id, name FROM student WHERE 1 = 1 AND age > 18;",
    "SELECT DISTINCT score FROM student ORDER BY score DESC LIMIT 3;",
    "UPDATE student SET score = score + 5 WHERE age < 18;",
    "DELETE FROM student WHERE id = 4;",
    "SELECT * FROM student;",
]

ERROR_DEMO = [
    "SELECT @ FROM student;",
    "SELECT id FROM student WHERE;",
    "SELECT score2 FROM student;",
    "SELECT * FROM student WHERE id + name > 1;",
]


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show_pipeline(db: Database, sql: str, with_tokens: bool = False) -> None:
    section(f"SQL: {sql}")
    results = db.execute(sql)
    for result in results:
        if with_tokens:
            print("\n[1] Token 流")
            print(format_tokens(result.tokens))
        if result.error is not None:
            print(f"\n{result.error}   <- 由 {result.stage} 阶段检出")
            continue
        print("\n[2] AST")
        print(result.ast_tree)
        print("\n[3] 语义检查通过（名字绑定已完成，类型已标注）")
        print("\n[4] 逻辑执行计划（优化前）")
        print(result.plan_before)
        print("\n[5] 逻辑执行计划（优化后）")
        print(result.plan_after)
        if result.rules:
            print("    生效规则: " + "; ".join(result.rules))
        print("    S 表达式: " + result.plan_sexpr)
        print("\n[6] 执行结果")
        if result.rows and result.columns:
            print(format_table(result.columns, result.rows))
        print("    " + result.message)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MiniSQL 全链路输出演示")
    parser.add_argument("--sql", action="append", default=[],
                        help="自定义 SQL（可多次指定）；默认运行内置演示脚本")
    parser.add_argument("--tokens", action="store_true", help="输出 Token 流")
    args = parser.parse_args(argv)

    path = os.path.join(tempfile.mkdtemp(prefix="minisql-demo-"), "demo.db")
    db = Database(path, pool_size=8)
    try:
        print(f"演示数据库: {path}（临时目录，退出后可自行删除）")
        for sql in (args.sql or DEMO):
            show_pipeline(db, sql, with_tokens=args.tokens)

        section("错误诊断演示：同一条错误 SQL 会被哪个阶段拦下")
        for sql in ERROR_DEMO:
            result = db.execute(sql)[0]
            stage = result.stage
            print(f"{sql:<50} -> {stage} 阶段: {result.error}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""阶段 1-4 测试：词法 / 语法 / 语义 / 执行计划与优化。

运行：python -m unittest database_system.tests.test_sql -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.catalog import Catalog
from database_system.sql_compiler.lexer import tokenize
from database_system.sql_compiler.optimizer import Optimizer
from database_system.sql_compiler.parser import Parser
from database_system.sql_compiler.planner import (
    FilterPlan,
    Planner,
    ProjectPlan,
    SeqScanPlan,
)
from database_system.sql_compiler.semantic import SemanticAnalyzer
from database_system.utils.constants import TokenType
from database_system.utils.errors import LexicalError, ParseError, SemanticError

SETUP = "CREATE TABLE student(id INT, name VARCHAR(20), age INT);"


def analyze(sql: str, catalog: Catalog | None = None):
    """建表 -> 解析 -> 语义分析，返回 (语句列表, catalog)。"""
    catalog = catalog or Catalog()
    stmts = Parser(tokenize(sql)).parse()
    analyzer = SemanticAnalyzer(catalog)
    for stmt in stmts:
        analyzer.analyze(stmt)
    return stmts, catalog


class LexerTest(unittest.TestCase):
    def test_basic_tokens(self):
        tokens = tokenize("SELECT id FROM t WHERE a>=1;")
        kinds = [t.category for t in tokens]
        self.assertEqual(
            kinds,
            ["KEYWORD", "IDENTIFIER", "KEYWORD", "IDENTIFIER", "KEYWORD",
             "IDENTIFIER", "OPERATOR", "CONST", "DELIMITER", "EOF"],
        )
        self.assertEqual(tokens[1].lexeme, "id")
        self.assertEqual(tokens[6].lexeme, ">=")

    def test_position(self):
        tokens = tokenize("SELECT\n  id;")
        self.assertEqual((tokens[0].line, tokens[0].column), (1, 1))
        self.assertEqual((tokens[1].line, tokens[1].column), (2, 3))

    def test_keyword_case_insensitive(self):
        tokens = tokenize("select ID from T;")
        self.assertEqual(tokens[0].value, "SELECT")
        self.assertEqual(tokens[0].lexeme, "select")

    def test_comments(self):
        tokens = tokenize("-- comment\nSELECT 1; /* block\ncomment */ SELECT 2;")
        ints = [t.value for t in tokens if t.type == TokenType.INT_CONST]
        self.assertEqual(ints, [1, 2])

    def test_string_escape(self):
        tokens = tokenize("INSERT INTO t VALUES ('Tom''s book');")
        strings = [t.value for t in tokens if t.type == TokenType.STRING_CONST]
        self.assertEqual(strings, ["Tom's book"])

    def test_illegal_character(self):
        with self.assertRaises(LexicalError) as ctx:
            tokenize("SELECT @ FROM t;")
        self.assertEqual(ctx.exception.line, 1)
        self.assertEqual(ctx.exception.column, 8)

    def test_unterminated_string(self):
        with self.assertRaises(LexicalError):
            tokenize("SELECT 'abc FROM t;")

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexicalError):
            tokenize("/* abc SELECT 1;")

    def test_invalid_number(self):
        for bad in ("SELECT 12ab;", "SELECT 1.5;"):
            with self.assertRaises(LexicalError):
                tokenize(bad)

    def test_multi_char_operators(self):
        for op in ("!=", "<>", ">=", "<=", "=", "<", ">"):
            tokens = tokenize(f"SELECT * FROM t WHERE a {op} 1;")
            self.assertIn(op, [t.lexeme for t in tokens])


class ParserTest(unittest.TestCase):
    def test_four_statements(self):
        sql = (
            "CREATE TABLE student(id INT, name VARCHAR(20), age INT);"
            "INSERT INTO student(id,name,age) VALUES (1,'Alice',20);"
            "SELECT id, name FROM student WHERE age > 18 AND id != 3;"
            "DELETE FROM student WHERE id = 1;"
        )
        stmts = Parser(tokenize(sql)).parse()
        self.assertEqual(len(stmts), 4)
        self.assertIsInstance(stmts[0], ast.CreateTable)
        self.assertIsInstance(stmts[1], ast.Insert)
        self.assertIsInstance(stmts[2], ast.Select)
        self.assertIsInstance(stmts[3], ast.Delete)

    def test_and_binds_tighter_than_or(self):
        stmt = Parser(tokenize("SELECT a FROM t WHERE a = 1 OR b = 2 AND c = 3;")).parse()[0]
        self.assertEqual(stmt.where.op, "OR")
        self.assertEqual(stmt.where.right.op, "AND")

    def test_not_binds_tighter_than_comparison(self):
        stmt = Parser(tokenize("SELECT a FROM t WHERE NOT a = 1;")).parse()[0]
        self.assertIsInstance(stmt.where, ast.Unary)
        self.assertEqual(stmt.where.op, "NOT")
        self.assertIsInstance(stmt.where.operand, ast.Binary)

    def test_parenthesis_changes_precedence(self):
        stmt = Parser(tokenize("SELECT a FROM t WHERE (a = 1 OR b = 2) AND c = 3;")).parse()[0]
        self.assertEqual(stmt.where.op, "AND")
        self.assertEqual(stmt.where.left.op, "OR")

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError) as ctx:
            Parser(tokenize("SELECT a FROM t")).parse()
        self.assertIn("expected", str(ctx.exception))

    def test_unbalanced_parenthesis(self):
        with self.assertRaises(ParseError):
            Parser(tokenize("SELECT a FROM t WHERE (a = 1;")).parse()

    def test_error_position(self):
        with self.assertRaises(ParseError) as ctx:
            Parser(tokenize("CREATE TABLE t(id INT)\nCREATE TABLE u(x BAD);")).parse()
        self.assertGreaterEqual(ctx.exception.line, 1)

    def test_multi_row_insert(self):
        stmt = Parser(tokenize("INSERT INTO t VALUES (1,'a'),(2,'b');")).parse()[0]
        self.assertEqual(len(stmt.rows), 2)

    def test_select_star(self):
        stmt = Parser(tokenize("SELECT * FROM t;")).parse()[0]
        self.assertIsInstance(stmt.items[0].expr, ast.Star)

    def test_distinct_order_limit(self):
        stmt = Parser(tokenize("SELECT DISTINCT a FROM t ORDER BY b DESC LIMIT 2;")).parse()[0]
        self.assertTrue(stmt.distinct)
        self.assertTrue(stmt.order_by[0].desc)
        self.assertEqual(stmt.limit, 2)

    def test_table_alias(self):
        stmt = Parser(tokenize("SELECT s.id FROM student AS s WHERE s.age > 1;")).parse()[0]
        self.assertEqual(stmt.from_alias, "s")
        self.assertEqual(stmt.items[0].expr.qualifier, "s")


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        SemanticAnalyzer(self.catalog).analyze(Parser(tokenize(SETUP)).parse()[0])

    def run_sql(self, sql: str):
        return analyze(sql, self.catalog)

    def test_unknown_table(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql("SELECT * FROM nosuch;")
        self.assertIn("does not exist", str(ctx.exception))

    def test_unknown_column(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql("SELECT score FROM student;")
        self.assertIn("column 'score'", str(ctx.exception))

    def test_arith_type_mismatch(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql("SELECT * FROM student WHERE id + name > 1;")
        self.assertIn("cannot be applied to INT and VARCHAR", str(ctx.exception))

    def test_compare_type_mismatch(self):
        with self.assertRaises(SemanticError):
            self.run_sql("SELECT * FROM student WHERE id > 'abc';")

    def test_where_must_be_bool(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql("SELECT * FROM student WHERE id;")
        self.assertIn("must be of type BOOL", str(ctx.exception))

    def test_and_requires_bool(self):
        with self.assertRaises(SemanticError):
            self.run_sql("SELECT * FROM student WHERE id AND age;")

    def test_name_binding(self):
        stmts, _ = self.run_sql("SELECT id FROM student WHERE age > 1;")
        ident = stmts[0].items[0].expr
        self.assertEqual(ident.ref.ordinal, 0)
        self.assertEqual(ident.ref.column_name, "id")

    def test_insert_column_count_mismatch(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql("INSERT INTO student(id,name) VALUES (1,'a',20);")
        self.assertIn("column count mismatch", str(ctx.exception))

    def test_insert_type_mismatch(self):
        with self.assertRaises(SemanticError):
            self.run_sql("INSERT INTO student(id,name,age) VALUES ('x','a',20);")

    def test_insert_unknown_column(self):
        with self.assertRaises(SemanticError):
            self.run_sql("INSERT INTO student(nope) VALUES (1);")

    def test_duplicate_table(self):
        with self.assertRaises(SemanticError) as ctx:
            self.run_sql(SETUP)
        self.assertIn("already exists", str(ctx.exception))

    def test_duplicate_column(self):
        with self.assertRaises(SemanticError):
            self.run_sql("CREATE TABLE t2(id INT, id INT);")

    def test_catalog_interface(self):
        self.assertIsNotNone(self.catalog.find_table("STUDENT"))  # 大小写不敏感
        self.assertEqual(str(self.catalog.get_type("student", "age")), "INT")
        self.assertIsNone(self.catalog.find_column("student", "nope"))
        self.assertEqual(self.catalog.table_names(), ["student"])


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        SemanticAnalyzer(self.catalog).analyze(Parser(tokenize(SETUP)).parse()[0])

    def plan(self, sql: str):
        stmts, _ = analyze(sql, self.catalog)
        return Planner(self.catalog).build(stmts[-1])

    def test_select_pipeline(self):
        plan = self.plan("SELECT name FROM student WHERE age > 18;")
        self.assertIsInstance(plan, ProjectPlan)
        self.assertIsInstance(plan.child, FilterPlan)
        self.assertIsInstance(plan.child.child, SeqScanPlan)

    def test_delete_pipeline(self):
        from database_system.sql_compiler.planner import DeletePlan

        plan = self.plan("DELETE FROM student WHERE id = 1;")
        self.assertIsInstance(plan, DeletePlan)
        self.assertIsInstance(plan.child, FilterPlan)

    def test_plan_outputs(self):
        plan = self.plan("SELECT id, name FROM student WHERE age > 18;")
        self.assertIn("SeqScan", plan.to_tree())
        self.assertIn("SeqScan", plan.to_s_expr())
        self.assertIn("SeqScan", plan.to_json())

    def test_constant_folding(self):
        plan = self.plan("SELECT name FROM student WHERE age > 10 + 8;")
        _, rules = Optimizer().optimize(plan)
        self.assertIn("常量折叠 ConstantFolding", rules)
        self.assertIn("age > 18", plan.to_tree())

    def test_boolean_simplification(self):
        plan = self.plan("SELECT id FROM student WHERE 1 = 1 AND age > 5;")
        optimized, rules = Optimizer().optimize(plan)
        self.assertIn("布尔化简 BooleanSimplification", rules)
        self.assertNotIn("1 = 1", optimized.to_tree())

    def test_filter_true_removed(self):
        plan = self.plan("SELECT id FROM student WHERE 1 = 1;")
        optimized, rules = Optimizer().optimize(plan)
        self.assertIn("冗余节点消除 RedundantNodeElimination", rules)
        self.assertNotIn("Filter", optimized.to_tree())

    def test_projection_pruning(self):
        plan = self.plan("SELECT name FROM student WHERE age > 18;")
        Optimizer().optimize(plan)
        scan = plan.child.child
        self.assertIsInstance(scan, SeqScanPlan)
        names = [scan.columns[i].name for i in scan.projection]
        self.assertEqual(sorted(names), ["age", "name"])

    def test_predicate_split(self):
        plan = self.plan("SELECT id FROM student WHERE age > 5 AND id != 3;")
        optimized, rules = Optimizer().optimize(plan)
        self.assertIn("谓词分解 PredicateSplitting", rules)
        self.assertEqual(optimized.to_tree().count("Filter"), 2)


if __name__ == "__main__":
    unittest.main()

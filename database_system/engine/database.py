"""数据库系统门面：把「编译器 -> 执行计划 -> 执行引擎 -> 页式存储」串成一条链路。

    SQL 文本
      -> Lexer        Token 流
      -> Parser       AST
      -> Semantic     语义检查 + 名字绑定（依赖 Catalog）
      -> Planner      逻辑执行计划
      -> Optimizer    规则式优化（可关闭，便于对比优化前后）
      -> Executor     执行（CreateTable / Insert / SeqScan / Filter / Project ...）
      -> Buffer/Disk  页式存储落盘
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from database_system.engine.catalog_manager import CatalogManager
from database_system.engine.executor import (
    DmlExecutor,
    ExecContext,
    collect_rows,
    create_executor,
)
from database_system.sql_compiler import ast_nodes as ast
from database_system.sql_compiler.lexer import tokenize
from database_system.sql_compiler.optimizer import Optimizer
from database_system.sql_compiler.parser import Parser
from database_system.sql_compiler.planner import Planner
from database_system.sql_compiler.semantic import SemanticAnalyzer
from database_system.storage.buffer import BufferPoolManager
from database_system.storage.file_manager import DiskManager
from database_system.utils.errors import MiniSQLError


@dataclass
class StatementResult:
    sql: str = ""
    ok: bool = False
    stage: str = ""          # Lexer / Parser / Semantic / Planner / Execute
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    message: str = ""
    error: Optional[MiniSQLError] = None
    tokens: list = field(default_factory=list)
    ast_tree: str = ""
    plan_before: str = ""
    plan_after: str = ""
    plan_sexpr: str = ""
    rules: list = field(default_factory=list)

    @property
    def error_type(self) -> str:
        return self.error.error_type if self.error else ""

    def __str__(self) -> str:
        if self.error is not None:
            return str(self.error)
        return self.message


def _slice_source(text: str, start_tok, end_tok) -> str:
    """按 Token 位置切出单条语句的源码文本。"""
    lines = text.split("\n")
    try:
        if start_tok.line == end_tok.line:
            line = lines[start_tok.line - 1]
            return line[start_tok.column - 1 : end_tok.column - 1 + len(end_tok.lexeme)]
        first = lines[start_tok.line - 1][start_tok.column - 1 :]
        last = lines[end_tok.line - 1][: end_tok.column - 1 + len(end_tok.lexeme)]
        middle = lines[start_tok.line : end_tok.line - 1]
        return "\n".join([first] + middle + [last]).strip()
    except Exception:
        return text


class Database:
    """MiniSQL 数据库实例。"""

    def __init__(self, path: str = "mini.db", pool_size: int = 16,
                 policy: str = "LRU", buffer_verbose: bool = False,
                 enable_optimizer: bool = True, show_plan: bool = False):
        self.path = path
        self.disk = DiskManager(path)
        self.buffer = BufferPoolManager(self.disk, pool_size, policy, buffer_verbose)
        self.catalog_manager = CatalogManager(self.disk, self.buffer)
        self.catalog = self.catalog_manager.load()
        self.ctx = ExecContext(self.catalog, self.buffer, self.catalog_manager)
        self.enable_optimizer = enable_optimizer
        self.show_plan = show_plan
        self.last_result: Optional[StatementResult] = None

    # ------------------------------ 执行入口 ------------------------------

    def execute(self, sql: str) -> list:
        """执行一段（可含多条语句的）SQL，返回每条语句的结果。"""
        results: list = []

        # 阶段 1：词法
        try:
            tokens = tokenize(sql)
        except MiniSQLError as err:
            return [StatementResult(sql=sql, ok=False, stage="Lexer", error=err)]

        # 阶段 2：语法
        parser = Parser(tokens)
        try:
            statements = parser.parse()
        except MiniSQLError as err:
            return [StatementResult(sql=sql, ok=False, stage="Parser",
                                    error=err, tokens=tokens)]
        if not statements:
            return []

        for i, stmt in enumerate(statements):
            start, end = parser.spans[i]
            text = _slice_source(sql, tokens[start], tokens[end]) if parser.spans else sql
            results.append(self._run_statement(stmt, text, tokens, (start, end)))

        self.buffer.flush_all()
        self.last_result = results[-1] if results else None
        return results

    # ------------------------------ 单条语句流水线 ------------------------------

    def _run_statement(self, stmt, sql_text: str, tokens: list, span) -> StatementResult:
        result = StatementResult(sql=sql_text, tokens=tokens[span[0] : span[1] + 1])

        # 阶段 3：语义分析（含 Catalog 维护）
        try:
            SemanticAnalyzer(self.catalog).analyze(stmt)
        except MiniSQLError as err:
            result.stage, result.error = "Semantic", err
            return result

        # 阶段 4：执行计划 + 优化
        try:
            plan = Planner(self.catalog).build(stmt)
        except MiniSQLError as err:
            result.stage, result.error = "Planner", err
            return result

        result.ast_tree = ast.to_tree(stmt)
        result.plan_before = plan.to_tree()
        if self.enable_optimizer:
            plan_after, rules = Optimizer().optimize(plan)
        else:
            plan_after, rules = plan, []
        result.plan_after = plan_after.to_tree()
        result.plan_sexpr = plan_after.to_s_expr()
        result.rules = rules

        # EXPLAIN：只输出计划，不执行
        if isinstance(stmt, ast.Explain):
            result.stage, result.ok = "Planner", True
            result.message = "(EXPLAIN: 只生成执行计划，未实际执行)"
            return result

        # 阶段 6：执行
        try:
            executor = create_executor(plan_after, self.ctx)
            if isinstance(executor, DmlExecutor):
                message, affected = executor.run()
                result.message, result.ok, result.stage = message, True, "Execute"
                result.rows = [[affected]]
            else:
                result.columns = executor.output_columns()
                result.rows = collect_rows(executor)
                result.stage, result.ok = "Execute", True
                result.message = f"{len(result.rows)} row(s) returned"
        except MiniSQLError as err:
            result.stage, result.error = "Execute", err
        return result

    # ------------------------------ 辅助接口 ------------------------------

    def tables(self) -> list:
        return self.catalog.table_names()

    def schema(self, table_name: str) -> Optional[str]:
        schema = self.catalog.find_table(table_name)
        return None if schema is None else str(schema)

    def buffer_stats(self) -> dict:
        return self.buffer.stats.to_dict()

    def reset_stats(self) -> None:
        self.buffer.stats = type(self.buffer.stats)()
        self.buffer.log.clear()

    def close(self) -> None:
        self.buffer.flush_all()
        self.disk.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

"""串联 Lexer、Parser、Binder 和计划生成的编译器入口。"""

from __future__ import annotations

from dataclasses import dataclass

from yoursql.sql.ast import Statement
from yoursql.sql.binder import Binder, BoundStatement, CatalogProtocol
from yoursql.sql.lexer import Token, tokenize
from yoursql.sql.parser import Parser
from yoursql.planner.logical import LogicalPlanNode, plan_from_statement
from yoursql.planner.physical import PhysicalPlanNode


@dataclass(frozen=True)
class CompilationResult:
    """编译各阶段产物，供调试、EXPLAIN 和测试使用。"""

    tokens: tuple[Token, ...]
    statement: Statement
    bound: BoundStatement
    plan: LogicalPlanNode
    optimized_plan: PhysicalPlanNode | None = None

    @property
    def ast(self) -> Statement:
        """返回编译结果中的 AST。"""
        return self.statement

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        result: dict[str, object] = {
            "tokens": [token.as_dict() for token in self.tokens],
            "ast": self.statement.to_dict(),
            "bound": self.bound.to_dict(),
            "plan": self.plan.to_dict(),
        }
        if self.optimized_plan is not None:
            result["optimized_plan"] = self.optimized_plan.to_dict()
        return result


class Compiler:
    """不修改 Catalog 的 SQL 编译入口。"""

    def compile(
        self, sql: str, catalog: CatalogProtocol | None = None
    ) -> CompilationResult:
        """编译输入 SQL 并返回各阶段产物。"""
        tokens = tuple(tokenize(sql))
        statement = Parser(tokens).parse_one()
        bound = Binder(catalog).bind(statement)
        return CompilationResult(
            tokens, statement, bound, plan_from_statement(statement)
        )

    def compile_script(
        self, sql: str, catalog: CatalogProtocol | None = None
    ) -> tuple[CompilationResult, ...]:
        """编译 SQL 脚本并返回各语句结果。"""
        tokens = tuple(tokenize(sql))
        statements = Parser(tokens).parse_script()
        return tuple(
            CompilationResult(
                tokens,
                statement,
                Binder(catalog).bind(statement),
                plan_from_statement(statement),
            )
            for statement in statements
        )


def compile_sql(sql: str, catalog: CatalogProtocol | None = None) -> CompilationResult:
    """编译单条 SQL 并返回编译结果。"""
    return Compiler().compile(sql, catalog)


__all__ = ["CompilationResult", "Compiler", "compile_sql"]

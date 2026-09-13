"""串联 Lexer、Parser、Binder 和计划生成的编译器入口。"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.errors import ParserError
from .ast import Statement
from .binder import Binder, BoundStatement, CatalogProtocol
from .lexer import Token, tokenize
from .parser import Parser
from .plan import PlanNode, plan_from_statement


@dataclass(frozen=True)
class CompilationResult:
    """编译各阶段产物，供调试、EXPLAIN 和测试使用。"""

    tokens: tuple[Token, ...]
    statement: Statement
    bound: BoundStatement
    plan: PlanNode
    optimized_plan: PlanNode | None = None

    @property
    def ast(self) -> Statement:
        return self.statement

    def to_dict(self) -> dict[str, object]:
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

    def compile(self, sql: str, catalog: CatalogProtocol | None = None) -> CompilationResult:
        tokens = tuple(tokenize(sql))
        statement = Parser(tokens).parse_one()
        bound = Binder(catalog).bind(statement)
        return CompilationResult(tokens, statement, bound, plan_from_statement(statement))

    def compile_script(self, sql: str, catalog: CatalogProtocol | None = None) -> tuple[CompilationResult, ...]:
        tokens = tuple(tokenize(sql))
        statements = Parser(tokens).parse_script()
        return tuple(
            CompilationResult(tokens, statement, (bound := Binder(catalog).bind(statement)), plan_from_statement(statement))
            for statement in statements
        )


def compile_sql(sql: str, catalog: CatalogProtocol | None = None) -> CompilationResult:
    return Compiler().compile(sql, catalog)


__all__ = ["CompilationResult", "Compiler", "compile_sql"]

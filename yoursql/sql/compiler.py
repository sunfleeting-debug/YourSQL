"""串联 Lexer、Parser、Binder 和计划生成的编译器入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from yoursql.sql.ast import Statement
from yoursql.sql.binder import Binder, BoundStatement, CatalogProtocol
from yoursql.sql.lexer import Token, tokenize
from yoursql.sql.parser import Parser

# WHY：planner 侧的导入必须留到函数内部执行。`planner.logical` 会反向导入
# `..sql.ast`，而后者会先把 `yoursql.sql` 这个包跑完；如果这里在模块顶层导入
# planner，就会出现 "yoursql.sql → compiler → planner.logical(未完成)" 的循环，
# 失败与否取决于调用方先 import 哪个包——这种隐式顺序依赖非常难查。
if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from ..planner.logical import LogicalPlanNode
    from ..planner.physical import PhysicalPlanNode


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

    def compile(
        self, sql: str, catalog: CatalogProtocol | None = None
    ) -> CompilationResult:
        from ..planner.logical import plan_from_statement

        tokens = tuple(tokenize(sql))
        statement = Parser(tokens).parse_one()
        bound = Binder(catalog).bind(statement)
        return CompilationResult(
            tokens, statement, bound, plan_from_statement(statement)
        )

    def compile_script(
        self, sql: str, catalog: CatalogProtocol | None = None
    ) -> tuple[CompilationResult, ...]:
        from ..planner.logical import plan_from_statement

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
    return Compiler().compile(sql, catalog)


__all__ = ["CompilationResult", "Compiler", "compile_sql"]

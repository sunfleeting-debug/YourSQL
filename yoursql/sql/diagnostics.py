"""SQL 前端的诊断收集：一次解析里把多个错误报全，而不是遇到第一个就停。

WHY：单错误返回对"现场验收 + 批量导入 SQL 脚本"都不好用——一个 20 行的脚本里
有 3 处笔误，用户得改一次跑一次。这里把词法与语法错误统一成 ``Diagnostic``，
配合 ``Lexer.tokenize_recovering`` / ``Parser.parse_recovering`` 的恐慌模式恢复，
一次就能给出全部错误及各自的行列位置。

NOTE：既有 API（``tokenize`` / ``parse_one`` / ``parse_script``）保持"首错即抛"的
行为不变，恢复式解析是新增入口，不改变既有调用方的契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yoursql.common.contracts import JsonObject, JsonValue
from yoursql.common.errors import YourSQLError
from yoursql.sql.ast import Statement


@dataclass(frozen=True)
class Diagnostic:
    """一条前端诊断（词法 / 语法 / 语义）。"""

    stage: str  # lexer / parser / binder
    code: str  # LEXER_ERROR / PARSER_ERROR / BINDER_ERROR
    message: str
    line: int | None = None
    column: int | None = None
    expected: str | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def from_error(cls, stage: str, error: YourSQLError) -> "Diagnostic":
        expected = error.details.get("expected")
        details = {
            key: value for key, value in error.details.items() if key != "expected"
        }
        return cls(
            stage=stage,
            code=error.code,
            message=error.message,
            line=error.line,
            column=error.column,
            expected=expected if isinstance(expected, str) else None,
            details=details,
        )

    @property
    def location(self) -> tuple[int, int] | None:
        if self.line is None:
            return None
        return (self.line, self.column if self.column is not None else 1)

    def __str__(self) -> str:
        location = ""
        if self.line is not None:
            location = f" at line {self.line}"
            if self.column is not None:
                location += f", column {self.column}"
        hint = f"（期望 {self.expected}）" if self.expected else ""
        return f"[{self.code}]{location}: {self.message}{hint}"

    def as_dict(self) -> JsonObject:
        result: JsonObject = {
            "stage": self.stage,
            "error": self.code,
            "message": self.message,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.expected is not None:
            result["expected"] = self.expected
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class ParseOutcome:
    """一次恢复式解析的产物：能解析出来的语句 + 收集到的全部诊断。"""

    statements: tuple[Statement, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    @property
    def failed(self) -> int:
        return len(self.diagnostics)

    def report(self) -> str:
        """把诊断渲染成多行文本，供 CLI / 工作台直接展示。"""

        if not self.diagnostics:
            return "解析通过，共 %d 条语句。" % len(self.statements)
        lines = [
            f"发现 {len(self.diagnostics)} 处错误"
            f"（已成功解析 {len(self.statements)} 条语句）："
        ]
        lines.extend(f"  {index}. {item}" for index, item in enumerate(self.diagnostics, 1))
        return "\n".join(lines)

    def as_dict(self) -> JsonObject:
        return {
            "ok": self.ok,
            "statements": len(self.statements),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


__all__ = ["Diagnostic", "ParseOutcome"]

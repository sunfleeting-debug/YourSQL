"""YourSQL 各层共享的结构化异常。"""

from __future__ import annotations

from dataclasses import dataclass, field

from yoursql.common.contracts import JsonObject, JsonValue


@dataclass
class YourSQLError(Exception):
    """带机器可读错误码、位置和附加信息的基础异常。"""

    message: str
    code: str = "YOURSQL_ERROR"
    line: int | None = None
    column: int | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """完成数据类初始化后的派生状态设置。"""
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        """返回适合展示的字符串表示。"""
        location = ""
        if self.line is not None:
            location = f" at line {self.line}"
            if self.column is not None:
                location += f", column {self.column}"
        return f"[{self.code}]{location}: {self.message}"

    def as_dict(self) -> JsonObject:
        """将对象转换为可序列化的字典。"""
        result: JsonObject = {"error": self.code, "message": self.message}
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.details:
            result["details"] = self.details
        return result


class LexerError(YourSQLError):
    """词法阶段错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "LEXER_ERROR", line, column, details)


class ParserError(YourSQLError):
    """语法阶段错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "PARSER_ERROR", line, column, details)


class BinderError(YourSQLError):
    """语义绑定阶段错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "BINDER_ERROR", line, column, details)


class CatalogError(YourSQLError):
    """系统目录错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "CATALOG_ERROR", line, column, details)


class StorageError(YourSQLError):
    """页文件、缓存和记录存储错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "STORAGE_ERROR", line, column, details)


class ExecutionError(YourSQLError):
    """执行算子或约束错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "EXECUTION_ERROR", line, column, details)


class AuthorizationError(YourSQLError):
    """权限检查错误。"""

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        **details: JsonValue,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        super().__init__(message, "AUTHORIZATION_ERROR", line, column, details)

"""标识符、SQL 值、模式、统计信息和统一执行结果。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping

from .errors import BinderError


class DataType(str, Enum):
    """SQL 子集支持的数据类型。"""

    INT = "INT"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"
    VARCHAR = "VARCHAR"
    NULL = "NULL"

    @classmethod
    def parse(cls, name: str) -> "DataType":
        normalized = name.strip().upper()
        aliases = {
            "INTEGER": cls.INT,
            "REAL": cls.FLOAT,
            "DOUBLE": cls.FLOAT,
            "BOOL": cls.BOOLEAN,
            "TEXT": cls.VARCHAR,
            "STRING": cls.VARCHAR,
        }
        selected = aliases.get(normalized)
        if selected is not None:
            return selected
        try:
            return cls[normalized]
        except KeyError as exc:
            raise BinderError(f"不支持的数据类型 {name!r}") from exc


@dataclass(frozen=True, order=True)
class TableId:
    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, order=True)
class PageId:
    value: int

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, order=True)
class RowId:
    page_id: PageId
    slot_id: int

    def as_tuple(self) -> tuple[int, int]:
        return int(self.page_id), self.slot_id


@dataclass(frozen=True)
class Value:
    """携带 SQL 类型和值的轻量包装。"""

    data_type: DataType
    value: Any

    @classmethod
    def null(cls) -> "Value":
        return cls(DataType.NULL, None)

    @classmethod
    def infer(cls, value: Any) -> "Value":
        if value is None:
            return cls.null()
        if isinstance(value, bool):
            return cls(DataType.BOOLEAN, value)
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(DataType.INT, value)
        if isinstance(value, float):
            return cls(DataType.FLOAT, value)
        return cls(DataType.VARCHAR, str(value))

    def coerce(self, target: DataType) -> "Value":
        if self.data_type is DataType.NULL:
            return Value(target, None)
        if self.data_type is target:
            return self
        if target is DataType.INT and self.data_type is DataType.FLOAT and math.isfinite(float(self.value)) and float(self.value).is_integer():
            return Value(target, int(self.value))
        if target is DataType.FLOAT and self.data_type is DataType.INT:
            return Value(target, float(self.value))
        if target is DataType.VARCHAR:
            return Value(target, str(self.value))
        if target is DataType.BOOLEAN and self.data_type is DataType.VARCHAR:
            lowered = str(self.value).lower()
            if lowered in {"true", "1"}:
                return Value(target, True)
            if lowered in {"false", "0"}:
                return Value(target, False)
        raise BinderError(f"不能把 {self.data_type.value} 转换为 {target.value}")

    def unwrap(self) -> Any:
        return self.value

    def __hash__(self) -> int:
        try:
            return hash((self.data_type, self.value))
        except TypeError:
            return hash((self.data_type, repr(self.value)))


@dataclass(frozen=True)
class Column:
    name: str
    data_type: DataType
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: Value | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("列名不能为空且不能带首尾空格")
        if self.primary_key and self.nullable:
            object.__setattr__(self, "nullable", False)


@dataclass(frozen=True)
class Schema:
    columns: tuple[Column, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise ValueError("模式至少要有一列")
        names = [column.name.lower() for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("列名不能重复")

    @classmethod
    def from_iterable(cls, columns: Iterable[Column]) -> "Schema":
        return cls(tuple(columns))

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    def index(self, name: str) -> int:
        target = name.lower()
        for index, column in enumerate(self.columns):
            if column.name.lower() == target:
                return index
        raise BinderError(f"列 {name!r} 不存在")

    def column(self, name: str) -> Column:
        return self.columns[self.index(name)]

    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def validate_row(self, row: Iterable[Any], *, partial: bool = False) -> tuple[Any, ...]:
        values = tuple(row)
        if len(values) > len(self.columns):
            raise BinderError(f"需要 {len(self.columns)} 个值，实际得到 {len(values)} 个")
        if not partial:
            missing = self.columns[len(values) :]
            required = [column.name for column in missing if column.default is None and not column.nullable]
            if required:
                raise BinderError(f"缺少必填列: {', '.join(required)}")
        result: list[Any] = []
        for index, column in enumerate(self.columns):
            value = values[index] if index < len(values) else (column.default.unwrap() if column.default is not None else None)
            typed = Value.infer(value)
            if typed.data_type is DataType.NULL:
                if not column.nullable and column.default is None:
                    raise BinderError(f"列 {column.name!r} 不能为 NULL")
                result.append(None)
                continue
            result.append(typed.coerce(column.data_type).unwrap())
        return tuple(result)


@dataclass(frozen=True)
class TableStats:
    row_count: int = 0
    page_count: int = 0
    distinct_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """上层 CLI、HTTP 和 Python API 共用的结构化结果。"""

    columns: tuple[str, ...] = ()
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    affected_rows: int = 0
    message: str | None = None
    plan: dict[str, Any] | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "affected_rows": self.affected_rows,
            "message": self.message,
            "plan": self.plan,
            "stats": self.stats,
        }

    def __bool__(self) -> bool:
        return bool(self.rows) or self.affected_rows > 0


def compare_values(left: Any, right: Any, operator: str) -> bool | None:
    """执行 SQL 三值逻辑中的比较；NULL 比较结果为 UNKNOWN。"""

    if left is None or right is None:
        return None
    try:
        if operator in {"=", "=="}:
            return left == right
        if operator in {"!=", "<>"}:
            return left != right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
    except TypeError:
        return False
    raise ValueError(f"不支持的比较运算符 {operator}")


def sql_truth(value: Any) -> bool:
    """WHERE/HAVING 只有 TRUE 才通过，FALSE/UNKNOWN 都过滤。"""

    return value is True

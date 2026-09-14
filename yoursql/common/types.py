"""标识符、SQL 值、模式、统计信息和统一执行结果。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable, Iterator, Mapping

from .contracts import JsonObject, SqlValue
from .errors import BinderError


class DataType(str, Enum):
    """SQL 子集支持的数据类型；`parse` 另外接受 INTEGER/REAL/DOUBLE/BOOL/TEXT/STRING 别名。"""

    INT = "INT"  # 整数
    FLOAT = "FLOAT"  # 浮点数
    DECIMAL = "DECIMAL"  # 精确十进制定点数（不带精度的 NUMERIC/DECIMAL）
    BOOLEAN = "BOOLEAN"  # 布尔（列可含 NULL）
    VARCHAR = (
        "VARCHAR"  # UTF-8 变长字符串，长度上限见 DatabaseConfig.max_varchar_length
    )
    NULL = "NULL"  # 未定型字面量或空值

    @classmethod
    def parse(cls, name: str) -> "DataType":
        normalized = name.strip().upper()
        aliases = {
            "INTEGER": cls.INT,
            "BIGINT": cls.INT,
            "REAL": cls.FLOAT,
            "DOUBLE": cls.FLOAT,
            "NUMERIC": cls.DECIMAL,
            "NUMBER": cls.DECIMAL,
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

    @property
    def is_numeric(self) -> bool:
        """是否属于可参与算术的数值类型（INT/FLOAT/DECIMAL）。"""

        return self in {DataType.INT, DataType.FLOAT, DataType.DECIMAL}


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
    value: SqlValue

    @classmethod
    def null(cls) -> "Value":
        return cls(DataType.NULL, None)

    @classmethod
    def infer(cls, value: object) -> "Value":
        if value is None:
            return cls.null()
        if isinstance(value, bool):
            return cls(DataType.BOOLEAN, value)
        if isinstance(value, Decimal):
            return cls(DataType.DECIMAL, value)
        if isinstance(value, int):
            return cls(DataType.INT, value)
        if isinstance(value, float):
            return cls(DataType.FLOAT, value)
        return cls(DataType.VARCHAR, str(value))

    def coerce(self, target: DataType) -> "Value":
        if self.data_type is DataType.NULL:
            return Value(target, None)
        if self.data_type is target:
            return self
        if target is DataType.DECIMAL:
            return Value(target, to_decimal(self.value))
        if target is DataType.FLOAT and self.data_type is DataType.DECIMAL:
            return Value(target, float(self.value))
        if (
            target is DataType.INT
            and self.data_type is DataType.FLOAT
            and math.isfinite(float(self.value))
            and float(self.value).is_integer()
        ):
            return Value(target, int(self.value))
        if (
            target is DataType.INT
            and self.data_type is DataType.DECIMAL
            and self.value == self.value.to_integral_value()
        ):
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

    def unwrap(self) -> SqlValue:
        return self.value

    def __hash__(self) -> int:
        try:
            return hash((self.data_type, self.value))
        except TypeError:
            return hash((self.data_type, repr(self.value)))


def to_decimal(value: object) -> Decimal:
    """把 SQL 值转成 Decimal。

    WHY：浮点必须先经 ``repr`` 再进 Decimal。``Decimal(0.07)`` 会把二进制的
    0.070000000000000006938893903907228377647697925567626953125 原样带入，
    定点化的意义就没了。
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise BinderError("不能把 BOOLEAN 转换为 DECIMAL")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BinderError(f"不能把 {value!r} 转换为 DECIMAL")
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise BinderError(f"不能把 {value!r} 转换为 DECIMAL") from exc
    raise BinderError(f"不能把 {type(value).__name__} 转换为 DECIMAL")


def decimal_to_json(value: Decimal) -> float | str:
    """把 Decimal 转成 JSON 可编码的数值。

    HOW：能安全放进 IEEE-754 双精度的走 float（保持 JSON 数字类型，前端和
    现有断言都不用改）；超出范围的退回字符串，避免 ``json.dumps`` 报
    ``Infinity`` 或静默丢精度。
    """

    try:
        as_float = float(value)
    except (OverflowError, InvalidOperation):
        return str(value)
    if math.isfinite(as_float):
        return as_float
    return str(value)


def json_safe(value: object) -> object:
    """把任意结果值递归转成可以直接 ``json.dumps`` 的形式。

    WHY：Decimal 不是 JSON 类型；执行结果的展示边界（CLI / HTTP / 工作台）
    统一在这里落地，内部仍保持定点精度。
    """

    if isinstance(value, Decimal):
        return decimal_to_json(value)
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return value


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

    def validate_row(
        self, row: Iterable[object], *, partial: bool = False
    ) -> tuple[SqlValue, ...]:
        values = tuple(row)
        if len(values) > len(self.columns):
            raise BinderError(
                f"需要 {len(self.columns)} 个值，实际得到 {len(values)} 个"
            )
        if not partial:
            missing = self.columns[len(values) :]
            required = [
                column.name
                for column in missing
                if column.default is None and not column.nullable
            ]
            if required:
                raise BinderError(f"缺少必填列: {', '.join(required)}")
        result: list[SqlValue] = []
        for index, column in enumerate(self.columns):
            value = (
                values[index]
                if index < len(values)
                else (column.default.unwrap() if column.default is not None else None)
            )
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
    rows: list[tuple[SqlValue, ...]] = field(default_factory=list)
    affected_rows: int = 0
    message: str | None = None
    plan: JsonObject | None = None
    stats: JsonObject = field(default_factory=dict)

    def as_dict(self) -> JsonObject:
        return {
            "columns": list(self.columns),
            "rows": [json_safe(list(row)) for row in self.rows],
            "affected_rows": self.affected_rows,
            "message": self.message,
            "plan": self.plan,
            "stats": self.stats,
        }

    def __bool__(self) -> bool:
        return bool(self.rows) or self.affected_rows > 0


def compare_values(left: SqlValue, right: SqlValue, operator: str) -> bool | None:
    """执行 SQL 三值逻辑中的比较；NULL 比较结果为 UNKNOWN。

    HOW：DECIMAL 与 FLOAT 不互相转换。定点列参与比较的字面量在词法阶段就是
    Decimal，所以 DECIMAL 的等值/范围判断是精确的；而 FLOAT 列保留 IEEE-754
    语义（``0.07`` 仍是 0.070000000000000007），与 SQLite/duckdb 对 REAL 的行为一致。
    """

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


def sql_truth(value: SqlValue) -> bool:
    """WHERE/HAVING 只有 TRUE 才通过，FALSE/UNKNOWN 都过滤。"""

    return value is True

"""可组合的 Volcano 风格执行算子。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


RowT = TypeVar("RowT")
OutT = TypeVar("OutT")


class Executor(Generic[OutT]):
    """提供 open/next/close 与 Python 迭代协议。"""

    def open(self) -> None:
        return None

    def next(self) -> OutT | None:
        raise StopIteration

    def close(self) -> None:
        return None

    def __iter__(self) -> Iterator[OutT]:
        self.open()
        try:
            while True:
                item = self.next()
                if item is None:
                    return
                yield item
        finally:
            self.close()


class ValuesExecutor(Executor[RowT]):
    def __init__(self, rows: Iterable[RowT]) -> None:
        self._source = rows
        self._iterator: Iterator[RowT] | None = None

    def open(self) -> None:
        self._iterator = iter(self._source)

    def next(self) -> RowT | None:
        if self._iterator is None:
            self.open()
        try:
            return next(self._iterator)  # type: ignore[arg-type]
        except StopIteration:
            return None


class SeqScanExecutor(ValuesExecutor[RowT]):
    """顺序读取输入行，数据库层传入 TableHeap.scan。"""


class FilterExecutor(Executor[RowT]):
    def __init__(self, child: Executor[RowT], predicate: Callable[[RowT], bool]) -> None:
        self.child = child
        self.predicate = predicate

    def open(self) -> None:
        self.child.open()

    def next(self) -> RowT | None:
        while True:
            row = self.child.next()
            if row is None:
                return None
            if self.predicate(row):
                return row

    def close(self) -> None:
        self.child.close()


class ProjectExecutor(Executor[OutT]):
    def __init__(self, child: Executor[RowT], projection: Callable[[RowT], OutT]) -> None:
        self.child, self.projection = child, projection

    def open(self) -> None:
        self.child.open()

    def next(self) -> OutT | None:
        row = self.child.next()
        return None if row is None else self.projection(row)

    def close(self) -> None:
        self.child.close()


class SortExecutor(ValuesExecutor[RowT]):
    def __init__(self, child: Executor[RowT], key: Callable[[RowT], object], *, reverse: bool = False) -> None:
        self.child, self.key, self.reverse = child, key, reverse
        super().__init__(())

    def open(self) -> None:
        self._source = sorted(list(self.child), key=self.key, reverse=self.reverse)
        self._iterator = iter(self._source)


class LimitExecutor(Executor[RowT]):
    def __init__(self, child: Executor[RowT], limit: int | None, offset: int = 0) -> None:
        self.child, self.limit, self.offset = child, limit, offset
        self._seen = 0
        self._returned = 0

    def open(self) -> None:
        self.child.open()
        self._seen = self._returned = 0

    def next(self) -> RowT | None:
        while self._seen < self.offset:
            if self.child.next() is None:
                return None
            self._seen += 1
        if self.limit is not None and self._returned >= self.limit:
            return None
        row = self.child.next()
        if row is None:
            return None
        self._returned += 1
        return row

    def close(self) -> None:
        self.child.close()


class NestedLoopJoinExecutor(ValuesExecutor[tuple[RowT, OutT]]):
    def __init__(self, left: Executor[RowT], right: Executor[OutT], predicate: Callable[[RowT, OutT], bool]) -> None:
        self.left, self.right, self.predicate = left, right, predicate
        super().__init__(())

    def open(self) -> None:
        left_rows = list(self.left)
        right_rows = list(self.right)
        self._source = [(left, right) for left in left_rows for right in right_rows if self.predicate(left, right)]
        self._iterator = iter(self._source)


@dataclass
class AggregateExecutor(ValuesExecutor[OutT]):
    """把每组输入交给聚合函数，便于单元测试 Volcano 接口。"""

    child: Executor[RowT]
    aggregate: Callable[[list[RowT]], OutT]

    def __post_init__(self) -> None:
        ValuesExecutor.__init__(self, ())

    def open(self) -> None:
        self._source = [self.aggregate(list(self.child))]
        self._iterator = iter(self._source)


__all__ = [
    "AggregateExecutor",
    "Executor",
    "FilterExecutor",
    "LimitExecutor",
    "NestedLoopJoinExecutor",
    "ProjectExecutor",
    "SeqScanExecutor",
    "SortExecutor",
    "ValuesExecutor",
]

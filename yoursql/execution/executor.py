"""可组合的 Volcano 风格执行算子。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar


RowT = TypeVar("RowT")
OutT = TypeVar("OutT")


class Executor(Generic[OutT]):
    """提供 open/next/close 与 Python 迭代协议。"""

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        return None

    def next(self) -> OutT | None:
        """拉取下一个输出项；输入耗尽时返回 None。"""
        raise StopIteration

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        return None

    def __iter__(self) -> Iterator[OutT]:
        """返回对象的迭代器。"""
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
    """从可迭代输入逐行产出数据；也作为物化算子的公共基类。"""

    def __init__(self, rows: Iterable[RowT]) -> None:
        """初始化实例所需的状态和依赖。"""
        self._source = rows
        self._iterator: Iterator[RowT] | None = None

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        self._iterator = iter(self._source)

    def next(self) -> RowT | None:
        """拉取下一个输出项；输入耗尽时返回 None。"""
        iterator = self._iterator
        if iterator is None:
            self.open()
            iterator = self._iterator
        if iterator is None:
            return None
        try:
            return next(iterator)
        except StopIteration:
            return None


class SeqScanExecutor(ValuesExecutor[RowT]):
    """顺序读取输入行，数据库层传入 TableHeap.scan。"""


class FilterExecutor(Executor[RowT]):
    """只产出满足谓词的输入行。"""

    def __init__(
        self, child: Executor[RowT], predicate: Callable[[RowT], bool]
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.child = child
        self.predicate = predicate

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        self.child.open()

    def next(self) -> RowT | None:
        """拉取下一个输出项；输入耗尽时返回 None。"""
        while True:
            row = self.child.next()
            if row is None:
                return None
            if self.predicate(row):
                return row

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        self.child.close()


class ProjectExecutor(Executor[OutT]):
    """对每行应用投影函数，并产出新的行类型。"""

    def __init__(
        self, child: Executor[RowT], projection: Callable[[RowT], OutT]
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.child, self.projection = child, projection

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        self.child.open()

    def next(self) -> OutT | None:
        """拉取下一个输出项；输入耗尽时返回 None。"""
        row = self.child.next()
        return None if row is None else self.projection(row)

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        self.child.close()


class SortExecutor(ValuesExecutor[RowT]):
    """物化子节点后按键排序；适用于需要全量数据的排序阶段。"""

    def __init__(
        self,
        child: Executor[RowT],
        key: Callable[[RowT], object],
        *,
        reverse: bool = False,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.child, self.key, self.reverse = child, key, reverse
        super().__init__(())

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        self._source = sorted(list(self.child), key=self.key, reverse=self.reverse)
        self._iterator = iter(self._source)


class LimitExecutor(Executor[RowT]):
    """跳过 offset 后，最多从子节点返回 limit 行。"""

    def __init__(
        self, child: Executor[RowT], limit: int | None, offset: int = 0
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.child, self.limit, self.offset = child, limit, offset
        self._seen = 0
        self._returned = 0

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        self.child.open()
        self._seen = self._returned = 0

    def next(self) -> RowT | None:
        """拉取下一个输出项；输入耗尽时返回 None。"""
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
        """关闭资源并释放关联状态。"""
        self.child.close()


class NestedLoopJoinExecutor(ValuesExecutor[tuple[RowT, OutT]]):
    """物化两侧并枚举满足谓词的行对，作为基准连接实现。"""

    def __init__(
        self,
        left: Executor[RowT],
        right: Executor[OutT],
        predicate: Callable[[RowT, OutT], bool],
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.left, self.right, self.predicate = left, right, predicate
        super().__init__(())

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
        left_rows = list(self.left)
        right_rows = list(self.right)
        self._source = [
            (left, right)
            for left in left_rows
            for right in right_rows
            if self.predicate(left, right)
        ]
        self._iterator = iter(self._source)


@dataclass
class AggregateExecutor(ValuesExecutor[OutT]):
    """把每组输入交给聚合函数，便于单元测试 Volcano 接口。"""

    child: Executor[RowT]
    aggregate: Callable[[list[RowT]], OutT]

    def __post_init__(self) -> None:
        """完成数据类初始化后的派生状态设置。"""
        ValuesExecutor.__init__(self, ())

    def open(self) -> None:
        """打开资源并初始化迭代或访问状态。"""
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

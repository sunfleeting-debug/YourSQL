"""索引键规范化、比较和有序定位。"""

from __future__ import annotations

import math

from yoursql.common.types import RowId

Key = tuple[object, ...]
MemoryKey = tuple[tuple[int, object], ...]


def _key(value: object | tuple[object, ...]) -> Key:
    """把标量和复合键统一为 tuple。"""

    return value if isinstance(value, tuple) else (value,)


def _memory_key(value: Key) -> MemoryKey:
    """为内存模式生成带类型标签的字典键，区分 ``False`` 与 ``0``。"""

    return tuple(_value_order(item) for item in value)


def _value_order(value: object) -> tuple[int, object]:
    """为 SQL 支持的值提供稳定的总序，避免 NULL 和异构值比较崩溃。"""

    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        # 保留 Python int 的精度，不能先转 float，否则大于 2**53 的主键会折叠。
        return (2, (0, value))
    if isinstance(value, float):
        if math.isnan(value):
            return (2, (1, 0.0))
        return (2, (0, value))
    if isinstance(value, str):
        return (3, value)
    # SQL 行值来自 JSON，理论上只有上述类型；repr 让损坏/扩展值仍有确定顺序。
    return (4, repr(value))


def _compare_values(left: object, right: object) -> int:
    """比较两个键元素，返回 -1、0 或 1。"""

    left_order = _value_order(left)
    right_order = _value_order(right)
    if left_order[0] != right_order[0]:
        return -1 if left_order[0] < right_order[0] else 1
    try:
        if left_order[1] < right_order[1]:
            return -1
        if left_order[1] > right_order[1]:
            return 1
    except TypeError:
        left_text = repr(left_order[1])
        right_text = repr(right_order[1])
        if left_text < right_text:
            return -1
        if left_text > right_text:
            return 1
    return 0


def _compare_keys(left: Key, right: Key) -> int:
    """按字典序比较复合索引键。"""

    for left_value, right_value in zip(left, right, strict=False):
        result = _compare_values(left_value, right_value)
        if result:
            return result
    if len(left) < len(right):
        return -1
    if len(left) > len(right):
        return 1
    return 0


def _compare_entries(
    left_key: Key, left_row: RowId, right_key: Key, right_row: RowId
) -> int:
    """比较两个索引条目的键及其排序位置。"""
    result = _compare_keys(left_key, right_key)
    if result:
        return result
    left_tuple = left_row.as_tuple()
    right_tuple = right_row.as_tuple()
    if left_tuple < right_tuple:
        return -1
    if left_tuple > right_tuple:
        return 1
    return 0


def _lower_bound(keys: list[Key], target: Key) -> int:
    """在键数组中二分寻找第一个大于等于 target 的位置。"""

    low, high = 0, len(keys)
    while low < high:
        middle = (low + high) // 2
        if _compare_keys(keys[middle], target) < 0:
            low = middle + 1
        else:
            high = middle
    return low

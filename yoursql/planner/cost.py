"""查询计划代价模型的公共值对象和校准常数。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostEstimate:
    """计划的相对启动代价、总代价和估计行数。"""

    startup_cost: float
    total_cost: float
    rows: int


# HOW：这些常数只用于访问路径之间的相对比较，不代表真实毫秒数。
SEQ_PAGE_COST = 30e-6
DECODE_ROW_COST = 4.2e-6
INDEX_ENTRY_COST = 8.6e-6
RANDOM_PAGE_COST = 94e-6
INDEX_ONLY_ENTRY_COST = 4.6e-6

__all__ = [
    "CostEstimate",
    "DECODE_ROW_COST",
    "INDEX_ENTRY_COST",
    "INDEX_ONLY_ENTRY_COST",
    "RANDOM_PAGE_COST",
    "SEQ_PAGE_COST",
]

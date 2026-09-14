"""【前端特供】工作台性能观测：聚合查询耗时并保留慢查询诊断样本。"""

from __future__ import annotations

import json
from collections import deque
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Iterable

from yoursql.common import JsonObject, JsonValue, YourSQLError


class PerformanceMonitor:
    """【前端特供】聚合看板查询观测，并保留慢查询诊断样本。"""

    def __init__(
        self,
        *,
        slow_threshold_ms: float = 500,
        log_dir: str | Path = "logs",
        max_records: int = 1000,
    ) -> None:
        """初始化性能采样器。"""
        if slow_threshold_ms <= 0:
            raise ValueError("slow_threshold_ms 必须为正数")
        if max_records < 1:
            raise ValueError("max_records 必须为正数")
        self.slow_threshold_ms = float(slow_threshold_ms)
        self.log_dir = Path(log_dir)
        self.max_records = max_records
        self._records: deque[JsonObject] = deque(maxlen=max_records)
        self._log_failures = 0
        self._lock = RLock()

    def record(self, observation: JsonObject) -> JsonObject:
        """【前端特供】记录一条查询观测；日志写入失败不影响查询结果。"""
        record = _copy_json_object(observation)
        total_ms = _number(record.get("total_ms"))
        record["total_ms"] = total_ms
        record["slow"] = total_ms >= self.slow_threshold_ms
        with self._lock:
            self._records.append(record)
            if record["slow"]:
                self._append_slow_log(record)
        return _copy_json_object(record)

    def summary(self) -> JsonObject:
        """【前端特供】返回看板所需的延迟、成功率和存储累计摘要。"""
        with self._lock:
            records = list(self._records)
        durations = [_number(record.get("total_ms")) for record in records]
        successful = sum(record.get("status") == "success" for record in records)
        failed = len(records) - successful
        slow = sum(bool(record.get("slow")) for record in records)
        cache_hits = sum(_integer(record.get("cache_hits")) for record in records)
        cache_misses = sum(_integer(record.get("cache_misses")) for record in records)
        chronological = list(reversed(records))
        return {
            "threshold_ms": self.slow_threshold_ms,
            "sampled_queries": len(records),
            "successful_queries": successful,
            "failed_queries": failed,
            "slow_queries": slow,
            "avg_ms": _average(durations),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "max_ms": max(durations, default=0.0),
            "cache_hit_rate": cache_hits / (cache_hits + cache_misses)
            if cache_hits + cache_misses
            else 0.0,
            "page_reads": sum(_integer(record.get("page_reads")) for record in records),
            "page_writes": sum(
                _integer(record.get("page_writes")) for record in records
            ),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_evictions": sum(
                _integer(record.get("cache_evictions")) for record in records
            ),
            "latency_series": [
                {
                    "at": record.get("finished_at", ""),
                    "total_ms": _number(record.get("total_ms")),
                    "slow": bool(record.get("slow")),
                    "status": record.get("status", "error"),
                }
                for record in chronological[-30:]
            ],
            "retention": f"最近 {self.max_records} 条查询观测；重启清空，慢查询另存为按日期 JSONL。",
            "log_failures": self._log_failures,
        }

    def queries(self, *, slow_only: bool = False, limit: int = 100) -> JsonObject:
        """【前端特供】返回最近查询摘要，慢查询默认按耗时从高到低排列。"""
        if not 1 <= limit <= 500:
            raise YourSQLError("查询监控 limit 应在 1–500 范围内", "BAD_REQUEST")
        with self._lock:
            records = [
                {
                    key: value
                    for key, value in _copy_json_object(record).items()
                    if key not in {"stages", "plan"}
                }
                for record in self._records
                if not slow_only or record.get("slow") is True
            ]
        records.sort(key=lambda record: _number(record.get("total_ms")), reverse=True)
        return {
            "items": records[:limit],
            "total": len(records),
            "slow_only": slow_only,
            "threshold_ms": self.slow_threshold_ms,
        }

    def detail(self, query_id: str) -> JsonObject:
        """【前端特供】返回单条观测详情，供看板展示阶段和访问路径。"""
        with self._lock:
            for record in self._records:
                if record.get("query_id") == query_id:
                    return _copy_json_object(record)
        raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")

    def _append_slow_log(self, record: JsonObject) -> None:
        """追加慢查询摘要；磁盘异常只计数，避免观测模块影响主链路。"""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"performance-{date.today().isoformat()}.jsonl"
            summary = {
                key: value
                for key, value in record.items()
                if key not in {"stages", "plan"}
            }
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(summary, ensure_ascii=False) + "\n")
        except OSError:
            self._log_failures += 1


def _copy_json_object(value: JsonObject) -> JsonObject:
    """复制 JSON 对象，防止调用方修改监控历史。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _number(value: JsonValue | None) -> float:
    """读取可用于统计的数字值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _integer(value: JsonValue | None) -> int:
    """读取可用于计数的整数值。"""
    return int(_number(value))


def _average(values: Iterable[float]) -> float:
    """返回均值，空样本返回零。"""
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Iterable[float], quantile: float) -> float:
    """使用线性插值估算百分位延迟。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


__all__ = ["PerformanceMonitor"]

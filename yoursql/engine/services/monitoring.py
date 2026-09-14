"""【前端特供】工作台性能观测：聚合查询耗时并保留慢查询诊断样本。"""

from __future__ import annotations

import json
from collections import deque
from datetime import date
from hashlib import blake2b
from pathlib import Path
from queue import Full, Queue
from random import random
from threading import RLock, Thread
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
        diagnostic_sample_rate: float = 0.01,
    ) -> None:
        """初始化性能采样器。"""
        if slow_threshold_ms <= 0:
            raise ValueError("slow_threshold_ms 必须为正数")
        if max_records < 1:
            raise ValueError("max_records 必须为正数")
        if not 0 <= diagnostic_sample_rate <= 1:
            raise ValueError("diagnostic_sample_rate 必须在 0–1 范围内")
        self.slow_threshold_ms = float(slow_threshold_ms)
        self.log_dir = Path(log_dir)
        self.max_records = max_records
        self.diagnostic_sample_rate = float(diagnostic_sample_rate)
        self._records: deque[JsonObject] = deque(maxlen=max_records)
        self._baselines: dict[str, tuple[int, float]] = {}
        self._log_failures = 0
        self._log_dropped = 0
        self._log_queue: Queue[JsonObject | None] = Queue(maxsize=256)
        self._closed = False
        self._lock = RLock()
        self._log_thread = Thread(
            target=self._log_worker,
            name="yoursql-performance-log",
            daemon=True,
        )
        self._log_thread.start()

    def record(self, observation: JsonObject) -> JsonObject:
        """【前端特供】记录轻量查询观测，必要时保留完整诊断样本。"""
        total_ms = _number(observation.get("total_ms"))
        execute_ms = _number(observation.get("execute_ms"))
        queue_wait_ms = _number(observation.get("queue_wait_ms"))
        fingerprint = _fingerprint(observation.get("sql"))
        baseline_count, baseline_ms, latency_regression = self._update_baseline(
            fingerprint, total_ms
        )
        resource_signals = _resource_signals(observation)
        if queue_wait_ms >= self.slow_threshold_ms:
            resource_signals.append("queue")
        if latency_regression:
            resource_signals.append("latency_regression")
        slow = total_ms >= self.slow_threshold_ms
        diagnostic = (
            slow
            or bool(resource_signals)
            or random() < self.diagnostic_sample_rate
        )
        record = (
            _copy_json_object(observation)
            if diagnostic
            else _copy_lightweight_record(observation)
        )
        record["total_ms"] = total_ms
        record["slow"] = slow
        record["execution_slow"] = execute_ms >= self.slow_threshold_ms
        record["queue_slow"] = queue_wait_ms >= self.slow_threshold_ms
        record["fingerprint"] = fingerprint
        record["baseline_ms"] = baseline_ms if baseline_count else None
        record["latency_regression"] = latency_regression
        record["diagnostic_available"] = diagnostic
        record["resource_signals"] = resource_signals
        with self._lock:
            self._records.append(record)
        if slow:
            self._enqueue_slow_log(record)
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
        chronological = records
        return {
            "threshold_ms": self.slow_threshold_ms,
            "diagnostic_sample_rate": self.diagnostic_sample_rate,
            "sampled_queries": len(records),
            "successful_queries": successful,
            "failed_queries": failed,
            "slow_queries": slow,
            "execution_slow_queries": sum(
                bool(record.get("execution_slow")) for record in records
            ),
            "queue_slow_queries": sum(
                bool(record.get("queue_slow")) for record in records
            ),
            "latency_regressions": sum(
                bool(record.get("latency_regression")) for record in records
            ),
            "resource_warnings": sum(
                bool(record.get("resource_signals")) for record in records
            ),
            "diagnostic_samples": sum(
                bool(record.get("diagnostic_available")) for record in records
            ),
            "lightweight_samples": sum(
                not bool(record.get("diagnostic_available")) for record in records
            ),
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
                    "page_reads": _integer(record.get("page_reads")),
                    "page_writes": _integer(record.get("page_writes")),
                    "cache_hits": _integer(record.get("cache_hits")),
                    "cache_misses": _integer(record.get("cache_misses")),
                }
                for record in chronological[-30:]
            ],
            "retention": f"最近 {self.max_records} 条查询观测；普通查询保留轻量指标，诊断样本按比例保留；重启清空。",
            "log_failures": self._log_failures,
            "log_dropped": self._log_dropped,
        }

    def queries(
        self,
        *,
        slow_only: bool = False,
        attention_only: bool = False,
        limit: int = 100,
    ) -> JsonObject:
        """【前端特供】返回需关注的查询摘要，按耗时从高到低排列。"""
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
                if (not slow_only or record.get("slow") is True) and (
                    not attention_only
                    or record.get("slow") is True
                    or bool(record.get("resource_signals"))
                )
            ]
        records.sort(key=lambda record: _number(record.get("total_ms")), reverse=True)
        return {
            "items": records[:limit],
            "total": len(records),
            "slow_only": slow_only,
            "attention_only": attention_only,
            "threshold_ms": self.slow_threshold_ms,
        }

    def detail(self, query_id: str) -> JsonObject:
        """【前端特供】返回单条观测详情，供看板展示阶段和访问路径。"""
        with self._lock:
            for record in self._records:
                if record.get("query_id") == query_id:
                    return _copy_json_object(record)
        raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")

    def statistics(self, query_id: str, *, user: str | None = None) -> JsonObject:
        """【前端特供】返回单条查询的轻量执行统计，不包含历史和诊断产物。"""
        with self._lock:
            for record in self._records:
                if record.get("query_id") != query_id:
                    continue
                if user is not None and record.get("user") != user:
                    continue
                return _copy_lightweight_record(record)
        raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")

    def history(
        self, query_id: str, *, limit: int = 30, user: str | None = None
    ) -> JsonObject:
        """【前端特供】返回同一 SQL 指纹的历史执行轻量序列。"""
        if not 1 <= limit <= 100:
            raise YourSQLError("查询监控 history limit 应在 1–100 范围内", "BAD_REQUEST")
        with self._lock:
            current = next(
                (
                    record
                    for record in self._records
                    if record.get("query_id") == query_id
                    and (user is None or record.get("user") == user)
                ),
                None,
            )
            if current is None:
                raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")
            fingerprint = current.get("fingerprint")
            records = [
                _copy_lightweight_record(record)
                for record in self._records
                if record.get("fingerprint") == fingerprint
                and (user is None or record.get("user") == user)
            ]
        records.sort(key=lambda record: str(record.get("finished_at", "")))
        return {
            "current": _copy_lightweight_record(current),
            "items": records[-limit:],
            "total": len(records),
            "fingerprint": fingerprint,
        }

    def _update_baseline(
        self, fingerprint: str, total_ms: float
    ) -> tuple[int, float, bool]:
        """用常数开销更新查询指纹的 EWMA 基线并识别延迟回归。"""
        with self._lock:
            if fingerprint not in self._baselines and len(self._baselines) >= self.max_records:
                self._baselines.pop(next(iter(self._baselines)))
            count, baseline = self._baselines.get(fingerprint, (0, 0.0))
            regression = count >= 5 and total_ms >= max(
                250.0, baseline * 2, baseline + 50.0
            )
            next_baseline = (
                total_ms if count == 0 else baseline * 0.8 + total_ms * 0.2
            )
            self._baselines[fingerprint] = (count + 1, next_baseline)
            return count, baseline, regression

    def _enqueue_slow_log(self, record: JsonObject) -> None:
        """将慢查询摘要放入有界队列，不在查询线程同步写磁盘。"""
        with self._lock:
            if self._closed:
                return
            try:
                self._log_queue.put_nowait(_copy_lightweight_record(record))
            except Full:
                self._log_dropped += 1

    def _log_worker(self) -> None:
        """后台批量消费慢查询日志队列。"""
        while True:
            record = self._log_queue.get()
            try:
                if record is None:
                    return
                self._append_slow_log(record)
            except Exception:
                # WHY：观测线程不能因为异常退出，否则后续慢查询将静默丢失。
                with self._lock:
                    self._log_failures += 1
            finally:
                self._log_queue.task_done()

    def _append_slow_log(self, record: JsonObject) -> None:
        """追加慢查询摘要；该方法只由后台日志线程调用。"""
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
            with self._lock:
                self._log_failures += 1

    def flush(self) -> None:
        """等待已排队的慢查询摘要写入磁盘。"""
        self._log_queue.join()

    def close(self) -> None:
        """关闭后台日志线程，并等待已排队日志完成。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._log_queue.put(None)
        self.flush()
        self._log_thread.join(timeout=2)


_LIGHTWEIGHT_KEYS = {
    "query_id",
    "task_id",
    "statement_index",
    "user",
    "sql",
    "status",
    "started_at",
    "finished_at",
    "total_ms",
    "queue_wait_ms",
    "compile_ms",
    "execute_ms",
    "materialize_ms",
    "rows_examined",
    "rows_returned",
    "affected_rows",
    "page_reads",
    "page_writes",
    "cache_hits",
    "cache_misses",
    "cache_evictions",
    "operator",
    "error_code",
    "plan_estimate",
    "fingerprint",
    "baseline_ms",
    "execution_slow",
    "queue_slow",
    "latency_regression",
    "slow",
    "diagnostic_available",
    "resource_signals",
}


def _copy_lightweight_record(value: JsonObject) -> JsonObject:
    """只复制看板必需字段，避免普通查询深拷贝 Trace 和执行计划。"""
    return _copy_json_object(
        {key: item for key, item in value.items() if key in _LIGHTWEIGHT_KEYS}
    )


def _resource_signals(value: JsonObject) -> list[str]:
    """根据低成本计数识别需要保留诊断信息的查询。"""
    signals: list[str] = []
    if _integer(value.get("page_reads")) >= 64 or _integer(value.get("cache_misses")) >= 64:
        signals.append("io")
    if _integer(value.get("cache_evictions")) > 0:
        signals.append("cache_eviction")
    examined = _integer(value.get("rows_examined"))
    returned = _integer(value.get("rows_returned"))
    if examined >= 10_000 and returned * 100 < examined:
        signals.append("scan_amplification")
    return signals


def _fingerprint(value: JsonValue | None) -> str:
    """为已脱敏 SQL 生成短指纹，供延迟基线分组。"""
    sql = value if isinstance(value, str) else ""
    return blake2b(sql.encode("utf-8"), digest_size=8).hexdigest()


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

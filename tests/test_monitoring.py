"""查询性能监控和缓存诊断的回归测试。"""

import time
from pathlib import Path

from yoursql.common.config import RuntimeConfig
from yoursql.engine.runtime.database import Database
from yoursql.engine.services.monitoring import PerformanceMonitor
from yoursql.engine.services.workbench import Workbench


def test_performance_monitor_aggregates_and_persists_slow_queries(
    tmp_path: Path,
) -> None:
    """慢查询应进入摘要、列表和按日期 JSONL。"""
    monitor = PerformanceMonitor(
        slow_threshold_ms=100,
        log_dir=tmp_path,
        diagnostic_sample_rate=0,
    )
    try:
        monitor.record(
            {
                "query_id": "fast",
                "status": "success",
                "finished_at": "2026-09-14T00:00:00+00:00",
                "total_ms": 20,
                "cache_hits": 3,
                "cache_misses": 1,
                "stages": [],
                "plan": None,
            }
        )
        monitor.record(
            {
                "query_id": "slow",
                "status": "success",
                "finished_at": "2026-09-14T00:00:01+00:00",
                "total_ms": 220,
                "cache_hits": 1,
                "cache_misses": 3,
                "stages": [{"name": "execution"}],
                "plan": {"operator": "SeqScan"},
            }
        )
        monitor.flush()

        summary = monitor.summary()
        slow_queries = monitor.queries(slow_only=True, limit=10)

        assert summary["sampled_queries"] == 2
        assert summary["slow_queries"] == 1
        assert summary["p95_ms"] == 210
        assert summary["cache_hit_rate"] == 0.5
        assert [item["query_id"] for item in slow_queries["items"]] == ["slow"]
        assert "stages" not in slow_queries["items"][0]
        assert list(tmp_path.glob("performance-*.jsonl"))
        assert monitor.detail("slow")["plan"] == {"operator": "SeqScan"}
        history = monitor.history("slow")
        assert history["current"]["query_id"] == "slow"
        assert [item["query_id"] for item in history["items"]] == ["fast", "slow"]
    finally:
        monitor.close()


def test_performance_monitor_strips_normal_query_diagnostics() -> None:
    """普通查询只保留看板字段，资源信号仍应升级为诊断样本。"""
    monitor = PerformanceMonitor(diagnostic_sample_rate=0)
    try:
        fast = monitor.record(
            {
                "query_id": "fast",
                "status": "success",
                "total_ms": 20,
                "rows_examined": 10,
                "rows_returned": 10,
                "stages": [{"name": "tokens", "data": ["large"]}],
                "plan": {"operator": "SeqScan"},
            }
        )
        resource_heavy = monitor.record(
            {
                "query_id": "resource-heavy",
                "status": "success",
                "total_ms": 20,
                "rows_examined": 10_000,
                "rows_returned": 1,
                "page_reads": 64,
                "stages": [{"name": "execution"}],
                "plan": {"operator": "SeqScan"},
            }
        )

        assert fast["diagnostic_available"] is False
        assert "stages" not in monitor.detail("fast")
        assert resource_heavy["diagnostic_available"] is True
        assert resource_heavy["resource_signals"] == ["io", "scan_amplification"]
        assert monitor.detail("resource-heavy")["plan"] == {"operator": "SeqScan"}
        attention = monitor.queries(attention_only=True, limit=10)
        assert attention["attention_only"] is True
        assert [item["query_id"] for item in attention["items"]] == [
            "resource-heavy"
        ]
    finally:
        monitor.close()


def test_performance_monitor_history_groups_same_sql_only() -> None:
    """历史统计只应包含同一 SQL 指纹的轻量执行记录。"""
    monitor = PerformanceMonitor(diagnostic_sample_rate=0)
    try:
        for query_id, sql in (
            ("first", "SELECT id FROM orders WHERE id = ?"),
            ("other", "SELECT id FROM customers WHERE id = ?"),
            ("second", "SELECT id FROM orders WHERE id = ?"),
        ):
            monitor.record(
                {
                    "query_id": query_id,
                    "sql": sql,
                    "status": "success",
                    "finished_at": f"2026-09-14T00:00:0{len(query_id)}+00:00",
                    "total_ms": 20,
                    "stages": [{"name": "execution"}],
                    "plan": {"operator": "SeqScan"},
                }
            )

        history = monitor.history("second")

        assert [item["query_id"] for item in history["items"]] == ["first", "second"]
        assert all("stages" not in item and "plan" not in item for item in history["items"])
    finally:
        monitor.close()


def test_performance_monitor_detects_latency_regression_by_fingerprint() -> None:
    """同一 SQL 指纹明显偏离历史基线时，应升级为诊断样本。"""
    monitor = PerformanceMonitor(slow_threshold_ms=500, diagnostic_sample_rate=0)
    try:
        for _ in range(5):
            monitor.record(
                {
                    "query_id": "baseline",
                    "sql": "SELECT * FROM orders WHERE id = ?",
                    "status": "success",
                    "total_ms": 100,
                }
            )
        regression = monitor.record(
            {
                "query_id": "regression",
                "sql": "SELECT * FROM orders WHERE id = ?",
                "status": "success",
                "total_ms": 260,
            }
        )

        assert regression["slow"] is False
        assert regression["latency_regression"] is True
        assert regression["diagnostic_available"] is True
        assert regression["resource_signals"] == ["latency_regression"]
        assert monitor.summary()["latency_regressions"] == 1
    finally:
        monitor.close()


def test_workbench_records_query_and_exposes_monitoring_detail(tmp_path: Path) -> None:
    """真实查询完成后应能从 Workbench 监控接口取回阶段明细。"""
    database = Database(tmp_path / "monitor.db")
    workbench = Workbench(
        database,
        settings=RuntimeConfig(slow_query_ms=500, monitor_diagnostic_sample_rate=1),
    )
    try:
        _, session = workbench.login("admin", "admin")
        task = workbench.submit(session, "SELECT 1", 15, 100)
        deadline = time.monotonic() + 5
        while task.state in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)

        assert task.state == "success"
        summary = workbench.monitoring_summary(session)
        detail = workbench.monitoring_detail(session, f"{task.id}:0")

        assert summary["sampled_queries"] == 1
        assert detail["task_id"] == task.id
        assert detail["status"] == "success"
        assert detail["stages"]
    finally:
        workbench.close()
        database.close()

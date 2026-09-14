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
    monitor = PerformanceMonitor(slow_threshold_ms=100, log_dir=tmp_path)
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


def test_workbench_records_query_and_exposes_monitoring_detail(tmp_path: Path) -> None:
    """真实查询完成后应能从 Workbench 监控接口取回阶段明细。"""
    database = Database(tmp_path / "monitor.db")
    workbench = Workbench(database, settings=RuntimeConfig(slow_query_ms=500))
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

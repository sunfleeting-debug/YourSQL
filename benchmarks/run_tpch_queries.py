"""TPC-H SF0.01 多查询基准：装载全部 8 张表，跑当前引擎可编译的查询子集。

HOW：与单条 Q6 基准同一口径（同一份 .tbl、预热 1 次、静置后计时），
报告写入 benchmarks/reports/tpch_sf001_queries.json，便于横向比较不同查询。
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from benchbox import TPCH

from benchmarks.run_benchbox_tpch import (
    BENCHMARK_DATABASE_CONFIG,
    ROOT,
    SCALE_FACTOR,
    _base_type,
    _read_rows,
    _remove_database,
    _typed_value,
)
from yoursql.common import DatabaseConfig, YourSQLError
from yoursql.engine.runtime.database import Database

DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_DB_PATH = ROOT / "benchmarks" / "results" / "tpch_sf001_multi.db"
DEFAULT_REPORT_PATH = ROOT / "benchmarks" / "reports" / "tpch_sf001_queries.json"
# 当前引擎可编译的查询子集（见 README「TPC-H 覆盖率」）。
SUPPORTED_QUERIES = (1, 3, 5, 6, 10, 16, 18, 19)


def yoursql_type(type_name: str) -> str:
    base = _base_type(type_name)
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INT"
    if base in {"DECIMAL", "NUMERIC", "REAL", "DOUBLE", "FLOAT"}:
        return "FLOAT"
    return "VARCHAR"


def load_table(database: Database, benchmark: TPCH, table: str, data_dir: Path) -> int:
    """按 TPC-H schema 建表并导入 <table>.tbl；返回行数。"""

    definition = benchmark.get_schema()[table]
    column_types = [str(column["type"]) for column in definition["columns"]]
    columns = ", ".join(f'{column["name"]} {yoursql_type(type_name)}' for column, type_name in zip(definition["columns"], column_types, strict=True))
    database.execute(f"CREATE TABLE {table} ({columns});")
    rows: Iterable[tuple[object, ...]] = (
        tuple(_typed_value(value, type_name) for value, type_name in zip(row, column_types, strict=True))
        for row in _read_rows(data_dir / f"{table}.tbl", column_types)
    )
    return database.insert_rows(table, rows).affected_rows


def run(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = Path(args.data_dir).resolve()
    db_path = Path(args.database).resolve()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.force:
        _remove_database(db_path)

    benchmark = TPCH(scale_factor=SCALE_FACTOR, output_dir=data_dir)
    if not (data_dir / "lineitem.tbl").exists():
        benchmark.generate_data()

    load_started = time.perf_counter()
    loaded: dict[str, int] = {}
    with Database(db_path, config=BENCHMARK_DATABASE_CONFIG) as database:
        for table in ("region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem"):
            loaded[table] = load_table(database, benchmark, table, data_dir)
    load_seconds = time.perf_counter() - load_started
    # WHY：子进程需要独占数据库文件，先把父进程的连接关掉。
    if args.settle_seconds > 0:
        time.sleep(args.settle_seconds)

    measurements: list[dict[str, Any]] = []
    for query_id in SUPPORTED_QUERIES:
        command = [
            sys.executable,
            "-m",
            "benchmarks.run_tpch_query",
            "--database",
            str(db_path),
            "--query",
            str(query_id),
            "--iterations",
            str(args.iterations),
            "--data-dir",
            str(data_dir),
        ]
        try:
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=args.query_timeout
            )
        except subprocess.TimeoutExpired:
            measurements.append(
                {
                    "query": f"Q{query_id}",
                    "status": "TIMEOUT",
                    "detail": f"超过 {args.query_timeout}s 未完成（当前为嵌套循环连接 + 全量物化）",
                }
            )
            continue
        if completed.returncode != 0:
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            measurements.append(
                {
                    "query": f"Q{query_id}",
                    "status": f"FAILED(exit={completed.returncode})",
                    "detail": tail[0][:160],
                }
            )
            continue
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        measurements.append(payload)

    report = {
        "status": "PASS",
        "benchmark": "TPC-H",
        "scale_factor": SCALE_FACTOR,
        "queries": [f"Q{query_id}" for query_id in SUPPORTED_QUERIES],
        "coverage_note": "当前引擎可编译 8/22 条 TPC-H 查询；其余因缺少派生表/CASE WHEN/子查询表达式/CTE 无法解析。多表连接查询在 SF0.01 上会超时或 OOM（嵌套循环 + 全量物化）。",
        "loaded_rows": loaded,
        "load_seconds": load_seconds,
        "storage_config": {
            "page_size": BENCHMARK_DATABASE_CONFIG.page_size,
            "buffer_pool_size": BENCHMARK_DATABASE_CONFIG.buffer_pool_size,
        },
        "measurements": measurements,
        "environment": {"python": sys.version, "platform": platform.platform()},
        "scope_note": "这是 TPC-H SF0.01 子集实验，不宣称官方 QphH@Size 成绩。",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"装载 {sum(loaded.values())} 行用 {load_seconds:.1f}s · 查询 {len(SUPPORTED_QUERIES)} 条")
    print(f"{'查询':<6}{'状态':<16}{'行数':>8}{'首次 s':>10}{'平均 s':>10}{'中位 s':>10}{'算子':>16}")
    for item in measurements:
        if item["status"] != "OK":
            print(f"{item['query']:<6}{item['status']:<16}{item.get('detail', '')[:70]}")
            continue
        print(
            f"{item['query']:<6}{'OK':<16}{item['rows']:>8}{item['first_seconds']:>9.3f}s"
            f"{item['mean_seconds']:>9.4f}s{item['median_seconds']:>9.4f}s{str(item['operator']):>16}"
        )
    print(f"报告写入: {report_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    parser.add_argument("--query-timeout", type=float, default=120.0, help="单条查询超时（秒），超时记为 TIMEOUT")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()

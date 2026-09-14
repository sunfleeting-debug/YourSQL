"""TPC-H SF0.01 最终对标基准：YourSQL / SQLite / DuckDB 在同一份数据、同一批查询上比较。

HOW：
- YourSQL 用子进程逐条执行（超时保护，执行失败/OOM 时如实记录）；
- SQLite 直接用它自己方言的同一批 SQL（BenchBox 生成的就是 sqlite 方言）；
- DuckDB 需把 `DATE('...', '±N unit')` 这类 SQLite 语法改写为 DuckDB 等价形式；改写失败记为不可比。

NOTE：Q6 三引擎样本值存在已知口径差异（不是执行错误）：`0.06 + 0.01` 在 SQLite/YourSQL 里是
二进制浮点（0.06999999999999999），会丢掉 l_discount = 0.07 的行；DuckDB 用精确 DECIMAL 得到 0.07。
按 TPC-H 的 DECIMAL 语义 DuckDB 才对；YourSQL 与 SQLite 逐值一致。详见报告 value_notes。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import duckdb
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
from yoursql.engine.runtime.database import Database

DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_REPORT_PATH = ROOT / "benchmarks" / "reports" / "tpch_sf001_compare.json"
TABLES = ("region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem")
QUERIES = (1, 3, 5, 6, 10, 16, 18, 19)

# SQLite 日期修饰符 → DuckDB 的 interval 写法（只覆盖本批查询用到的形式，含正负号）。
_DATE_MODIFIER = re.compile(r"DATE\('(\d{4}-\d{2}-\d{2})',\s*'([+-])(\d+)\s+(day|month|year)'\)")


def _sqlite_type(type_name: str) -> str:
    """将 TPC-H 类型转换为 SQLite 类型。"""
    base = _base_type(type_name)
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INTEGER"
    if base in {"DECIMAL", "NUMERIC", "REAL", "DOUBLE", "FLOAT"}:
        return "REAL"
    return "TEXT"


def _yoursql_type(type_name: str) -> str:
    """将 TPC-H 类型转换为 YourSQL 类型。"""
    base = _base_type(type_name)
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INT"
    if base in {"DECIMAL", "NUMERIC", "REAL", "DOUBLE", "FLOAT"}:
        return "FLOAT"
    return "VARCHAR"


def to_duckdb(sql: str) -> str:
    """把 BenchBox 的 sqlite 方言改写为 DuckDB 可执行形式。"""

    def replace(match: re.Match[str]) -> str:
        """替换 SQL 模板中的占位符。"""
        base, sign, amount, unit = match.groups()
        operator = "+" if sign == "+" else "-"
        return f"(DATE '{base}' {operator} INTERVAL {amount} {unit.upper()})"

    return _DATE_MODIFIER.sub(replace, sql)


def load_yoursql(benchmark: TPCH, data_dir: Path, db_path: Path) -> dict[str, int]:
    """创建 YourSQL 表并导入 TPC-H 数据。"""
    loaded: dict[str, int] = {}
    with Database(db_path, config=BENCHMARK_DATABASE_CONFIG) as database:
        for table in TABLES:
            definition = benchmark.get_schema()[table]
            column_types = [str(column["type"]) for column in definition["columns"]]
            columns = ", ".join(
                f'{column["name"]} {_yoursql_type(type_name)}'
                for column, type_name in zip(definition["columns"], column_types, strict=True)
            )
            database.execute(f"CREATE TABLE {table} ({columns});")
            rows = (
                tuple(_typed_value(value, type_name) for value, type_name in zip(row, column_types, strict=True))
                for row in _read_rows(data_dir / f"{table}.tbl", column_types)
            )
            loaded[table] = database.insert_rows(table, rows).affected_rows
    return loaded


def load_sqlite(benchmark: TPCH, data_dir: Path, db_path: Path) -> dict[str, int]:
    """创建 SQLite 表并导入 TPC-H 数据。"""
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(str(db_path))
    loaded: dict[str, int] = {}
    try:
        for table in TABLES:
            definition = benchmark.get_schema()[table]
            column_types = [str(column["type"]) for column in definition["columns"]]
            columns = ", ".join(
                f'"{column["name"]}" {_sqlite_type(type_name)}'
                for column, type_name in zip(definition["columns"], column_types, strict=True)
            )
            connection.execute(f"CREATE TABLE {table} ({columns})")
            payload = [
                tuple(_typed_value(value, type_name) for value, type_name in zip(row, column_types, strict=True))
                for row in _read_rows(data_dir / f"{table}.tbl", column_types)
            ]
            placeholders = ", ".join("?" for _ in definition["columns"])
            connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', payload)
            loaded[table] = len(payload)
        connection.commit()
    finally:
        connection.close()
    return loaded


def load_duckdb(benchmark: TPCH, data_dir: Path, db_path: Path) -> dict[str, int]:
    """创建 DuckDB 表并导入 TPC-H 数据。"""
    db_path.unlink(missing_ok=True)
    connection = duckdb.connect(str(db_path))
    loaded: dict[str, int] = {}
    try:
        for table in TABLES:
            definition = benchmark.get_schema()[table]
            columns = ", ".join(f"'{column['name']}': '{column['type']}'" for column in definition["columns"])
            connection.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv('{(data_dir / f'{table}.tbl').as_posix()}', "
                f"delim='|', header=false, columns={{{columns}}})"
            )
            loaded[table] = int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()
    return loaded


def best_of(iterations: int, action) -> tuple[list[float], Any]:
    """多次执行基准函数并返回最快耗时。"""
    times: list[float] = []
    result = None
    for _ in range(iterations):
        started = time.perf_counter()
        result = action()
        times.append(time.perf_counter() - started)
    return times, result


def run_yoursql_query(db_path: Path, data_dir: Path, query_id: int, iterations: int, timeout: float) -> dict[str, Any]:
    """执行一条 YourSQL TPC-H 查询并记录耗时。"""
    command = [
        sys.executable, "-m", "benchmarks.run_tpch_query",
        "--database", str(db_path), "--query", str(query_id),
        "--iterations", str(iterations), "--data-dir", str(data_dir),
    ]
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "detail": f">{timeout:.0f}s"}
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()[-1:]
        return {"status": f"FAILED(exit={completed.returncode})", "detail": (tail[0] if tail else "")[:120]}
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _normalized_sample(engine: dict[str, Any]) -> list[list[str]]:
    """样本值归一化（数值转 float 后保留 6 位）后再比较。"""

    rows = engine.get("sample") or []
    normalized: list[list[str]] = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            try:
                cells.append(f"{float(cell):.6f}")
            except (TypeError, ValueError):
                cells.append(str(cell))
        normalized.append(cells)
    return normalized


def _sample_match(entry: dict[str, Any]) -> dict[str, bool]:
    """以 SQLite 为基准，标记 YourSQL / DuckDB 样本值是否一致（行数不同即视为不一致）。"""

    baseline = entry.get("sqlite", {})
    baseline_rows = baseline.get("rows")
    baseline_sample = _normalized_sample(baseline)
    match: dict[str, bool] = {}
    for engine in ("yoursql", "duckdb"):
        candidate = entry.get(engine, {})
        if candidate.get("status") != "OK" or baseline.get("status") != "OK":
            continue
        same = candidate.get("rows") == baseline_rows
        same = same and _normalized_sample(candidate)[: len(baseline_sample)] == baseline_sample
        match[engine] = same
    return match


def main() -> None:
    """解析命令行参数并启动当前脚本任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--query-timeout", type=float, default=120.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    results_dir = ROOT / "benchmarks" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    benchmark = TPCH(scale_factor=SCALE_FACTOR, output_dir=data_dir)
    if not (data_dir / "lineitem.tbl").exists():
        benchmark.generate_data()

    # ① 装载（YourSQL 用批量路径；SQLite/DuckDB 各自原生路径）
    yoursql_db = results_dir / "compare_yoursql.db"
    if args.force:
        _remove_database(yoursql_db)
    started = time.perf_counter()
    yoursql_rows = load_yoursql(benchmark, data_dir, yoursql_db)
    yoursql_load = time.perf_counter() - started

    started = time.perf_counter()
    sqlite_rows = load_sqlite(benchmark, data_dir, results_dir / "compare_sqlite.db")
    sqlite_load = time.perf_counter() - started

    started = time.perf_counter()
    duckdb_rows = load_duckdb(benchmark, data_dir, results_dir / "compare_duckdb.db")
    duckdb_load = time.perf_counter() - started

    # ② 查询
    measurements: list[dict[str, Any]] = []
    for query_id in QUERIES:
        sql = benchmark.get_query(query_id, dialect="sqlite")
        entry: dict[str, Any] = {"query": f"Q{query_id}"}

        entry["yoursql"] = run_yoursql_query(yoursql_db, data_dir, query_id, args.iterations, args.query_timeout)

        connection = sqlite3.connect(str(results_dir / "compare_sqlite.db"))
        try:
            connection.execute(sql)
            times, result = best_of(args.iterations, lambda: connection.execute(sql).fetchall())
            entry["sqlite"] = {"status": "OK", "rows": len(result), "mean_seconds": statistics.mean(times),
                               "min_seconds": min(times), "sample": [[str(value) for value in row] for row in result[:2]]}
        except Exception as error:  # noqa: BLE001
            entry["sqlite"] = {"status": type(error).__name__, "detail": str(error)[:120]}
        finally:
            connection.close()

        connection = duckdb.connect(str(results_dir / "compare_duckdb.db"), read_only=True)
        try:
            duck_sql = to_duckdb(sql)
            connection.execute(duck_sql)
            times, result = best_of(args.iterations, lambda: connection.execute(duck_sql).fetchall())
            entry["duckdb"] = {"status": "OK", "rows": len(result), "mean_seconds": statistics.mean(times),
                               "min_seconds": min(times), "sample": [[str(value) for value in row] for row in result[:2]]}
        except Exception as error:  # noqa: BLE001
            entry["duckdb"] = {"status": type(error).__name__, "detail": str(error)[:120]}
        finally:
            connection.close()
        # HOW：用样本值自动标记跨引擎差异，避免只报耗时、漏掉语义不一致。
        entry["sample_match"] = _sample_match(entry)
        measurements.append(entry)
        joins = ",".join(entry["yoursql"].get("joins") or []) if entry["yoursql"].get("status") == "OK" else ""
        yoursql_text = (
            f"{entry['yoursql']['mean_seconds'] * 1000:.1f} ms / {entry['yoursql']['rows']} 行"
            if entry["yoursql"].get("status") == "OK"
            else entry["yoursql"].get("status", "-")
        )
        sqlite_text = (
            f"{entry['sqlite']['mean_seconds'] * 1000:.1f} ms"
            if entry["sqlite"].get("status") == "OK"
            else entry["sqlite"].get("status", "-")
        )
        duckdb_text = (
            f"{entry['duckdb']['mean_seconds'] * 1000:.1f} ms"
            if entry["duckdb"].get("status") == "OK"
            else entry["duckdb"].get("status", "-")
        )
        # HOW：逐条即时打印，长跑时不必等全部结束。
        print(
            f"Q{query_id:<4} YourSQL {yoursql_text:<28} SQLite {sqlite_text:<14} DuckDB {duckdb_text:<14}"
            f" 连接 [{joins}]",
            flush=True,
        )

    report = {
        "benchmark": "TPC-H",
        "scale_factor": SCALE_FACTOR,
        "queries": [f"Q{query_id}" for query_id in QUERIES],
        "coverage_note": "YourSQL 当前可编译 8/22 条 TPC-H 查询；其余 14 条缺少的特性（派生表/CTE/CASE WHEN/子查询表达式）尚未实现，故不在本批对标内。",
        "value_notes": {
            "Q6": (
                "三引擎数据与 1994 年折扣分布逐值相同；样本差异来自十进制字面量 0.06 ± 0.01 的求值口径："
                "SQLite/YourSQL 得到二进制浮点 0.06999999999999999，丢掉 l_discount = 0.07 的行（837 行），"
                "DuckDB 得到精确 0.07。按 TPC-H 的 DECIMAL 语义 DuckDB 才是正确答案；"
                "YourSQL 与 SQLite 一致。根因是 YourSQL 的 DECIMAL 目前是浮点实现，未提供定点语义。"
            )
        },
        "loaded_rows": {"yoursql": yoursql_rows, "sqlite": sqlite_rows, "duckdb": duckdb_rows},
        "load_seconds": {"yoursql": yoursql_load, "sqlite": sqlite_load, "duckdb": duckdb_load},
        "measurements": measurements,
        "environment": {"python": sys.version, "platform": platform.platform()},
        "scope_note": "TPC-H SF0.01 子集实验，不宣称官方 QphH@Size 成绩。",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"装载: YourSQL {yoursql_load:.1f}s · SQLite {sqlite_load:.1f}s · DuckDB {duckdb_load:.1f}s")
    print(f"{'查询':<6}{'YourSQL':>26}{'SQLite':>22}{'DuckDB':>22}")
    for entry in measurements:
        def render(item: dict[str, Any] | None) -> str:
            if not item:
                return "-"
            if item.get("status") != "OK":
                return f"{item['status']}"
            joins = ",".join(item.get("joins") or [])
            suffix = f" [{joins}]" if joins else ""
            return f"{item['mean_seconds'] * 1000:.1f} ms / {item['rows']} 行{suffix}"
        print(f"{entry['query']:<6}{render(entry.get('yoursql')):>26}{render(entry.get('sqlite')):>22}{render(entry.get('duckdb')):>22}")
    print(f"报告写入: {report_path}")


if __name__ == "__main__":
    main()

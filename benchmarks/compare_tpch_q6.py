"""用同一份 TPC-H SF0.01 数据和同一条 Q6，对照 YourSQL / SQLite / DuckDB。

HOW：三个引擎都走“建表 → 导入 lineitem.tbl → 预热 1 次 → N 次计时”的同一条流程，
只用 time.perf_counter 计查询耗时，导入时间单独记录，避免把装载成本算进查询成绩。
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

import duckdb
from benchbox import TPCH

from benchmarks.run_benchbox_tpch import (
    BENCHMARK_DATABASE_CONFIG,
    ROOT,
    _create_lineitem,
    _load_lineitem,
    _remove_database,
    _read_rows,
    _typed_value,
)
from yoursql.engine.runtime.database import Database


DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_RESULT_DIR = ROOT / "benchmarks" / "results"
DEFAULT_REPORT_PATH = ROOT / "benchmarks" / "reports" / "tpch_sf001_q6_engines.json"
SCALE_FACTOR = 0.01
QUERY_ID = 6


def _schema_columns(benchmark: TPCH) -> list[dict[str, Any]]:
    return benchmark.get_schema()["lineitem"]["columns"]


def _duckdb_type(type_name: str, *, decimal: bool) -> str:
    """TPC-H 类型映射到 DuckDB 列类型。

    HOW：默认把 DECIMAL 映射为 DOUBLE，使三个引擎的谓词都走 IEEE double 语义；
    TPC-H Q6 的 `l_discount BETWEEN 0.06 - 0.01 AND 0.06 + 0.01` 在两种语义下命中
    行数不同（double 会排除恰好等于 0.07 的行），不同语义直接对比没有意义。
    """

    base = type_name.split("(", 1)[0].strip().upper()
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INTEGER"
    if base in {"DECIMAL", "NUMERIC", "REAL", "DOUBLE", "FLOAT"}:
        return type_name if decimal else "DOUBLE"
    if base in {"DATE", "DATETIME", "TIMESTAMP"}:
        return "DATE"
    return "VARCHAR"


def _duckdb_query(sqlite_query: str, *, double_semantics: bool = True) -> str:
    """把 SQLite 方言的 Q6 改写成 DuckDB 可执行形式。

    WHY：两处方言/数值差异必须显式抹平，否则比的是不同语义：
    1. DuckDB 的 date() 不接 SQLite 的 '+1 year' 修饰符；
    2. 折扣常量 `0.06 - 0.01 / 0.06 + 0.01` 在 DuckDB 里按 DECIMAL 精确折叠成 0.05/0.07，
       而 SQLite/YourSQL 按 IEEE double 得到 0.06999999999999999，会把 l_discount = 0.07
       的 391 行排除在外；加显式 DOUBLE 转换后才与 SQLite 同语义。
    """

    translated = sqlite_query.replace("DATE('1994-01-01', '+1 year')", "DATE '1995-01-01'")
    if translated == sqlite_query:
        raise ValueError("Q6 里没有找到需要改写的 '+1 year' 日期修饰符，请检查 BenchBox 版本")
    if not double_semantics:
        return translated
    for old, new in (
        ("0.06 - 0.01", "CAST(0.06 AS DOUBLE) - CAST(0.01 AS DOUBLE)"),
        ("0.06 + 0.01", "CAST(0.06 AS DOUBLE) + CAST(0.01 AS DOUBLE)"),
    ):
        if old not in translated:
            raise ValueError(f"Q6 里没有找到折扣常量 {old}，请检查 BenchBox 版本")
        translated = translated.replace(old, new)
    return translated


def _timed_query(execute: Callable[[str], list[tuple[Any, ...]]], query: str, iterations: int, settle_seconds: float) -> dict[str, Any]:
    """预热一次、静置一段时间后计时；返回每次耗时与最后结果。

    HOW：静置是为了避开“刚导完数据、OS 回写未结束”的冷态，否则同一引擎会测出
    两倍差距（实测 YourSQL 同库同配置：刚装载完 0.72 s vs 稳定后 0.40 s）。
    """

    warmup_rows = execute(query)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    times: list[float] = []
    rows = warmup_rows
    for _ in range(iterations):
        started = time.perf_counter()
        rows = execute(query)
        times.append(time.perf_counter() - started)
    return {
        "settle_seconds": settle_seconds,
        "warmup": 1,
        "iterations": iterations,
        "times_seconds": times,
        "mean_seconds": mean(times),
        "median_seconds": median(times),
        "min_seconds": min(times),
        "result": [[float(value) for value in row] for row in rows],
        "rows_returned": len(rows),
    }


def _run_yoursql(benchmark: TPCH, data_dir: Path, iterations: int, settle_seconds: float) -> dict[str, Any]:
    db_path = DEFAULT_RESULT_DIR / "tpch_sf001_yoursql.db"
    _remove_database(db_path)
    started = time.perf_counter()
    with Database(db_path, config=BENCHMARK_DATABASE_CONFIG) as database:
        _create_lineitem(database, benchmark)
        rows_loaded = _load_lineitem(database, benchmark, data_dir)
        load_seconds = time.perf_counter() - started

        def execute(statement: str) -> list[tuple[Any, ...]]:
            return list(database.execute(statement).rows)

        execution = _timed_query(execute, benchmark.get_query(QUERY_ID, dialect="sqlite"), iterations, settle_seconds)
    return {
        "engine": "YourSQL",
        "version": "0.1.0",
        "semantics": "double",
        "storage": "行存 · 4096B 页 · B+Tree（本查询无可用索引）",
        "query_sql": benchmark.get_query(QUERY_ID, dialect="sqlite"),
        "rows_loaded": rows_loaded,
        "load_seconds": load_seconds,
        "storage_config": {
            "page_size": BENCHMARK_DATABASE_CONFIG.page_size,
            "buffer_pool_size": BENCHMARK_DATABASE_CONFIG.buffer_pool_size,
        },
        **execution,
    }


def _sqlite_type(type_name: str) -> str:
    """TPC-H 类型映射到 SQLite 存储类；只有整数走 INTEGER，其余数值用 REAL。"""

    base = type_name.split("(", 1)[0].strip().upper()
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INTEGER"
    if base in {"DECIMAL", "NUMERIC", "REAL", "DOUBLE", "FLOAT"}:
        return "REAL"
    return "TEXT"


def _load_sqlite(connection: sqlite3.Connection, benchmark: TPCH, data_dir: Path) -> int:
    columns = _schema_columns(benchmark)
    column_types = [str(column["type"]) for column in columns]
    create_columns = ", ".join(f'"{column["name"]}" {_sqlite_type(str(column["type"]))}' for column in columns)
    connection.execute(f"CREATE TABLE lineitem ({create_columns})")
    placeholders = ", ".join("?" for _ in columns)
    payload = [
        tuple(_typed_value(value, type_name) for value, type_name in zip(row, column_types, strict=True))
        for row in _read_rows(data_dir / "lineitem.tbl", column_types)
    ]
    connection.executemany(f"INSERT INTO lineitem VALUES ({placeholders})", payload)
    connection.commit()
    return len(payload)


def _run_sqlite(benchmark: TPCH, data_dir: Path, iterations: int, *, in_memory: bool, settle_seconds: float) -> dict[str, Any]:
    db_path = ":memory:" if in_memory else str(DEFAULT_RESULT_DIR / "tpch_sf001_sqlite.db")
    if not in_memory:
        Path(db_path).unlink(missing_ok=True)
    started = time.perf_counter()
    connection = sqlite3.connect(db_path)
    try:
        rows_loaded = _load_sqlite(connection, benchmark, data_dir)
        load_seconds = time.perf_counter() - started

        def execute(statement: str) -> list[tuple[Any, ...]]:
            cursor = connection.execute(statement)
            try:
                return list(cursor.fetchall())
            finally:
                cursor.close()

        execution = _timed_query(execute, benchmark.get_query(QUERY_ID, dialect="sqlite"), iterations, settle_seconds)
    finally:
        connection.close()
    return {
        "engine": "SQLite（内存库）" if in_memory else "SQLite（文件库）",
        "version": sqlite3.sqlite_version,
        "semantics": "double",
        "storage": "行存 · 8KB 默认页（本查询无索引）",
        "query_sql": benchmark.get_query(QUERY_ID, dialect="sqlite"),
        "rows_loaded": rows_loaded,
        "load_seconds": load_seconds,
        **execution,
    }


def _run_duckdb(benchmark: TPCH, data_dir: Path, iterations: int, *, decimal: bool, settle_seconds: float) -> dict[str, Any]:
    suffix = "decimal" if decimal else "double"
    db_path = DEFAULT_RESULT_DIR / f"tpch_sf001_duckdb_{suffix}.db"
    db_path.unlink(missing_ok=True)
    data_path = (data_dir / "lineitem.tbl").as_posix()
    started = time.perf_counter()
    connection = duckdb.connect(str(db_path))
    try:
        # HOW：列名与类型沿用 TPC-H schema，分隔符按 .tbl 的 '|'；DuckDB 直接把 CSV 建成列存表。
        columns = ", ".join(
            f"'{column['name']}': '{_duckdb_type(str(column['type']), decimal=decimal)}'"
            for column in _schema_columns(benchmark)
        )
        connection.execute(
            f"CREATE TABLE lineitem AS SELECT * FROM read_csv('{data_path}', delim='|', header=false, columns={{{columns}}})"
        )
        rows_loaded = int(connection.execute("SELECT count(*) FROM lineitem").fetchone()[0])
        load_seconds = time.perf_counter() - started
        query = _duckdb_query(benchmark.get_query(QUERY_ID, dialect="sqlite"), double_semantics=not decimal)

        def execute(statement: str) -> list[tuple[Any, ...]]:
            return list(connection.execute(statement).fetchall())

        execution = _timed_query(execute, query, iterations, settle_seconds)
    finally:
        connection.close()
    return {
        "engine": f"DuckDB（{'DECIMAL' if decimal else 'DOUBLE'} 列）",
        "version": duckdb.__version__,
        "semantics": "decimal" if decimal else "double",
        "storage": "列存 · 向量化 OLAP",
        "query_note": (
            "DuckDB 默认按 DECIMAL 精确折叠折扣常量，这里显式转 DOUBLE 以对齐 SQLite/YourSQL 的 IEEE double 语义"
            if not decimal
            else "保留 DECIMAL 精确折叠，仅作参考"
        ),
        "query_sql": query,
        "rows_loaded": rows_loaded,
        "load_seconds": load_seconds,
        **execution,
    }


def _check_results(results: list[dict[str, Any]]) -> None:
    """同谓词语义的引擎（double）结果必须一致；DECIMAL 变体单独作参考，不参与校验。"""

    comparable = [item for item in results if item.get("semantics", "double") == "double"]
    values = [item["result"][0][0] for item in comparable]
    reference = values[0]
    for item, value in zip(comparable, values, strict=True):
        if abs(value - reference) > max(1e-6, abs(reference) * 1e-9):
            raise SystemExit(f"{item['engine']} 的结果 {value} 与参照 {reference} 不一致")
    decimal_variants = [item for item in results if item.get("semantics") == "decimal"]
    for item in decimal_variants:
        print(
            f"参考（不同谓词语义，不计入对比）: {item['engine']} = {item['result'][0][0]:.4f}"
        )


def _print_table(results: list[dict[str, Any]]) -> None:
    header = f"{'引擎':<24}{'谓词语义':<10}{'装载 s':>10}{'平均 s':>10}{'中位 s':>10}{'最快 s':>10}{'Q6 结果':>18}"
    print(header)
    print("-" * len(header))
    for item in results:
        print(
            f"{item['engine']:<24}{item.get('semantics', 'double'):<10}{item['load_seconds']:>10.3f}"
            f"{item['mean_seconds']:>10.4f}{item['median_seconds']:>10.4f}{item['min_seconds']:>10.4f}{item['result'][0][0]:>18.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--settle-seconds", type=float, default=3.0, help="装载后静置时间，避开 OS 回写未结束的冷态")
    parser.add_argument("--skip-yoursql", action="store_true", help="只跑外部引擎基线")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations 必须大于 0")
    if args.settle_seconds < 0:
        parser.error("--settle-seconds 不能为负")

    data_dir = Path(args.data_dir).resolve()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    benchmark = TPCH(scale_factor=SCALE_FACTOR, output_dir=data_dir)
    if not (data_dir / "lineitem.tbl").exists():
        benchmark.generate_data()

    results: list[dict[str, Any]] = []
    if not args.skip_yoursql:
        results.append(_run_yoursql(benchmark, data_dir, args.iterations, args.settle_seconds))
    results.append(_run_sqlite(benchmark, data_dir, args.iterations, in_memory=False, settle_seconds=args.settle_seconds))
    results.append(_run_sqlite(benchmark, data_dir, args.iterations, in_memory=True, settle_seconds=args.settle_seconds))
    results.append(_run_duckdb(benchmark, data_dir, args.iterations, decimal=False, settle_seconds=args.settle_seconds))
    results.append(_run_duckdb(benchmark, data_dir, args.iterations, decimal=True, settle_seconds=args.settle_seconds))

    _check_results(results)
    _print_table(results)
    report = {
        "status": "PASS",
        "benchmark": "TPC-H",
        "query_id": f"Q{QUERY_ID}",
        "scale_factor": SCALE_FACTOR,
        "data_source": str((data_dir / "lineitem.tbl").resolve()),
        "method": "各引擎装载同一份 lineitem.tbl，预热 1 次、静置后计时；仅统计查询耗时，装载单独记录",
        "semantics_note": (
            "TPC-H Q6 折扣谓词在两套数值语义下命中行数不同：DuckDB 默认把字面量 `0.06 ± 0.01` 精确折叠为 "
            "DECIMAL 0.05/0.07，多命中 l_discount = 0.07 的 391 行；SQLite/YourSQL 用 IEEE double 得到 "
            "0.06999999999999999 会排除这些行。同语义组已给 DuckDB 加显式 DOUBLE 转换，DECIMAL 变体仅作参考。"
        ),
        "engines": results,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "cpu": platform.processor(),
        },
        "scope_note": (
            "这是 BenchBox/TPC-H Q6 子集实验，不宣称完整 TPC-H QphH@Size 成绩；"
            "YourSQL 为行存 + 逐行 Python 求值，DuckDB 为列存向量化引擎，两者定位不同，仅作数量级参照。"
        ),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告写入: {report_path}")


if __name__ == "__main__":
    main()

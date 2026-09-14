"""用 BenchBox 的真实 TPC-H Q6 workload 测量 YourSQL。"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from benchbox import TPCH
from benchbox.platforms.sqlite import SQLiteAdapter

from yoursql.common import DatabaseConfig, json_safe
from yoursql.engine.runtime.database import Database


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_DB_PATH = ROOT / "benchmarks" / "results" / "tpch_sf001_yoursql.db"
DEFAULT_REPORT_PATH = ROOT / "benchmarks" / "reports" / "tpch_sf001_q6.json"
SCALE_FACTOR = 0.01
QUERY_ID = 6
BENCHMARK_DATABASE_CONFIG = DatabaseConfig(page_size=16 * 1024, buffer_pool_size=256)


@dataclass
class _YourSQLCursor:
    """把 YourSQL 的 ExecutionResult 映射为 BenchBox 所需的游标。"""

    connection: "_YourSQLConnection"
    _rows: list[tuple[Any, ...]] | None = None

    def execute(self, statement: str) -> "_YourSQLCursor":
        result = self.connection.database.execute(statement)
        self._rows = list(result.rows)
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows or [])

    def close(self) -> None:
        return None


class _YourSQLConnection:
    """最小 DB-API 适配层，仅用于调用 BenchBox 的 SQL 执行路径。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def cursor(self) -> _YourSQLCursor:
        return _YourSQLCursor(self)

    def close(self) -> None:
        self.database.close()


def _base_type(type_name: str) -> str:
    return type_name.split("(", 1)[0].strip().upper()


def _yoursql_type(type_name: str) -> str:
    """将 TPC-H schema 类型映射到 YourSQL 支持的类型。"""

    base = _base_type(type_name)
    if base in {"INTEGER", "INT", "BIGINT"}:
        return "INT"
    # HOW：TPC-H 的金额/折扣/税都是 DECIMAL，必须映射到定点类型；映射成 FLOAT
    # 会让 Q6 的 0.06 ± 0.01 变成浮点近似，从而漏掉 l_discount = 0.07 的行。
    if base in {"DECIMAL", "NUMERIC"}:
        return "DECIMAL"
    if base in {"REAL", "DOUBLE", "FLOAT"}:
        return "FLOAT"
    return "VARCHAR"


def _create_lineitem(database: Database, benchmark: TPCH) -> None:
    schema = benchmark.get_schema()["lineitem"]
    columns = ", ".join(f"{column['name']} {_yoursql_type(column['type'])}" for column in schema["columns"])
    database.execute(f"CREATE TABLE lineitem ({columns});")


def _typed_value(value: str, type_name: str) -> Any:
    base = _base_type(type_name)
    if base in {"INTEGER", "INT", "BIGINT"}:
        return int(value)
    if base in {"DECIMAL", "NUMERIC"}:
        # HOW：.tbl 里的定点字段是文本，直接进 Decimal 才能保住标度。
        return Decimal(value)
    if base in {"REAL", "DOUBLE", "FLOAT"}:
        return float(value)
    return value


def _read_rows(data_path: Path, column_types: list[str]) -> Iterable[tuple[str, ...]]:
    with data_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw_fields = line.rstrip("\r\n").split("|")
            if raw_fields and raw_fields[-1] == "":
                raw_fields.pop()
            fields = tuple(raw_fields)
            if len(fields) != len(column_types):
                raise ValueError(f"lineitem.tbl 第 {line_number} 行字段数错误: {len(fields)}")
            yield fields


def _load_lineitem(database: Database, benchmark: TPCH, data_dir: Path) -> int:
    schema = benchmark.get_schema()["lineitem"]
    column_types = [str(column["type"]) for column in schema["columns"]]
    data_path = data_dir / "lineitem.tbl"
    typed_rows = (
        tuple(_typed_value(value, type_name) for value, type_name in zip(row, column_types, strict=True))
        for row in _read_rows(data_path, column_types)
    )
    return database.insert_rows("lineitem", typed_rows).affected_rows


def _remove_database(path: Path) -> Path:
    """清理同名旧基准库；返回本次实际可用的库路径。

    WHY：受限环境（安全删除策略、只读缓存目录）不允许 unlink 已存在的库文件，
    直接失败会让整轮基准跑不完。这里退回到带序号的新文件名，保证仍可复现地跑完。
    """

    allowed_root = (ROOT / "benchmarks" / "results").resolve()
    resolved = path.resolve()
    if allowed_root not in resolved.parents:
        raise ValueError(f"--force 只允许清理 {allowed_root} 下的 benchmark 数据库")
    if not resolved.exists():
        return resolved
    try:
        resolved.unlink()
        return resolved
    except OSError:
        for index in range(1, 100):
            candidate = resolved.with_name(f"{resolved.stem}-{index}{resolved.suffix}")
            if not candidate.exists():
                return candidate
        raise


def _run(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = Path(args.data_dir).resolve()
    db_path = Path(args.database).resolve()
    report_path = Path(args.report).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if args.force:
        db_path = _remove_database(db_path)

    benchmark = TPCH(scale_factor=SCALE_FACTOR, output_dir=data_dir)
    generated_files = benchmark.generate_data()
    query = benchmark.get_query(QUERY_ID, dialect="sqlite")
    schema = benchmark.get_schema()["lineitem"]

    with Database(db_path, config=BENCHMARK_DATABASE_CONFIG) as database:
        _create_lineitem(database, benchmark)
        loaded_rows = _load_lineitem(database, benchmark, data_dir)
        connection = _YourSQLConnection(database)
        adapter = SQLiteAdapter(database_path=":memory:")

        # BenchBox adapter 的 execute_query 是实际计时与结果封装路径；这里只负责提供 YourSQL 连接。
        adapter.execute_query(connection, query, f"Q{QUERY_ID}", benchmark_type="olap", validate_row_count=False)
        measurements: list[dict[str, Any]] = []
        for _ in range(args.iterations):
            measurements.append(
                adapter.execute_query(
                    connection,
                    query,
                    f"Q{QUERY_ID}",
                    benchmark_type="olap",
                    scale_factor=SCALE_FACTOR,
                    validate_row_count=False,
                )
            )
        result = measurements[-1]

    times = [float(item["execution_time_seconds"]) for item in measurements]
    report: dict[str, Any] = {
        "status": "PASS" if all(item.get("status") != "FAILED" for item in measurements) else "FAIL",
        "benchmark": "TPC-H",
        "benchmark_runner": {"name": "BenchBox", "version": "0.4.0"},
        "scale_factor": SCALE_FACTOR,
        "query_id": f"Q{QUERY_ID}",
        "query_source": "TPCH(scale_factor=0.01).get_query(6, dialect='sqlite')",
        "query_sql": query,
        "generated_files": [str(Path(path).resolve()) for path in generated_files],
        "loaded_table": {
            "name": "lineitem",
            "rows": loaded_rows,
            "source": str((data_dir / "lineitem.tbl").resolve()),
            "columns": schema["columns"],
        },
        "storage_config": {
            "page_size": BENCHMARK_DATABASE_CONFIG.page_size,
            "buffer_pool_size": BENCHMARK_DATABASE_CONFIG.buffer_pool_size,
        },
        "execution": {
            "warmup": 1,
            "iterations": args.iterations,
            "times_seconds": times,
            "mean_seconds": mean(times),
            "median_seconds": median(times),
            "min_seconds": min(times),
            "rows_returned": len(result.get("results") or []),
            "result": result.get("results"),
        },
        "environment": {"python": sys.version, "platform": platform.platform()},
        "scope_note": "这是 BenchBox/TPC-H Q6 的可复现实验，不宣称完整 TPC-H QphH@Size 官方成绩。",
    }
    report_path.write_text(
        json.dumps(json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(json_safe(report), ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--force", action="store_true", help="清理本次 benchmark 目录下的旧数据库")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations 必须大于 0")
    _run(args)


if __name__ == "__main__":
    main()

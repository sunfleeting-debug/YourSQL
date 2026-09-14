"""跨引擎逐值对拍：8 条可编译 TPC-H 查询的 YourSQL 结果 vs 同数据 SQLite 库。

HOW：先跑 `python -m benchmarks.compare_tpch_queries`（会重建三个引擎的对照库），再执行本脚本。
WHY：性能对标必须建立在“算的是同一件事”之上；只比对行数会漏掉值层面的语义偏差。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from benchbox import TPCH

from benchmarks.run_benchbox_tpch import BENCHMARK_DATABASE_CONFIG
from yoursql.engine.runtime.database import Database

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_RESULTS_DIR = ROOT / "benchmarks" / "results"
QUERIES = (1, 3, 5, 6, 10, 16, 18, 19)


def normalize(value: object) -> object:
    """浮点归一到 6 位小数，便于跨引擎比较（DuckDB/YourSQL 的 DECIMAL 宽度略有不同）。"""

    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bool):
        return int(value)
    return value


def canonical(rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    """将结果行转换为稳定的可比较表示。"""
    return sorted(tuple(normalize(value) for value in row) for row in rows)


def main() -> int:
    """解析命令行参数并启动当前脚本任务。"""
    parser = argparse.ArgumentParser(description="TPC-H 结果逐值对拍（YourSQL vs SQLite）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args()

    benchmark = TPCH(scale_factor=0.01, output_dir=args.data_dir)
    sqlite_path = args.results_dir / "compare_sqlite.db"
    yoursql_path = args.results_dir / "compare_yoursql.db"
    if not sqlite_path.exists() or not yoursql_path.exists():
        print(f"缺少对照库，请先运行 benchmarks.compare_tpch_queries：{sqlite_path}")
        return 2

    connection = sqlite3.connect(sqlite_path)
    failures = 0
    with Database(yoursql_path, config=BENCHMARK_DATABASE_CONFIG) as database:
        for number in QUERIES:
            sql = benchmark.get_query(number, dialect="sqlite")
            expected = canonical([tuple(row) for row in connection.execute(sql).fetchall()])
            result = database.execute(sql)
            actual = canonical([tuple(row) for row in result.rows])
            joins = result.stats.get("joins") or []
            if actual != expected:
                print(f"Q{number:<3} 不一致 · YourSQL {len(actual)} 行 · SQLite {len(expected)} 行")
                for index in range(min(len(actual), len(expected))):
                    if actual[index] != expected[index]:
                        print(f"      首个差异 YourSQL {actual[index]} · SQLite {expected[index]}")
                        break
                failures += 1
                continue
            print(f"Q{number:<3} 一致 · {len(actual)} 行 · 连接 {joins}")
    connection.close()

    print("\n对拍", "全部一致" if failures == 0 else f"{failures} 条不一致")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

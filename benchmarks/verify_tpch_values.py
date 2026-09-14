"""跨引擎逐值对拍：TPC-H 查询的 YourSQL 结果 vs 同数据 SQLite 库。

HOW：默认先跑 `python -m benchmarks.compare_tpch_queries`（会重建三个引擎的对照库），
再执行本脚本；也可以直接用 `--sqlite-db/--yoursql-db` 指向任意一对同数据对照库。
WHY：性能对标必须建立在"算的是同一件事"之上；只比对行数会漏掉值层面的语义偏差。

覆盖范围（TPC-H SF0.01，共 22 条）：
- QUERIES：可执行且耗时可控，逐值对拍；
- SLOW_QUERIES：可执行但单条约 50 s（相关子查询逐行重跑），默认跳过，用 `--queries` 显式带上；
- BLOCKED_QUERIES：相关子查询逐行重跑导致分钟级超时，尚未进入验收集合。
"""

from __future__ import annotations

import argparse
import sqlite3
from decimal import Decimal
from pathlib import Path

from benchbox import TPCH

from benchmarks.run_benchbox_tpch import BENCHMARK_DATABASE_CONFIG
from yoursql.engine.runtime.database import Database

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_RESULTS_DIR = ROOT / "benchmarks" / "results"
# HOW：可执行且耗时可控的查询（Q8 由自连接歧义键修复后进入本集合）。
QUERIES = (1, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19)
# HOW：能出正确结果但单条约 50 s，默认不跑。
SLOW_QUERIES = (22,)
# HOW：相关子查询逐行重跑，当前为分钟级超时（性能问题，非正确性问题）。
BLOCKED_QUERIES = (2, 4, 15, 20, 21)

# WHY：这些查询与 SQLite 的差异是「口径」而非「错误」，逐值对拍必然不等，单独记录。
# Q6 的 Where 条件是 `l_discount` 落在 `0.06 + 0.01` 区间：YourSQL 的 DECIMAL 是精确十进制，
# 得到 0.07（与 DuckDB DECIMAL 一致，也是 TPC-H 要求的语义）；SQLite 用双精度浮点得到
# 0.06999999999999999，因此多算/少算若干 l_discount = 0.07 的行，样本值必然不同。
KNOWN_DIFFERENCES = {
    6: "DECIMAL 定点语义 vs SQLite 双精度浮点：YourSQL/ DuckDB 得 1193053.2253，SQLite 得 734493.7281",
}


def normalize(value: object) -> object:
    """数值归一到 6 位小数，便于跨引擎比较。

    HOW：YourSQL 的 DECIMAL 是精确十进制、SQLite 是双精度浮点，末几位必然不同；
    归一后两者在 15 位有效数字内一致（例如 Q14：15.48654581228407148574248997 vs 15.48654581228407）。
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return round(float(value), 6)
    if isinstance(value, float):
        return round(value, 6)
    return value


def canonical(rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return sorted(tuple(normalize(value) for value in row) for row in rows)


def _resolve(path: Path, fallback: Path) -> Path:
    return path if path.exists() else fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="TPC-H 结果逐值对拍（YourSQL vs SQLite）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--sqlite-db", type=Path, default=None)
    parser.add_argument("--yoursql-db", type=Path, default=None)
    parser.add_argument("--password", default="admin")
    parser.add_argument(
        "--queries",
        default=None,
        help=f"逗号分隔的查询号，默认 {'/'.join(str(i) for i in QUERIES)}",
    )
    args = parser.parse_args()

    benchmark = TPCH(scale_factor=0.01, output_dir=args.data_dir)
    sqlite_path = args.sqlite_db or _resolve(
        args.results_dir / "compare_sqlite.db",
        args.results_dir / "tpch_sf001_sqlite.db",
    )
    yoursql_path = args.yoursql_db or _resolve(
        args.results_dir / "compare_yoursql.db",
        args.results_dir / "tpch_sf001_coverage.db",
    )
    if not sqlite_path.exists() or not yoursql_path.exists():
        print(f"缺少对照库：{sqlite_path} / {yoursql_path}")
        print("请先运行 `python -m benchmarks.compare_tpch_queries`，或用 --sqlite-db/--yoursql-db 指定。")
        return 2

    numbers = (
        tuple(int(part) for part in args.queries.split(","))
        if args.queries
        else QUERIES
    )
    connection = sqlite3.connect(str(sqlite_path))
    failures = 0
    total = 0
    expected_diffs = 0
    with Database(yoursql_path, config=BENCHMARK_DATABASE_CONFIG, password=args.password) as database:
        for number in numbers:
            total += 1
            sql = benchmark.get_query(number, dialect="sqlite")
            try:
                expected = canonical([tuple(row) for row in connection.execute(sql).fetchall()])
            except sqlite3.Error as error:
                print(f"Q{number:<3} SQLite 侧失败：{error}")
                failures += 1
                continue
            try:
                result = database.execute(sql)
            except Exception as error:  # noqa: BLE001 - 逐条如实记录，不中断整轮
                print(f"Q{number:<3} YourSQL 侧失败：{str(error).splitlines()[0][:110]}")
                failures += 1
                continue
            actual = canonical([tuple(row) for row in result.rows])
            joins = result.stats.get("joins") or []
            if actual != expected:
                if number in KNOWN_DIFFERENCES:
                    expected_diffs += 1
                    print(f"Q{number:<3} 已知口径差异 · {KNOWN_DIFFERENCES[number]}")
                    print(f"      YourSQL {actual[:1]} · SQLite {expected[:1]}")
                    continue
                print(f"Q{number:<3} 不一致 · YourSQL {len(actual)} 行 · SQLite {len(expected)} 行")
                for index in range(min(len(actual), len(expected))):
                    if actual[index] != expected[index]:
                        print(f"      首个差异 YourSQL {actual[index]} · SQLite {expected[index]}")
                        break
                failures += 1
                continue
            print(f"Q{number:<3} 一致 · {len(actual)} 行 · 连接 {joins}")
    connection.close()

    print(f"\n对拍 {total - failures}/{total} 一致")
    if expected_diffs:
        print(f"其中 {expected_diffs} 条为已知口径差异（见脚本内 KNOWN_DIFFERENCES）")
    if SLOW_QUERIES or BLOCKED_QUERIES:
        slow = "/".join(f"Q{i}" for i in SLOW_QUERIES + BLOCKED_QUERIES)
        print(f"未纳入本轮：{slow}（相关子查询逐行重跑，分钟级耗时）")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

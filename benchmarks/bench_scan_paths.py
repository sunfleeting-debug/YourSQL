"""单表扫描/聚合路径的耗时基线：每个用例独立进程、重复取最小值。

WHY：同进程内连着测几条语句会被堆状态与 GC 干扰——同一个 `COUNT(*)` 在不同用例
之后跑能差 20% 以上（实测 480–650 ms 之间漂）。判断"扫描路径改动有没有变快"必须
用固定口径：**每个用例新起一个进程、重复 R 次取最小值**。

HOW：父进程为每个用例 spawn 自身并带 `--one`，子进程只跑那一条语句；默认用例同时
覆盖两类路径——不需要任何列值（`COUNT(*)` / `SELECT 常量`，可免解码）与需要列值
（`COUNT(列)` / `SUM(列)` / 纯投影，解码是硬性下限）。

用法：
    python -m benchmarks.bench_scan_paths
    python -m benchmarks.bench_scan_paths --sql "SELECT COUNT(*) FROM lineitem;"
    python -m benchmarks.bench_scan_paths --rounds 5 --repeats 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "benchmarks" / "results" / "tpch_sf001_yoursql.db"

# HOW：默认用例围绕 TPC-H SF0.01 的 lineitem（60,175 行、16 列、含 4 个 DECIMAL 列），
# 它是本项目里最典型的"宽表全表扫描"。
DEFAULT_CASES: list[tuple[str, str]] = [
    ("COUNT(*) 全表", "SELECT COUNT(*) FROM lineitem;"),
    ("COUNT(列)", "SELECT COUNT(l_quantity) FROM lineitem;"),
    ("SUM(列)", "SELECT sum(l_quantity) FROM lineitem;"),
    ("纯投影 单列", "SELECT l_quantity FROM lineitem;"),
    ("纯投影 常量", "SELECT 1 FROM lineitem;"),
]


def measure(database: Path, sql: str, repeats: int) -> dict[str, object]:
    """在同一进程内重复执行并取最小值；供子进程调用。"""

    from yoursql.engine.runtime.database import Database

    with Database(database) as connection:
        # HOW：先跑一条语句把目录、缓冲池和计划缓存预热到位，避免把首次开销算进去。
        connection.execute("SELECT 1;")
        best: float | None = None
        result = None
        for _ in range(repeats):
            start = time.perf_counter()
            result = connection.execute(sql)
            elapsed = (time.perf_counter() - start) * 1000
            best = elapsed if best is None else min(best, elapsed)
    return {
        "milliseconds": best,
        "rows_examined": None if result is None else result.stats.get("rows_examined"),
        "operator": None if result is None else result.stats.get("operator"),
    }


def run_isolated(
    database: Path, sql: str, repeats: int, rounds: int
) -> dict[str, object]:
    """为一条语句起 rounds 个子进程，各自取最小耗时后再取最小值。"""

    best: float | None = None
    detail: dict[str, object] = {}
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    for _ in range(rounds):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--one",
                sql,
                "--database",
                str(database),
                "--repeats",
                str(repeats),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=environment,
            check=True,
        )
        detail = json.loads(completed.stdout)
        value = float(detail["milliseconds"])
        best = value if best is None else min(best, value)
    detail["milliseconds"] = best
    return detail


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="单表扫描/聚合路径耗时基线")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--sql", action="append", help="只测指定语句（可重复）")
    parser.add_argument("--rounds", type=int, default=3, help="每个用例起几个进程")
    parser.add_argument("--repeats", type=int, default=5, help="每个进程内重复几次")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--one", help="内部用法：子进程只跑这一条语句")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.one:
        print(json.dumps(measure(args.database, args.one, args.repeats)))
        return 0
    cases = (
        [(sql, sql) for sql in args.sql] if args.sql else list(DEFAULT_CASES)
    )
    print(f"数据库 {args.database}")
    print(f"口径：{args.rounds} 个进程 × 每进程 {args.repeats} 次，取最小值\n")
    report: list[dict[str, object]] = []
    for label, sql in cases:
        detail = run_isolated(args.database, sql, args.repeats, args.rounds)
        report.append({"label": label, "sql": sql, **detail})
        print(
            f"{label:14}: {float(detail['milliseconds']):8.1f} ms   "
            f"operator={detail['operator']}  rows_examined={detail['rows_examined']}"
        )
    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""在独立进程中执行一条 TPC-H 查询并输出计时 JSON（供多查询基准调用）。

WHY：多表查询可能触发嵌套循环连接的超时甚至 MemoryError，放在子进程里跑可以让
父进程用超时与退出码记录“超时/OOM/失败”，而不是把整轮基准带崩。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from benchbox import TPCH

from benchmarks.run_benchbox_tpch import BENCHMARK_DATABASE_CONFIG, ROOT, SCALE_FACTOR
from yoursql.engine.runtime.database import Database

DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--query", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    sql = TPCH(scale_factor=SCALE_FACTOR, output_dir=args.data_dir).get_query(args.query, dialect="sqlite")
    with Database(args.database, config=BENCHMARK_DATABASE_CONFIG) as database:
        started = time.perf_counter()
        result = database.execute(sql)
        first_seconds = time.perf_counter() - started
        times: list[float] = []
        for _ in range(args.iterations):
            started = time.perf_counter()
            database.execute(sql)
            times.append(time.perf_counter() - started)

    print(
        json.dumps(
            {
                "query": f"Q{args.query}",
                "status": "OK",
                "rows": len(result.rows),
                "operator": str((result.stats or {}).get("operator")),
                "joins": list((result.stats or {}).get("joins", [])),
                "first_seconds": first_seconds,
                "times_seconds": times,
                "mean_seconds": statistics.mean(times),
                "median_seconds": statistics.median(times),
                "min_seconds": min(times),
                "sample": [[str(value) for value in row] for row in result.rows[:3]],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())

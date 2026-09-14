"""TPC-H SF0.01 覆盖率报告：22 条查询在 YourSQL 上"可编译 / 可执行"的情况。

HOW：
- `--mode compile`：在当前进程里逐条走 parser+binder+planner，记录编译失败原因；
- `--mode exec`（默认）：逐条起子进程执行（单条超时保护），记录耗时/行数/算子，
  避免某一条挂死拖垮整轮。

WHY：TPC-H 的 22 条查询覆盖了本课程要求的大部分 SQL 特性，用它当"特性是否补齐"的
回归门禁最省事。执行失败要区分两类原因：编译不了（缺特性）和跑不完（性能），
两者的后续动作完全不同。

报告写入 benchmarks/reports/tpch_sf001_coverage.json。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "benchmarks" / "third_party" / "tpch_sf001"
DEFAULT_DB_PATH = ROOT / "benchmarks" / "results" / "tpch_sf001_coverage.db"
DEFAULT_REPORT_PATH = ROOT / "benchmarks" / "reports" / "tpch_sf001_coverage.json"
TOTAL = 22
DEFAULT_TIMEOUT = 45
DEFAULT_PASSWORD = "admin"


def run_compile(database_path: Path, data_dir: Path, password: str) -> list[dict]:
    """逐条编译，返回每条查询的状态（本进程内完成，不需要子进程）。"""

    sys.path.insert(0, str(ROOT))
    from benchbox import TPCH

    from yoursql.engine.runtime.database import Database

    benchmark = TPCH(scale_factor=0.01, output_dir=data_dir)
    entries: list[dict] = []
    with Database(database_path, password=password) as database:
        for index in range(1, TOTAL + 1):
            sql = benchmark.get_query(index, dialect="sqlite")
            try:
                compilation = database.compile(sql)
            except Exception as error:  # noqa: BLE001 - 逐条如实记录
                detail = str(error).splitlines()[0][:200]
                entries.append({"query": f"Q{index}", "status": "ERROR", "detail": detail})
                print(f"Q{index:<2} 编译失败  {detail}", flush=True)
                continue
            entries.append({"query": f"Q{index}", "status": "OK"})
            print(f"Q{index:<2} 编译通过  节点数={len(str(compilation.plan))}", flush=True)
    return entries


def run_exec(database_path: Path, data_dir: Path, timeout: int) -> list[dict]:
    """逐条起子进程执行，单条超时即记为 TIMEOUT。"""

    entries: list[dict] = []
    for index in range(1, TOTAL + 1):
        command = [
            sys.executable, "-m", "benchmarks.run_tpch_query",
            "--database", str(database_path), "--query", str(index),
            "--iterations", "1", "--data-dir", str(data_dir),
        ]
        try:
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            entries.append({"query": f"Q{index}", "status": "TIMEOUT", "detail": f"超过 {timeout}s"})
            print(f"Q{index:<2} TIMEOUT", flush=True)
            continue
        if completed.returncode != 0:
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            entries.append({"query": f"Q{index}", "status": "ERROR", "detail": tail[0][:200]})
            print(f"Q{index:<2} ERROR   {tail[0][:110]}", flush=True)
            continue
        payload = json.loads((completed.stdout or "{}").strip().splitlines()[-1])
        entries.append({
            "query": f"Q{index}",
            "status": "OK",
            "rows": payload.get("rows"),
            "first_seconds": payload.get("first_seconds"),
            "operator": payload.get("operator"),
        })
        print(
            f"Q{index:<2} OK      {payload.get('first_seconds', 0) * 1000:9.1f} ms "
            f"行数={payload.get('rows')} 算子={payload.get('operator')}",
            flush=True,
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="TPC-H 22 条查询的编译/执行覆盖率")
    parser.add_argument("--mode", choices=("compile", "exec"), default="exec")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    if not args.database.exists():
        print(f"缺少数据库：{args.database}")
        print("请先用基准装载脚本建库（见 benchmarks/run_benchbox_tpch.py）。")
        return 2

    entries = (
        run_compile(args.database, args.data_dir, args.password)
        if args.mode == "compile"
        else run_exec(args.database, args.data_dir, args.timeout)
    )
    ok = [item for item in entries if item["status"] == "OK"]
    label = "可编译" if args.mode == "compile" else "可执行"

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            {"mode": args.mode, "queries": entries, "ok": len(ok), "total": len(entries)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n{label} {len(ok)}/{len(entries)}")
    print("报告:", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

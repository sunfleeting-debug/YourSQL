"""对照 Buffer Pool 基线、2Q 和页面类型保护策略。

HOW：实验先重复访问少量 INDEX 页形成热点，再访问一组热点 HEAP 页和冷的次级
INDEX，随后顺序扫描所有 HEAP 页，最后重复探测核心 INDEX。`--mode compare` 会按
基线 → 2Q → 类型保护 → 组合策略的顺序运行，方便直接观察两个优化的先后贡献。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from yoursql.engine.runtime.database import Database
from yoursql.storage import BufferPool, DiskManager, PageType


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "buffer_pool_lab_demo.db"
DEFAULT_REPORT = ROOT / "benchmarks" / "reports" / "buffer_pool_lab.json"
POLICIES = ("lru", "fifo", "2q")


@dataclass(frozen=True)
class TrialMode:
    """一次实验所使用的缓存开关。"""

    name: str
    policy: str
    protect_page_types: bool


MODES = (
    TrialMode("baseline", "lru", False),
    TrialMode("2q", "2q", False),
    TrialMode("type-aware", "lru", True),
    TrialMode("combined", "2q", True),
)


def _inventory(path: Path) -> tuple[int, list[int], list[int]]:
    """枚举实验库中的页类型，返回页大小、HEAP 页和 INDEX 页。"""

    page_size = Database.detect_page_size(path)
    if page_size is None:
        raise ValueError(f"无法识别数据库页大小：{path}")
    heap_pages: list[int] = []
    index_pages: list[int] = []
    with DiskManager(path, page_size=page_size) as disk:
        for page_id in range(1, disk.page_count):
            page_type = disk.read(page_id).page_type
            if page_type is PageType.HEAP:
                heap_pages.append(page_id)
            elif page_type is PageType.INDEX:
                index_pages.append(page_id)
    return page_size, heap_pages, index_pages


def _touch(pool: BufferPool, page_id: int) -> None:
    """访问并释放一个页，模拟一次上层算子使用页的过程。"""

    pool.get_page(page_id, pin=True)
    pool.unpin(page_id)


def _stats_delta(before: object, after: object) -> dict[str, int | float]:
    """计算两个 BufferPoolStats 之间的访问增量。"""

    fields = (
        "hits",
        "misses",
        "evictions",
        "promotions",
        "writebacks",
        "type_protection_skips",
        "cold_hits",
        "hot_hits",
    )
    return {
        field: getattr(after, field) - getattr(before, field) for field in fields
    }


def _phase(pool: BufferPool, operation: Callable[[], None]) -> dict[str, int | float]:
    """运行一个工作负载阶段并返回缓存指标增量。"""

    before = pool.stats()
    operation()
    return _stats_delta(before, pool.stats())


def _aggregate(phases: list[dict[str, int | float]]) -> dict[str, int | float]:
    """合并多个循环的阶段指标。"""

    fields = (
        "hits",
        "misses",
        "evictions",
        "promotions",
        "writebacks",
        "type_protection_skips",
        "cold_hits",
        "hot_hits",
    )
    result = {field: sum(phase[field] for phase in phases) for field in fields}
    requests = int(result["hits"] + result["misses"])
    result["requests"] = requests
    result["hit_rate"] = result["hits"] / requests if requests else 0.0
    return result


def _run_trial(
    path: Path,
    mode: TrialMode,
    *,
    capacity: int,
    prime_rounds: int,
    scan_rounds: int,
    probe_rounds: int,
    cycles: int,
) -> dict[str, object]:
    """运行一次热点索引与顺序扫描冲突实验。"""

    page_size, heap_pages, index_pages = _inventory(path)
    if len(heap_pages) <= capacity:
        raise ValueError(
            f"HEAP 页数为 {len(heap_pages)}，需要大于缓存容量 {capacity}"
        )
    if len(index_pages) < 2:
        raise ValueError("INDEX 页至少需要 2 页，才能形成稳定的热点工作集")
    hot_index_count = min(max(2, capacity // 2 + 3), len(index_pages))
    hot_index_pages = index_pages[:hot_index_count]
    core_index_count = min(max(2, capacity // 2), len(hot_index_pages))
    core_index_pages = hot_index_pages[:core_index_count]
    hot_heap_pages = heap_pages[-min(max(2, capacity // 2), len(heap_pages)) :]
    secondary_index_pages = hot_index_pages[core_index_count:]
    probe_index_pages = list(reversed(core_index_pages)) * 4 + list(
        reversed(secondary_index_pages)
    )

    with DiskManager(path, page_size=page_size) as disk:
        pool = BufferPool(
            disk,
            capacity=capacity,
            replacement_policy=mode.policy,
            protect_page_types=mode.protect_page_types,
        )
        before_io = disk.io_stats()
        started = time.perf_counter()
        try:
            prime = _phase(
                pool,
                lambda: [
                    _touch(pool, page_id)
                    for _ in range(prime_rounds)
                    for page_id in core_index_pages
                ]
                + [
                    _touch(pool, page_id)
                    for _ in range(prime_rounds)
                    for page_id in hot_heap_pages
                ]
                + [
                    _touch(pool, page_id)
                    for page_id in secondary_index_pages
                ],
            )
            cycle_results = []
            scan_phases = []
            probe_phases = []
            resident_after_scan = []
            for _ in range(cycles):
                scan_phase = _phase(
                    pool,
                    lambda: [
                        _touch(pool, page_id)
                        for _ in range(scan_rounds)
                        for page_id in heap_pages
                    ],
                )
                resident_after_scan = [
                    page_id for page_id in hot_index_pages if page_id in pool
                ]
                probe_phase = _phase(
                    pool,
                    lambda: [
                        _touch(pool, page_id)
                        for _ in range(probe_rounds)
                        for page_id in probe_index_pages
                    ],
                )
                scan_phases.append(scan_phase)
                probe_phases.append(probe_phase)
                cycle_results.append({"scan": scan_phase, "probe": probe_phase})
            scan = _aggregate(scan_phases)
            probe = _aggregate(probe_phases)
            final_stats = pool.stats()
            after_io = disk.io_stats()
        finally:
            pool.close()

    elapsed = time.perf_counter() - started
    return {
        "mode": mode.name,
        "policy": mode.policy,
        "protect_page_types": mode.protect_page_types,
        "capacity": capacity,
        "hot_index_pages": hot_index_pages,
        "core_index_pages": core_index_pages,
        "probe_index_pages": probe_index_pages,
        "hot_index_resident_after_scan": resident_after_scan,
        "heap_pages": len(heap_pages),
        "index_pages": len(index_pages),
        "prime": prime,
        "scan": scan,
        "probe": probe,
        "cycles": cycle_results,
        "total": final_stats.to_dict(),
        "page_reads": after_io.page_reads - before_io.page_reads,
        "elapsed_seconds": elapsed,
    }


def _selected_modes(args: argparse.Namespace) -> tuple[TrialMode, ...]:
    """根据命令行开关选择运行基线、阶段或自定义组合。"""

    if args.mode == "compare":
        return MODES
    if args.mode == "custom":
        return (
            TrialMode(
                "custom",
                args.policy,
                args.protect_page_types,
            ),
        )
    return tuple(mode for mode in MODES if mode.name == args.mode)


def _print_results(results: list[dict[str, object]]) -> None:
    """打印适合现场观察的简洁对照表。"""

    print(
        "mode         policy  type-aware  probe_hits  probe_misses  "
        "probe_rate  resident"
    )
    for result in results:
        probe = result["probe"]
        assert isinstance(probe, dict)
        print(
            f"{str(result['mode']):<12} {str(result['policy']):<7} "
            f"{str(result['protect_page_types']):<10} "
            f"{int(probe['hits']):>10} {int(probe['misses']):>12} "
            f"{float(probe['hit_rate']):>10.1%} "
            f"{len(result['hot_index_resident_after_scan']):>8}"
        )


def main() -> None:
    """解析开关、运行实验并保存 JSON 报告。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--mode",
        choices=("compare", "baseline", "2q", "type-aware", "combined", "custom"),
        default="compare",
        help="compare 按基线、2Q、类型保护、组合策略依次运行",
    )
    parser.add_argument("--policy", choices=POLICIES, default="lru")
    parser.add_argument(
        "--protect-page-types",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="优先淘汰 HEAP 页，保护 INDEX/CATALOG/SUPERBLOCK 页",
    )
    parser.add_argument("--capacity", type=int, default=16)
    parser.add_argument("--prime-rounds", type=int, default=3)
    parser.add_argument("--scan-rounds", type=int, default=1)
    parser.add_argument("--probe-rounds", type=int, default=1)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if args.capacity < 2 or args.prime_rounds < 2 or args.scan_rounds < 1 or args.probe_rounds < 1 or args.cycles < 1:
        parser.error("capacity 至少为 2，prime-rounds 至少为 2，其余轮数至少为 1")
    if not args.database.is_file():
        parser.error(f"数据库不存在，请先运行 create_buffer_pool_lab：{args.database}")

    modes = _selected_modes(args)
    results = [
        _run_trial(
            args.database,
            mode,
            capacity=args.capacity,
            prime_rounds=args.prime_rounds,
            scan_rounds=args.scan_rounds,
            probe_rounds=args.probe_rounds,
            cycles=args.cycles,
        )
        for mode in modes
    ]
    report = {
        "database": str(args.database.resolve()),
        "workload": {
            "sequence": "prime core INDEX repeatedly -> hot HEAP working set -> cold secondary INDEX -> scan all HEAP pages -> probe core four times + secondary INDEX",
            "capacity": args.capacity,
            "prime_rounds": args.prime_rounds,
            "scan_rounds": args.scan_rounds,
            "probe_rounds": args.probe_rounds,
            "cycles": args.cycles,
        },
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _print_results(results)
    print(f"report: {args.report.resolve()}")


if __name__ == "__main__":
    main()

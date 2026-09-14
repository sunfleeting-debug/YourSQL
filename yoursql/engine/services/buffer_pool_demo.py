"""【前端特供】Buffer Pool 扫描污染实验，不属于数据库执行核心。"""

from __future__ import annotations

from time import perf_counter
from typing import Callable

from yoursql.common import JsonObject
from yoursql.common.errors import YourSQLError
from yoursql.storage import BufferPool, DiskManager, PageType


DEMO_MODES = (
    ("baseline", "lru", False),
    ("2q", "2q", False),
    ("type-aware", "lru", True),
    ("combined", "2q", True),
)


def _inventory(disk: DiskManager) -> tuple[list[int], list[int]]:
    """枚举当前数据库的 HEAP 和 INDEX 页，不改变 BufferPool 统计。"""

    heap_pages: list[int] = []
    index_pages: list[int] = []
    for page_id in range(1, disk.page_count):
        page_type = disk.peek(page_id).page_type
        if page_type is PageType.HEAP:
            heap_pages.append(page_id)
        elif page_type is PageType.INDEX:
            index_pages.append(page_id)
    return heap_pages, index_pages


def _touch(pool: BufferPool, page_id: int) -> None:
    """访问并释放一个页，模拟执行算子使用页的生命周期。"""

    pool.get_page(page_id, pin=True)
    pool.unpin(page_id)


def _delta(before: object, after: object) -> dict[str, int]:
    """计算一个实验阶段的缓存计数增量。"""

    fields = (
        "hits",
        "misses",
        "evictions",
        "promotions",
        "writebacks",
        "type_protection_skips",
    )
    return {field: int(getattr(after, field) - getattr(before, field)) for field in fields}


def _phase(pool: BufferPool, operation: Callable[[], None]) -> JsonObject:
    """运行阶段并记录耗时与缓存增量。"""

    before = pool.stats()
    started = perf_counter()
    operation()
    result = _delta(before, pool.stats())
    result["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    result["requests"] = result["hits"] + result["misses"]
    result["hit_rate"] = (
        round(result["hits"] / result["requests"], 4) if result["requests"] else 0.0
    )
    return result


def _aggregate(phases: list[JsonObject]) -> JsonObject:
    """合并多个同类阶段，保留总请求量和总耗时。"""

    fields = (
        "hits",
        "misses",
        "evictions",
        "promotions",
        "writebacks",
        "type_protection_skips",
        "elapsed_ms",
        "requests",
    )
    result = {field: sum(int(phase[field]) for phase in phases) for field in fields}
    result["hit_rate"] = (
        round(result["hits"] / result["requests"], 4) if result["requests"] else 0.0
    )
    return result


def run_pool_experiment(
    pool: BufferPool,
    *,
    heap_pages: list[int],
    index_pages: list[int],
    prime_rounds: int,
    scan_rounds: int,
    probe_rounds: int,
    cycles: int,
    reset: bool,
) -> JsonObject:
    """在指定缓存实例上运行固定的热点-扫描-热点实验。"""

    if len(heap_pages) <= pool.capacity:
        raise YourSQLError(
            f"HEAP 页数为 {len(heap_pages)}，当前缓存为 {pool.capacity} 页，无法制造扫描污染",
            "BAD_REQUEST",
        )
    if len(index_pages) < 2:
        raise YourSQLError("实验至少需要 2 个 INDEX 页", "BAD_REQUEST")
    if reset:
        pool.reset_runtime()
    hot_index_count = min(max(2, pool.capacity // 2 + 3), len(index_pages))
    hot_index_pages = index_pages[:hot_index_count]
    core_index_count = min(max(2, pool.capacity // 2), len(hot_index_pages))
    core_index_pages = hot_index_pages[:core_index_count]
    hot_heap_pages = heap_pages[-min(max(2, pool.capacity // 2), len(heap_pages)) :]
    secondary_index_pages = hot_index_pages[core_index_count:]
    probe_index_pages = list(reversed(core_index_pages)) * 4 + list(
        reversed(secondary_index_pages)
    )
    before_io = pool.disk.io_stats()
    started = perf_counter()
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
    cycle_results: list[JsonObject] = []
    scan_phases: list[JsonObject] = []
    probe_phases: list[JsonObject] = []
    resident_after_scan: list[int] = []
    for cycle in range(1, cycles + 1):
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
        cycle_results.append(
            {
                "cycle": cycle,
                "scan": scan_phase,
                "probe": probe_phase,
                "hot_index_resident_after_scan": resident_after_scan,
            }
        )
    scan = _aggregate(scan_phases)
    probe = _aggregate(probe_phases)
    total = pool.stats().to_dict()
    after_io = pool.disk.io_stats()
    return {
        "policy": pool.replacement_policy,
        "protect_page_types": pool.protect_page_types,
        "capacity": pool.capacity,
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
        "total": total,
        "page_reads": after_io.page_reads - before_io.page_reads,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
    }


def run_buffer_pool_demo(
    disk: DiskManager,
    *,
    active_pool: BufferPool | None = None,
    capacity: int,
    policy: str,
    protect_page_types: bool,
    prime_rounds: int = 3,
    scan_rounds: int = 2,
    probe_rounds: int = 1,
    cycles: int = 2,
    reset: bool = True,
    compare: bool = False,
) -> JsonObject:
    """运行当前开关或四种策略的对照实验。"""

    if not 2 <= prime_rounds <= 100:
        raise YourSQLError("prime_rounds 范围应为 2–100", "BAD_REQUEST")
    if not 1 <= scan_rounds <= 20:
        raise YourSQLError("scan_rounds 范围应为 1–20", "BAD_REQUEST")
    if not 1 <= probe_rounds <= 100:
        raise YourSQLError("probe_rounds 范围应为 1–100", "BAD_REQUEST")
    if not 1 <= cycles <= 50:
        raise YourSQLError("cycles 范围应为 1–50", "BAD_REQUEST")
    heap_pages, index_pages = _inventory(disk)
    if compare:
        results: list[JsonObject] = []
        for name, selected_policy, protection in DEMO_MODES:
            pool = BufferPool(
                disk,
                capacity=capacity,
                replacement_policy=selected_policy,
                protect_page_types=protection,
            )
            try:
                result = run_pool_experiment(
                    pool,
                    heap_pages=heap_pages,
                    index_pages=index_pages,
                    prime_rounds=prime_rounds,
                    scan_rounds=scan_rounds,
                    probe_rounds=probe_rounds,
                    cycles=cycles,
                    reset=True,
                )
                results.append({"mode": name, **result})
            finally:
                pool.close()
        return {
            "kind": "compare",
            "workload": {
                "sequence": "prime core INDEX repeatedly -> hot HEAP working set -> cold secondary INDEX -> scan HEAP -> probe core four times + secondary INDEX",
                "prime_rounds": prime_rounds,
                "scan_rounds": scan_rounds,
                "probe_rounds": probe_rounds,
                "cycles": cycles,
            },
            "results": results,
        }

    pool = active_pool or BufferPool(
        disk,
        capacity=capacity,
        replacement_policy=policy,
        protect_page_types=protect_page_types,
    )
    owns_pool = active_pool is None
    try:
        result = run_pool_experiment(
            pool,
            heap_pages=heap_pages,
            index_pages=index_pages,
            prime_rounds=prime_rounds,
            scan_rounds=scan_rounds,
            probe_rounds=probe_rounds,
            cycles=cycles,
            reset=reset,
        )
    finally:
        if owns_pool:
            pool.close()
    return {
        "kind": "current",
        "workload": {
            "sequence": "prime core INDEX repeatedly -> hot HEAP working set -> cold secondary INDEX -> scan HEAP -> probe core four times + secondary INDEX",
            "prime_rounds": prime_rounds,
            "scan_rounds": scan_rounds,
            "probe_rounds": probe_rounds,
            "cycles": cycles,
        },
        "result": result,
    }

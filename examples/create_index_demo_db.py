"""生成用于讲解索引优化的 YourSQL 演示数据库。"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from pathlib import Path
from time import perf_counter

from yoursql.common import DatabaseConfig
from yoursql.engine.runtime.database import Database


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "show.db"
ROW_COUNT = 200_000
USER_COUNT = 200
DEMO_ORDER_COUNT = 4_000
DEMO_ITEM_COUNT = 12_000
BATCH_SIZE = 4_096
TARGET_CUSTOMER_ID = 4_242

STATUSES = ("pending", "paid", "shipped", "cancelled")


def _batches(
    rows: Iterable[tuple[object, ...]], size: int = BATCH_SIZE
) -> Iterator[list[tuple[object, ...]]]:
    """把数据行分批，避免一次性占用过多内存。"""

    batch: list[tuple[object, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _sales_orders() -> Iterator[tuple[object, ...]]:
    """生成稳定、可重复的订单数据；每个客户恰好有两笔订单。"""

    for identifier in range(1, ROW_COUNT + 1):
        customer_id = (identifier - 1) % 100_000 + 1
        month = (identifier - 1) % 12 + 1
        day = (identifier - 1) % 28 + 1
        yield (
            identifier,
            customer_id,
            f"2025-{month:02d}-{day:02d}",
            round(29.9 + (identifier % 500) * 3.75, 2),
            STATUSES[(identifier * 7) % len(STATUSES)],
            f"批次-{identifier % 100:02d}",
        )


def _demo_users() -> Iterator[tuple[object, ...]]:
    """生成谓词下推、覆盖索引和三表连接使用的用户数据。"""

    for identifier in range(1, USER_COUNT + 1):
        yield (
            identifier,
            f"user-{identifier:03d}",
            16 + (identifier * 13) % 50,
            f"city-{identifier % 8 + 1}",
        )


def _demo_orders() -> Iterator[tuple[object, ...]]:
    """生成订单事实表；user_id 与 order_items_demo.order_id 构成连接链。"""

    for identifier in range(1, DEMO_ORDER_COUNT + 1):
        yield (
            identifier,
            (identifier * 17) % USER_COUNT + 1,
            16 + (identifier * 7) % 50,
            round(39.9 + (identifier % 300) * 8.25, 2),
            f"2025-{(identifier - 1) % 12 + 1:02d}-{(identifier - 1) % 28 + 1:02d}",
        )


def _demo_order_items() -> Iterator[tuple[object, ...]]:
    """生成订单明细；每笔订单稳定对应三条明细。"""

    for identifier in range(1, DEMO_ITEM_COUNT + 1):
        yield (
            identifier,
            (identifier - 1) % DEMO_ORDER_COUNT + 1,
            f"SKU-{(identifier * 19) % 500 + 1:04d}",
            (identifier * 5) % 8 + 1,
        )


def _schema(database: Database) -> None:
    """创建演示表；索引由后续 SQL 分段按需创建。"""

    database.execute(
        """
        CREATE TABLE sales_orders(
            id INT PRIMARY KEY,
            customer_id INT NOT NULL,
            order_date VARCHAR(10) NOT NULL,
            amount FLOAT NOT NULL,
            status VARCHAR(16) NOT NULL,
            note VARCHAR(64)
        );
        CREATE TABLE demo_users(
            id INT NOT NULL,
            name VARCHAR(32) NOT NULL,
            age INT NOT NULL,
            city VARCHAR(32) NOT NULL
        );
        CREATE TABLE demo_orders(
            id INT NOT NULL,
            user_id INT NOT NULL,
            age INT NOT NULL,
            amount FLOAT NOT NULL,
            order_date VARCHAR(10) NOT NULL
        );
        CREATE TABLE demo_order_items(
            id INT NOT NULL,
            order_id INT NOT NULL,
            sku VARCHAR(16) NOT NULL,
            quantity INT NOT NULL
        );
        """
    )


def _load(
    database: Database,
    table_name: str,
    rows: Iterable[tuple[object, ...]],
    expected: int,
) -> int:
    """分批导入一张演示表并返回实际行数。"""

    loaded = 0
    for batch in _batches(rows):
        loaded += database.insert_rows(
            table_name, batch, validate_constraints=False
        ).affected_rows
        if loaded == expected or loaded % 40_000 == 0:
            print(f"  {table_name}: {loaded:,}/{expected:,}")
    return loaded


def build(path: Path, *, force: bool = False) -> dict[str, object]:
    """生成索引对比库并返回摘要。"""

    if path.exists():
        if not force:
            raise FileExistsError(f"{path} 已存在；如需重建请传入 --force")
        if path.resolve() != DEFAULT_DATABASE.resolve():
            raise ValueError(f"--force 只允许清理默认文件 {DEFAULT_DATABASE}")
        path.unlink()

    started = perf_counter()
    config = DatabaseConfig(page_size=4096, buffer_pool_size=128)
    with Database(path, config=config) as database:
        _schema(database)
        loaded = {
            "sales_orders": _load(
                database, "sales_orders", _sales_orders(), ROW_COUNT
            ),
            "demo_users": _load(
                database, "demo_users", _demo_users(), USER_COUNT
            ),
            "demo_orders": _load(
                database, "demo_orders", _demo_orders(), DEMO_ORDER_COUNT
            ),
            "demo_order_items": _load(
                database,
                "demo_order_items",
                _demo_order_items(),
                DEMO_ITEM_COUNT,
            ),
        }
        summary = {
            "database": str(path.resolve()),
            "tables": loaded,
            "target_customer_id": TARGET_CUSTOMER_ID,
            "expected_target_rows": 2,
            "page_size": config.page_size,
            "page_count": database.disk.page_count,
            "elapsed_seconds": round(perf_counter() - started, 2),
        }
        print(summary)
    return summary


def main() -> None:
    """解析命令行参数并生成数据库。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true", help="重建默认演示库")
    args = parser.parse_args()
    build(args.database.resolve(), force=args.force)


if __name__ == "__main__":
    main()

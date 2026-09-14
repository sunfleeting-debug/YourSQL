"""生成覆盖工作台主要能力的 YourSQL 演示数据库。"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from pathlib import Path
from time import perf_counter

from yoursql.common import DatabaseConfig
from yoursql.common.codec import PayloadCodecName, validate_payload_codec
from yoursql.engine.runtime.database import Database


ROOT = Path(__file__).resolve().parents[1]
# HOW：演示库与运行时数据库同放在 data/，.gitignore 用 !data/showcase_v2.db 单独白名单。
DEFAULT_DATABASE = ROOT / "data" / "showcase_v2.db"
BATCH_SIZE = 2_000

CUSTOMER_COUNT = 8_000
PRODUCT_COUNT = 3_000
ORDER_COUNT = 20_000
TICKET_COUNT = 8_000
EVENT_COUNT = 20_000
REVIEW_COUNT = 8_000

REGIONS = ("华东", "华南", "华北", "西南", "西北")
CITIES = ("上海", "深圳", "北京", "成都", "西安", "杭州", "武汉", "南京")
SEGMENTS = ("enterprise", "smb", "consumer", "education", "public")
CATEGORIES = ("storage", "compute", "network", "security", "analytics", "office", "mobile", "display", "audio", "accessory", "service", "training")
ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")
CHANNELS = ("web", "mobile", "partner", "sales")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")
TICKET_STATUSES = ("open", "pending", "resolved", "closed")
EVENT_TYPES = ("login", "search", "view_product", "add_cart", "checkout", "payment", "logout")
EVENT_SOURCES = ("web", "ios", "android", "api", "batch")


def _batches(rows: Iterable[tuple[object, ...]], size: int = BATCH_SIZE) -> Iterator[list[tuple[object, ...]]]:
    """把行流切成较小批次，控制单次导入的内存占用。"""

    batch: list[tuple[object, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load(db: Database, table: str, rows: Iterable[tuple[object, ...]], expected: int) -> None:
    """批量写入一张表，并在终端显示可感知的进度。"""

    loaded = 0
    for batch in _batches(rows):
        # HOW：生成器已在导入前建立唯一索引；索引本身会 O(log n) 拒绝重复键，
        # 不再为每个批次重复回扫大表，常规 SQL INSERT 仍默认完整校验约束。
        loaded += db.insert_rows(table, batch, validate_constraints=False).affected_rows
        if loaded == expected or loaded % 10_000 == 0:
            print(f"  {table}: {loaded:,}/{expected:,}")


def _schema(db: Database) -> None:
    """创建演示数据的关系、约束和默认值。"""

    db.execute_script(
        """
        CREATE TABLE departments(
            id INT PRIMARY KEY,
            code VARCHAR(16) UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            region VARCHAR NOT NULL,
            budget FLOAT DEFAULT 0.0,
            active BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE warehouses(
            id INT PRIMARY KEY,
            code VARCHAR(16) UNIQUE NOT NULL,
            city VARCHAR(40) NOT NULL,
            region VARCHAR NOT NULL,
            capacity INT DEFAULT 0,
            active BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE customers(
            id INT PRIMARY KEY,
            name VARCHAR NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            segment VARCHAR,
            city VARCHAR,
            signup_date VARCHAR,
            credit_limit FLOAT DEFAULT 0.0,
            active BOOLEAN DEFAULT TRUE,
            notes VARCHAR
        );
        CREATE TABLE products(
            id INT PRIMARY KEY,
            sku VARCHAR(32) UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            category VARCHAR,
            price FLOAT NOT NULL,
            cost FLOAT,
            stock INT DEFAULT 0,
            rating FLOAT,
            discontinued BOOLEAN DEFAULT FALSE,
            description VARCHAR
        );
        CREATE TABLE orders(
            id INT PRIMARY KEY,
            customer_id INT NOT NULL,
            warehouse_id INT,
            status VARCHAR DEFAULT 'pending',
            order_date VARCHAR NOT NULL,
            shipped_date VARCHAR,
            total FLOAT DEFAULT 0.0,
            priority INT DEFAULT 3,
            channel VARCHAR,
            notes VARCHAR
        );
        CREATE TABLE order_items(
            id INT PRIMARY KEY,
            order_id INT NOT NULL,
            product_id INT NOT NULL,
            quantity INT DEFAULT 1,
            unit_price FLOAT NOT NULL,
            discount FLOAT DEFAULT 0.0,
            line_total FLOAT NOT NULL,
            fulfilled BOOLEAN DEFAULT FALSE,
            serial_note VARCHAR
        );
        CREATE TABLE support_tickets(
            id INT PRIMARY KEY,
            customer_id INT NOT NULL,
            order_id INT,
            priority VARCHAR DEFAULT 'normal',
            status VARCHAR DEFAULT 'open',
            opened_at VARCHAR NOT NULL,
            resolved_at VARCHAR,
            subject VARCHAR NOT NULL,
            body VARCHAR
        );
        CREATE TABLE events(
            id INT PRIMARY KEY,
            customer_id INT,
            event_type VARCHAR NOT NULL,
            event_date VARCHAR NOT NULL,
            source VARCHAR,
            payload VARCHAR,
            success BOOLEAN DEFAULT TRUE,
            latency_ms INT DEFAULT 0
        );
        CREATE TABLE reviews(
            id INT PRIMARY KEY,
            product_id INT NOT NULL,
            customer_id INT NOT NULL,
            rating INT NOT NULL,
            title VARCHAR NOT NULL,
            body VARCHAR,
            verified BOOLEAN DEFAULT FALSE,
            created_at VARCHAR NOT NULL
        );
        """
    )


def _departments() -> Iterable[tuple[object, ...]]:
    """生成演示部门数据行。"""
    for identifier in range(1, 13):
        yield (
            identifier,
            f"D{identifier:03d}",
            f"{CATEGORIES[(identifier - 1) % len(CATEGORIES)].title()} Department",
            REGIONS[(identifier - 1) % len(REGIONS)],
            round(80_000.0 + identifier * 13_750.25, 2),
            identifier % 7 != 0,
        )


def _warehouses() -> Iterable[tuple[object, ...]]:
    """生成演示仓库数据行。"""
    for identifier in range(1, 25):
        yield (
            identifier,
            f"WH-{identifier:03d}",
            CITIES[(identifier - 1) % len(CITIES)],
            REGIONS[(identifier - 1) % len(REGIONS)],
            10_000 + identifier * 1_250,
            identifier % 11 != 0,
        )


def _customers() -> Iterable[tuple[object, ...]]:
    """生成演示客户数据行。"""
    for identifier in range(1, CUSTOMER_COUNT + 1):
        segment = SEGMENTS[(identifier * 7) % len(SEGMENTS)]
        city = CITIES[(identifier * 11) % len(CITIES)]
        yield (
            identifier,
            f"Customer {identifier:05d}",
            f"customer{identifier:05d}@example.test",
            segment,
            city,
            f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 27 + 1:02d}",
            round(2_000.0 + (identifier % 40) * 275.5, 2),
            identifier % 19 != 0,
            None if identifier % 11 == 0 else f"segment={segment}; preferred_city={city}; contact_window={identifier % 6 + 1}",
        )


def _products() -> Iterable[tuple[object, ...]]:
    """生成演示产品数据行。"""
    for identifier in range(1, PRODUCT_COUNT + 1):
        category = CATEGORIES[(identifier * 5) % len(CATEGORIES)]
        price = round(19.5 + (identifier % 125) * 7.35 + (identifier % 10) * 0.19, 2)
        yield (
            identifier,
            f"SKU-{identifier:06d}",
            f"{category.title()} Item {identifier:04d}",
            category,
            price,
            round(price * (0.48 + (identifier % 9) / 100), 2),
            (identifier * 37) % 4_000,
            round(2.5 + (identifier % 26) / 10, 1),
            identifier % 47 == 0,
            f"{category.title()} product for YourSQL workload {identifier:04d}; "
            "longer text keeps variable length records visible in the page inspector.",
        )


def _orders() -> Iterable[tuple[object, ...]]:
    """生成演示订单数据行。"""
    for identifier in range(1, ORDER_COUNT + 1):
        status = ORDER_STATUSES[(identifier * 3) % len(ORDER_STATUSES)]
        shipped = None if status in {"pending", "cancelled"} else f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 25 + 2:02d}"
        yield (
            identifier,
            (identifier * 17) % CUSTOMER_COUNT + 1,
            (identifier * 19) % 24 + 1,
            status,
            f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 27 + 1:02d}",
            shipped,
            round(45.0 + (identifier % 400) * 12.75, 2),
            identifier % 5 + 1,
            CHANNELS[(identifier * 13) % len(CHANNELS)],
            None if identifier % 9 == 0 else f"order-note-{identifier:05d}; priority={identifier % 5 + 1}",
        )


def _order_items() -> Iterable[tuple[object, ...]]:
    """生成演示订单明细数据行。"""
    item_id = 1
    for order_id in range(1, ORDER_COUNT + 1):
        item_count = 2 + (order_id * 13) % 4
        status = ORDER_STATUSES[(order_id * 3) % len(ORDER_STATUSES)]
        for line_number in range(1, item_count + 1):
            product_id = (order_id * 17 + line_number * 29) % PRODUCT_COUNT + 1
            unit_price = round(19.5 + (product_id % 125) * 7.35 + (product_id % 10) * 0.19, 2)
            quantity = (order_id + line_number) % 4 + 1
            discount = (order_id + line_number) % 5 / 100
            yield (
                item_id,
                order_id,
                product_id,
                quantity,
                unit_price,
                discount,
                round(unit_price * quantity * (1.0 - discount), 2),
                status in {"paid", "shipped"},
                None if item_id % 23 else f"serial-batch-{item_id % 97:02d}",
            )
            item_id += 1


def _tickets() -> Iterable[tuple[object, ...]]:
    """生成演示工单数据行。"""
    for identifier in range(1, TICKET_COUNT + 1):
        status = TICKET_STATUSES[(identifier * 5) % len(TICKET_STATUSES)]
        resolved = None if status in {"open", "pending"} else f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 25 + 2:02d}"
        yield (
            identifier,
            (identifier * 23) % CUSTOMER_COUNT + 1,
            None if identifier % 8 == 0 else (identifier * 7) % ORDER_COUNT + 1,
            TICKET_PRIORITIES[(identifier * 3) % len(TICKET_PRIORITIES)],
            status,
            f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 27 + 1:02d}",
            resolved,
            f"Ticket {identifier:05d}: service request for order workflow",
            "Customer reported a reproducible issue while using the demo workload. "
            "This deliberately verbose body exercises variable length records, NULL resolution dates, "
            "and text filtering in the workbench.",
        )


def _events() -> Iterable[tuple[object, ...]]:
    """生成演示事件数据行。"""
    for identifier in range(1, EVENT_COUNT + 1):
        event_type = EVENT_TYPES[(identifier * 7) % len(EVENT_TYPES)]
        yield (
            identifier,
            None if identifier % 13 == 0 else (identifier * 29) % CUSTOMER_COUNT + 1,
            event_type,
            f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 27 + 1:02d}",
            EVENT_SOURCES[(identifier * 11) % len(EVENT_SOURCES)],
            f"{{\"event\":\"{event_type}\",\"attempt\":{identifier % 4},\"trace\":\"evt-{identifier:08d}\",\"detail\":\"synthetic payload for storage inspection\"}}",
            identifier % 17 != 0,
            15 + (identifier * 31) % 2_000,
        )


def _reviews() -> Iterable[tuple[object, ...]]:
    """生成演示评价数据行。"""
    for identifier in range(1, REVIEW_COUNT + 1):
        rating = (identifier * 7) % 5 + 1
        yield (
            identifier,
            (identifier * 17) % PRODUCT_COUNT + 1,
            (identifier * 31) % CUSTOMER_COUNT + 1,
            rating,
            f"Review {identifier:05d} rating={rating}",
            None if identifier % 29 == 0 else "The product matched the documented workload and remained stable during repeated queries. "
            "Useful text for LIKE, LENGTH and NULL examples.",
            identifier % 6 != 0,
            f"202{identifier % 5}-{identifier % 12 + 1:02d}-{identifier % 27 + 1:02d}",
        )


def _remove_database(path: Path) -> None:
    """只允许删除脚本默认生成的演示库，避免误删其它数据库。"""

    resolved = path.resolve()
    if resolved != DEFAULT_DATABASE.resolve():
        raise ValueError(f"--force 只允许清理默认文件 {DEFAULT_DATABASE}")
    if resolved.exists():
        resolved.unlink()


def build(
    path: Path,
    *,
    force: bool = False,
    payload_codec: PayloadCodecName = "json",
) -> dict[str, object]:
    """生成数据库并返回便于 README 引用的统计摘要。"""

    if path.exists():
        if not force:
            raise FileExistsError(f"{path} 已存在；如需重建请传入 --force")
        _remove_database(path)

    started = perf_counter()
    # 保留教学默认页大小，便于工作台观察更多 HEAP 和 INDEX 页面。
    config = DatabaseConfig(
        page_size=4096,
        buffer_pool_size=128,
        payload_codec=validate_payload_codec(payload_codec),
    )
    with Database(path, config=config) as db:
        print(f"创建演示数据库: {path}")
        _schema(db)

        # 先建索引再批量导入，让主键/唯一键校验走 B+Tree，避免大表逐行全表扫描。
        db.execute_script(
            """
            CREATE UNIQUE INDEX uq_departments_code ON departments (code);
            CREATE UNIQUE INDEX uq_warehouses_code ON warehouses (code);
            CREATE UNIQUE INDEX uq_customers_email ON customers (email);
            CREATE UNIQUE INDEX uq_products_sku ON products (sku);
            """
        )
        _load(db, "departments", _departments(), 12)
        _load(db, "warehouses", _warehouses(), 24)
        _load(db, "customers", _customers(), CUSTOMER_COUNT)
        _load(db, "products", _products(), PRODUCT_COUNT)
        _load(db, "orders", _orders(), ORDER_COUNT)
        _load(db, "order_items", _order_items(), 70_000)
        _load(db, "support_tickets", _tickets(), TICKET_COUNT)
        _load(db, "events", _events(), EVENT_COUNT)
        _load(db, "reviews", _reviews(), REVIEW_COUNT)

        # 非唯一索引在数据装载后批量构建，避免逐行维护大量低基数键。
        db.execute_script(
            """
            CREATE INDEX idx_orders_status ON orders (status);
            CREATE INDEX idx_orders_customer_status ON orders (customer_id, status);
            CREATE INDEX idx_products_category ON products (category);
            CREATE INDEX idx_tickets_priority ON support_tickets (priority);
            CREATE INDEX idx_events_type ON events (event_type);
            CREATE INDEX idx_reviews_rating ON reviews (rating);
            CREATE INDEX idx_order_items_order_product ON order_items (order_id, product_id);
            """
        )

        db.execute_script(
            """
            CREATE VIEW active_customers AS
                SELECT id, name, segment, city, credit_limit
                FROM customers
                WHERE active = TRUE;
            CREATE VIEW order_status_summary AS
                SELECT status, count(*) AS order_count, sum(total) AS gross_total, avg(total) AS average_total
                FROM orders
                GROUP BY status;
            CREATE VIEW customer_order_totals AS
                SELECT c.id AS customer_id, c.name AS customer_name,
                       count(o.id) AS order_count, sum(o.total) AS gross_total
                FROM customers AS c
                LEFT JOIN orders AS o ON c.id = o.customer_id
                GROUP BY c.id, c.name;
            CREATE ROLE analyst;
            CREATE ROLE support;
            CREATE ROLE auditor;
            CREATE USER analyst IDENTIFIED BY 'analyst' DEFAULT ROLE analyst;
            CREATE USER support IDENTIFIED BY 'support' DEFAULT ROLE support;
            CREATE USER auditor IDENTIFIED BY 'auditor' DEFAULT ROLE auditor;
            GRANT SELECT ON * TO ROLE analyst;
            GRANT SELECT ON * TO ROLE auditor;
            GRANT SELECT ON customers TO ROLE support;
            GRANT SELECT ON orders TO ROLE support;
            GRANT SELECT ON support_tickets TO ROLE support;
            GRANT SELECT ON active_customers TO ROLE support;
            GRANT INSERT, UPDATE ON support_tickets TO ROLE support;
            """
        )
        summary = {
            "database": str(path.resolve()),
            "page_size": config.page_size,
            "page_count": db.disk.page_count,
            "tables": len(db.catalog),
            "indexes": len(db.catalog.indexes()),
            "rows": {
                "departments": 12,
                "warehouses": 24,
                "customers": CUSTOMER_COUNT,
                "products": PRODUCT_COUNT,
                "orders": ORDER_COUNT,
                "order_items": "约 70,000（每单 2~5 行）",
                "support_tickets": TICKET_COUNT,
                "events": EVENT_COUNT,
                "reviews": REVIEW_COUNT,
            },
            "elapsed_seconds": round(perf_counter() - started, 2),
        }
        print(summary)
    return summary


def main() -> None:
    """解析命令行参数并启动当前脚本任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true", help="重建允许的演示库文件")
    parser.add_argument(
        "--payload-codec",
        choices=("json", "manual"),
        default="json",
        help="页内 payload 编码；manual 使用 YSPL 手写编解码",
    )
    args = parser.parse_args()
    build(
        args.database.resolve(),
        force=args.force,
        payload_codec=args.payload_codec,
    )


if __name__ == "__main__":
    main()

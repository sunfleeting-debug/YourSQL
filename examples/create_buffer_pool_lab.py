"""生成专门观察 Buffer Pool 淘汰行为的小型实验数据库。"""

from __future__ import annotations

import argparse
from pathlib import Path

from yoursql.common import DatabaseConfig
from yoursql.engine.runtime.database import Database


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "buffer_pool_lab_large.db"
DEFAULT_ROWS = 16_000
PAYLOAD_REPEAT = 30


def _rows(count: int) -> list[tuple[int, str]]:
    """生成确定性的宽行，让实验库拥有足够多的 HEAP 页。"""

    return [
        (
            row_id,
            f"buffer-lab-row-{row_id:05d}-" + ("payload-" * PAYLOAD_REPEAT),
        )
        for row_id in range(1, count + 1)
    ]


def create_database(path: Path, *, rows: int = DEFAULT_ROWS, force: bool = False) -> None:
    """创建 Buffer Pool 实验库。"""

    if rows < 512:
        raise ValueError("实验行数至少为 512，才能形成明显的顺序扫描工作集")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not force:
            raise FileExistsError(f"数据库已存在，请增加 --force：{path}")
        path.unlink()

    # HOW：实验库使用小页和较宽记录，保证少量缓存页就能制造“热点索引 + 大量 HEAP 页”冲突。
    config = DatabaseConfig(
        page_size=1024,
        buffer_pool_size=64,
        replacement_policy="lru",
    )
    with Database(path, config=config) as database:
        database.execute(
            "CREATE TABLE buffer_lab(id INT PRIMARY KEY, payload VARCHAR NOT NULL);"
        )
        database.insert_rows("buffer_lab", _rows(rows), validate_constraints=False)
        database.execute("CREATE INDEX idx_buffer_lab_id ON buffer_lab (id);")
        table = database.catalog.get_table("buffer_lab")
        index = database.index_manager.get("idx_buffer_lab_id")
        print(f"created: {path}")
        print(f"rows: {rows}")
        print(f"heap_pages: {len(table.page_ids)}")
        print(f"index_pages: {len(index.physical_page_ids(readonly=True))}")
        print("database_page_size: 1024")
        print("recommended_capacity: 32")


def main() -> None:
    """解析参数并创建实验库。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--force", action="store_true", help="覆盖已有实验库")
    args = parser.parse_args()
    create_database(args.database, rows=args.rows, force=args.force)


if __name__ == "__main__":
    main()

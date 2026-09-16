"""生成用于观察页式存储和 Buffer Pool 的演示数据库。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from yoursql.common.config import DatabaseConfig
from yoursql.engine.runtime.database import Database


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "show_storage.db"
PAGE_SIZE = 1024


def _repeat(letter: str, length: int) -> str:
    """生成不含引号的确定性字符串，便于直接编码进记录。"""

    return letter * length


def _slot_rows() -> list[tuple[int, str, str]]:
    """生成同一页内大小交错的记录，删除中间槽后会形成碎片。"""

    sizes = [20, 100, 36, 150, 48, 120, 28, 90, 42, 110, 34, 80]
    return [
        (row_id, "keep", _repeat(chr(64 + row_id), size))
        for row_id, size in enumerate(sizes, start=1)
    ]


def _index_rows(count: int) -> list[tuple[int, int, str]]:
    """生成可观察索引页、叶子链和顺序扫描的业务记录。"""

    return [
        (row_id, row_id % 12, f"index-row-{row_id:04d}-" + _repeat("I", 70))
        for row_id in range(1, count + 1)
    ]


def _cache_rows(count: int) -> list[tuple[int, str]]:
    """生成足以超过小缓存工作集的宽行。"""

    return [
        (row_id, f"cache-row-{row_id:04d}-" + _repeat("C", 165))
        for row_id in range(1, count + 1)
    ]


def _free_seed_rows(count: int) -> list[tuple[int, str]]:
    """生成随后整体删除的页，用于留下可复用 FREE 链。"""

    return [
        (row_id, f"released-row-{row_id:04d}-" + _repeat("F", 180))
        for row_id in range(1, count + 1)
    ]


def _remove_existing(path: Path) -> None:
    """清理本次生成明确指定的演示文件。"""

    backup = path.with_name(f"{path.name}.bak")
    for candidate in (path, backup, Path(f"{path}.wal")):
        if candidate.exists():
            candidate.unlink()


def create_database(path: Path, *, force: bool = False) -> tuple[Path, Path]:
    """创建正式演示库并返回主库、备份路径。"""

    path = path.resolve()
    backup = path.with_name(f"{path.name}.bak")
    if path.exists() or backup.exists():
        if not force:
            raise FileExistsError(f"数据库或备份已存在，请增加 --force：{path}")
        _remove_existing(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    config = DatabaseConfig(
        page_size=PAGE_SIZE,
        buffer_pool_size=16,
        replacement_policy="2q",
        protect_page_types=True,
    )
    with Database(path, config=config) as database:
        database.execute(
            "CREATE TABLE slot_lab(id INT PRIMARY KEY, kind VARCHAR(20), payload VARCHAR(200));"
        )
        database.insert_rows("slot_lab", _slot_rows(), validate_constraints=False)
        # HOW：保留奇数槽，删除偶数槽，确保页面检查中同时看到槽复用和多个空闲片段。
        database.execute("DELETE FROM slot_lab WHERE id IN (2, 4, 6, 8);")

        database.execute(
            "CREATE TABLE index_lab(id INT PRIMARY KEY, group_id INT, payload VARCHAR(120));"
        )
        database.insert_rows("index_lab", _index_rows(180), validate_constraints=False)
        database.execute("CREATE INDEX idx_index_lab_group ON index_lab (group_id);")
        database.execute("CREATE INDEX idx_index_lab_id ON index_lab (id);")

        database.execute("CREATE TABLE cache_lab(id INT PRIMARY KEY, payload VARCHAR(220));")
        database.insert_rows("cache_lab", _cache_rows(256), validate_constraints=False)
        database.execute("CREATE INDEX idx_cache_lab_id ON cache_lab (id);")

        database.execute(
            "CREATE TABLE txn_lab(id INT PRIMARY KEY, status VARCHAR(20), note VARCHAR(120));"
        )
        database.insert_rows(
            "txn_lab",
            [
                (1, "stable", "rollback keeps this row unchanged"),
                (2, "stable", "update this row inside BEGIN for WAL demo"),
                (3, "stable", "delete and rollback this row for comparison"),
            ],
            validate_constraints=False,
        )

        database.execute(
            "CREATE TABLE free_seed(id INT PRIMARY KEY, payload VARCHAR(220));"
        )
        database.insert_rows("free_seed", _free_seed_rows(192), validate_constraints=False)
        database.execute("CREATE INDEX idx_free_seed_id ON free_seed (id);")
        # WHY：自动提交后立即释放堆页和索引页，打开演示库即可观察 FREE 链表。
        database.execute("DROP TABLE free_seed;")

    shutil.copy2(path, backup)
    return path, backup


def main() -> None:
    """解析参数并生成演示文件。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true", help="覆盖指定的演示库和 .bak")
    args = parser.parse_args()
    database, backup = create_database(args.database, force=args.force)
    print(f"created: {database}")
    print(f"backup:  {backup}")
    print(f"page_size: {PAGE_SIZE}")


if __name__ == "__main__":
    main()

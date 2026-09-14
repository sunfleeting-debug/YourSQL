"""B+Tree 物理页枚举和树快照逻辑。"""

from __future__ import annotations

from yoursql.common.types import RowId
from yoursql.storage.index.ordering import Key, _compare_keys, _memory_key
from yoursql.storage.index.protocols import _TreeContext
from yoursql.storage.index.types import (
    BPlusTreeSnapshot,
    IndexNodeSnapshot,
    IndexSnapshotEntry,
)


class _TreeInspectionMixin(_TreeContext):
    """负责物理页枚举和索引快照。"""

    def _physical_page_ids_unlocked(self, *, readonly: bool = False) -> tuple[int, ...]:
        """返回无需再次加锁的索引物理页号。"""
        if self._root_page_id is None:
            return ()
        queue = [self._root_page_id]
        result: list[int] = []
        visited: set[int] = set()
        while queue:
            page_id = queue.pop(0)
            if page_id in visited or not self._valid_index_page(page_id):
                continue
            visited.add(page_id)
            result.append(page_id)
            node = self._read_node(page_id, readonly=readonly)
            if not node.leaf:
                queue.extend(node.children)
        return tuple(result)

    def physical_page_ids(self, *, readonly: bool = False) -> tuple[int, ...]:
        """返回索引物理页；只读检查可避免触碰 BufferPool 的访问顺序。"""

        with self._lock:
            if not self._persistent:
                return ()
            return self._physical_page_ids_unlocked(readonly=readonly)

    def snapshot(self, offset: int = 0, limit: int = 100) -> BPlusTreeSnapshot:
        """按键分页返回真实叶子记录，并附带物理节点摘要。"""

        safe_offset = max(0, int(offset))
        safe_limit = max(1, int(limit))
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                entries: list[IndexSnapshotEntry] = []
                for key in self._keys[safe_offset : safe_offset + safe_limit]:
                    values = self._values[_memory_key(key)]
                    entries.append(
                        IndexSnapshotEntry(
                            key=key,
                            row_ids=tuple(sorted(values)[:100]),
                            row_count=len(values),
                            rows_truncated=len(values) > 100,
                        )
                    )
                return BPlusTreeSnapshot(
                    entries=tuple(entries),
                    total=len(self._keys),
                    offset=safe_offset,
                    limit=safe_limit,
                    representation="ordered_leaf_array",
                    unique=self.unique,
                    physical=False,
                    format="memory_ordered_leaf",
                )
            entries = []
            total = 0
            current_key: Key | None = None
            current_rows: list[RowId] = []
            current_row_count = 0
            for entry in self._iter_entries(readonly=True):
                if current_key is None or _compare_keys(current_key, entry.key) != 0:
                    if current_key is not None:
                        if safe_offset <= total < safe_offset + safe_limit:
                            entries.append(
                                IndexSnapshotEntry(
                                    key=current_key,
                                    row_ids=tuple(current_rows[:100]),
                                    row_count=current_row_count,
                                    rows_truncated=current_row_count > 100,
                                )
                            )
                        total += 1
                    current_key = entry.key
                    current_rows = [entry.row_id]
                    current_row_count = 1
                else:
                    current_row_count += 1
                    if len(current_rows) < 101:
                        current_rows.append(entry.row_id)
            if current_key is not None:
                if safe_offset <= total < safe_offset + safe_limit:
                    entries.append(
                        IndexSnapshotEntry(
                            key=current_key,
                            row_ids=tuple(current_rows[:100]),
                            row_count=current_row_count,
                            rows_truncated=current_row_count > 100,
                        )
                    )
                total += 1
            page_ids = self._physical_page_ids_unlocked(readonly=True)
            nodes: list[IndexNodeSnapshot] = []
            for page_id in page_ids[:256]:
                node = self._read_node(page_id, readonly=True)
                nodes.append(
                    IndexNodeSnapshot(
                        page_id=node.page_id,
                        node_type="leaf" if node.leaf else "internal",
                        level=node.level,
                        parent_page_id=node.parent,
                        next_page_id=node.next_page,
                        prev_page_id=node.prev_page,
                        key_count=len(node.keys),
                        child_count=len(node.children),
                        children=tuple(node.children) if not node.leaf else (),
                        min_key=self._load_min_key(page_id, readonly=True)
                        if node.keys
                        else None,
                        max_key=self._load_max_key(page_id, readonly=True)
                        if node.keys
                        else None,
                    )
                )
            root = (
                self._read_node(self._root_page_id, readonly=True)
                if self._root_page_id is not None
                else None
            )
            return BPlusTreeSnapshot(
                entries=tuple(entries),
                total=total,
                offset=safe_offset,
                limit=safe_limit,
                # 该字段是旧工作台 API 的兼容值；physical/format 才是新语义。
                representation="ordered_leaf_array",
                format="disk_bplus_tree_v1",
                physical=True,
                unique=self.unique,
                root_page_id=self._root_page_id,
                height=0 if root is None else root.level + 1,
                page_count=len(page_ids),
                page_ids=tuple(page_ids[:256]),
                # 页面地图需要把当前索引的所有物理页和表绑定，不能复用节点图的 256 页展示上限。
                all_page_ids=tuple(page_ids),
                pages_truncated=len(page_ids) > 256,
                nodes=tuple(nodes),
                limitation="索引键、RowId、内部节点和叶子链均已落盘；单页采用可检查的 MBIX JSON 编码。",
            )

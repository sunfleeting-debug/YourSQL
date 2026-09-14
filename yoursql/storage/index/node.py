"""单个 INDEX 页节点的内存模型和 MBIX 编解码。"""

from __future__ import annotations

from dataclasses import dataclass, field

from yoursql.common.codec import PayloadCodec, PayloadCodecError, decode_payload
from yoursql.common.codec import payload_codec as get_payload_codec
from yoursql.common.errors import StorageError
from yoursql.common.types import PageId, RowId
from yoursql.storage.index.codec import INDEX_MAGIC, INDEX_VERSION
from yoursql.storage.index.ordering import Key, _compare_entries, _compare_keys
from yoursql.storage.index.types import IndexPayloadEntry
from yoursql.storage.page import Page, PageType


@dataclass
class _IndexNode:
    """内存中的一个索引页快照。"""

    page_id: int
    leaf: bool
    level: int = 0
    parent: int | None = None
    next_page: int | None = None
    prev_page: int | None = None
    keys: list[Key] = field(default_factory=list)
    row_ids: list[RowId] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    # HOW：覆盖索引（INCLUDE 列）的条目携带值；与 keys/row_ids 平行，纯键索引时为空。
    payloads: list[list[object]] = field(default_factory=list)

    def ensure_payloads(self) -> None:
        """把 payloads 补齐到与 keys 等长，保持三个数组始终平行。"""

        if len(self.payloads) != len(self.keys):
            self.payloads = [
                self.payloads[index] if index < len(self.payloads) else []
                for index in range(len(self.keys))
            ]

    def payload_at(self, position: int) -> list[object]:
        """读取指定索引条目的覆盖列载荷。"""
        return self.payloads[position] if position < len(self.payloads) else []

    def insert_entry(
        self,
        position: int,
        key: Key,
        row_id: RowId,
        payload: list[object] | None = None,
    ) -> None:
        """插入条目；payload 为空时占位空列表，保持与 keys/row_ids 平行。"""

        self.ensure_payloads()
        self.keys.insert(position, key)
        self.row_ids.insert(position, row_id)
        self.payloads.insert(position, list(payload) if payload else [])

    def pop_entry(self, position: int = -1) -> IndexPayloadEntry:
        """弹出条目（默认末尾），连同覆盖列值一起返回。"""

        self.ensure_payloads()
        return IndexPayloadEntry(
            self.keys.pop(position),
            self.row_ids.pop(position),
            self.payloads.pop(position),
        )

    def extend_from(self, other: "_IndexNode") -> None:
        """追加另一个叶页的条目（合并时用）。"""

        self.ensure_payloads()
        other.ensure_payloads()
        self.keys.extend(other.keys)
        self.row_ids.extend(other.row_ids)
        self.payloads.extend(other.payloads)

    def prepend_from(self, other: "_IndexNode") -> None:
        """在头部插入另一个叶页的条目（合并时用）。"""

        self.ensure_payloads()
        other.ensure_payloads()
        self.keys[:0] = other.keys
        self.row_ids[:0] = other.row_ids
        self.payloads[:0] = other.payloads

    def validate(self) -> None:
        """校验索引页或索引节点的结构完整性。"""
        if self.leaf:
            if len(self.keys) != len(self.row_ids):
                raise StorageError(f"索引叶页 {self.page_id} 的 key/RowId 数量不一致")
            if self.payloads and len(self.payloads) != len(self.keys):
                raise StorageError(
                    f"索引叶页 {self.page_id} 的覆盖列值与 key 数量不一致"
                )
            if self.children:
                raise StorageError(f"索引叶页 {self.page_id} 不应包含子页")
            for left_key, right_key, left_row, right_row in zip(
                self.keys, self.keys[1:], self.row_ids, self.row_ids[1:], strict=False
            ):
                if _compare_entries(left_key, left_row, right_key, right_row) > 0:
                    raise StorageError(
                        f"索引叶页 {self.page_id} 的条目未按 key/RowId 排序"
                    )
        elif len(self.children) != len(self.keys) + 1:
            raise StorageError(f"索引内部页 {self.page_id} 的子页数量非法")
        elif any(
            _compare_keys(left, right) > 0
            for left, right in zip(self.keys, self.keys[1:], strict=False)
        ):
            raise StorageError(f"索引内部页 {self.page_id} 的分隔键未排序")

    def payload(self, codec: PayloadCodec | str = "json") -> bytes:
        """返回索引条目的覆盖列载荷。"""
        self.validate()
        value: dict[str, object] = {
            "version": INDEX_VERSION,
            "kind": "leaf" if self.leaf else "internal",
            "level": self.level,
            "parent": self.parent,
            "next": self.next_page,
            "prev": self.prev_page,
            "keys": [list(key) for key in self.keys],
        }
        if self.leaf:
            value["row_ids"] = [
                [int(row_id.page_id), row_id.slot_id] for row_id in self.row_ids
            ]
            # HOW：只有覆盖索引才写入 payloads，纯键索引的页布局与旧版完全一致。
            if self.payloads and any(payload for payload in self.payloads):
                self.ensure_payloads()
                value["payloads"] = [list(payload) for payload in self.payloads]
        else:
            value["children"] = list(self.children)
        selected = (
            codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
        )
        try:
            encoded = selected.encode(value)
        except (PayloadCodecError, TypeError, ValueError) as exc:
            raise StorageError(f"索引页 {self.page_id} 含无法序列化的键值") from exc
        return INDEX_MAGIC + encoded

    @classmethod
    def from_page(
        cls, page: Page, preferred: PayloadCodec | str | None = None
    ) -> "_IndexNode":
        """从数据库页恢复索引节点。"""
        if page.page_type is not PageType.INDEX:
            raise StorageError(f"页 {page.page_id} 不是 INDEX 页")
        if not page.payload:
            raise StorageError(f"索引页 {page.page_id} 为空")
        if not page.payload.startswith(INDEX_MAGIC):
            raise StorageError(f"索引页 {page.page_id} 的格式版本不受支持")
        try:
            raw, _codec = decode_payload(page.payload[len(INDEX_MAGIC) :], preferred)
            if not isinstance(raw, dict) or int(raw.get("version", 0)) != INDEX_VERSION:
                raise ValueError("版本不匹配")
            kind = str(raw.get("kind"))
            if kind not in {"leaf", "internal"}:
                raise ValueError("节点类型非法")
            keys_raw = raw.get("keys", [])
            if not isinstance(keys_raw, list) or any(
                not isinstance(item, list) for item in keys_raw
            ):
                raise ValueError("keys 不是数组")
            keys = [tuple(item) for item in keys_raw]
            parent = raw.get("parent")
            next_page = raw.get("next")
            prev_page = raw.get("prev")
            node = cls(
                page_id=page.page_id,
                leaf=kind == "leaf",
                level=int(raw.get("level", 0)),
                parent=None if parent is None else int(parent),
                next_page=None if next_page is None else int(next_page),
                prev_page=None if prev_page is None else int(prev_page),
                keys=keys,
            )
            if node.leaf:
                raw_row_ids = raw.get("row_ids", [])
                if not isinstance(raw_row_ids, list):
                    raise ValueError("row_ids 不是数组")
                node.row_ids = [
                    RowId(PageId(int(item[0])), int(item[1])) for item in raw_row_ids
                ]
                # HOW：覆盖索引才有 payloads；旧页与纯键索引没有该字段，保持空列表。
                raw_payloads = raw.get("payloads", [])
                if not isinstance(raw_payloads, list):
                    raise ValueError("payloads 不是数组")
                node.payloads = [list(item) for item in raw_payloads]
            else:
                raw_children = raw.get("children", [])
                if not isinstance(raw_children, list):
                    raise ValueError("children 不是数组")
                node.children = [int(item) for item in raw_children]
            node.validate()
            return node
        except (
            UnicodeDecodeError,
            PayloadCodecError,
            TypeError,
            ValueError,
            KeyError,
            IndexError,
        ) as exc:
            raise StorageError(f"索引页 {page.page_id} 内容损坏") from exc

# Index 接口

`index` 对外主要提供 `IndexManager` 和 `BPlusTree`。前者按名称管理索引，后者负责索引键与 `RowId` 的查询和维护。节点、页布局、分裂合并和编码均由内部完成。

## IndexManager

- `create(name, ...)`：创建并登记内存或持久化索引。
- `get(name)`：按名称获取 `BPlusTree`。
- `drop(name)`：移除名称登记；需要释放持久化页面时继续调用返回树的 `destroy()`。
- `items()`：返回当前登记项的快照。

## BPlusTree

### 查询

- `search(key)`：精确查找，返回 `tuple[RowId, ...]`。
- `search_entries(key)`：精确查找，返回 `IndexPayloadEntry`；其中 `payload` 按 INCLUDE 列顺序排列。
- `range_scan(low, high, ...)`：按键范围返回 `IndexEntry`。
- `range_scan_entries(low, high, ...)`：按键范围返回带 payload 的条目。
- `prefix_scan(prefix)`：扫描联合索引前缀。
- `range_scan_prefix(prefix, low, high, ...)`：扫描前缀后的范围。
- `range_scan_prefix_entries(...)`：前缀范围扫描并返回 payload。
- `all_items()`：完整物化索引条目，适合检查或小型索引。

`key` 可以是单列标量，也可以是按联合索引列顺序排列的元组。`low` 和 `high` 为空表示不限制对应边界；`include_low`、`include_high` 控制边界是否包含。

### 修改

- `insert(key, row_id, payload=None)`：插入一条索引记录；`payload` 只用于持久化覆盖索引。
- `delete(key, row_id=None)`：删除指定 RowId；省略 `row_id` 时删除该 key 的全部记录。
- `bulk_load(entries)`：消费一批条目并重建索引，支持 `(key, row_id)`、`(key, row_id, payload)` 和 `IndexPayloadEntry`。
- `destroy()`：销毁索引并释放持久化节点页。

`IndexEntry` 表示 `key + RowId`；`IndexPayloadEntry` 额外携带 INCLUDE 列值。索引维护由上层在 heap 记录变更后协调，索引本身不负责读取完整表记录。

# Storage 模块接口（简版）

`storage` 对上层主要提供两类对象：`TableHeap` 负责表记录，`BPlusTree` 负责索引。页布局、槽位、编码、缓存协作和压缩过程都在内部完成。

## `TableHeap`

堆表是记录读写入口。运行时创建堆表并传入已有页信息，上层只按记录接口操作：

- `insert(row) -> RowId`
- `append_batch(rows) -> list[RowId]`
- `read(row_id)`
- `update(row_id, row)`
- `delete(row_id)`
- `scan()`
- `count()`

删除记录后，命令层会在需要时调用 `reclaim_empty_pages(page_ids)` 回收空页，并使用 `page_ids` 同步表元数据。这属于页生命周期协调，不涉及记录格式。

## `IndexManager` / `BPlusTree`

详见[README.md](./index/README.md)

## 生命周期约定

- `RowId` 是存储定位符，不是用户层主键。
- 记录变更后的索引维护由上层协调。
- `BufferPool` 由运行时持有；需要时使用 `flush_all()` 刷盘、`delete_page(page_id)` 清理页。
- `DiskManager`、`Page`、`SlottedPage` 以及带下划线的方法是存储内部实现，不作为业务层依赖。
- `StorageError` 表示存储操作失败，由上层事务边界决定如何处理。

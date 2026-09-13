# YourSQL 系统架构与验收边界

## 数据流

```text
CLI / Python API / HTTP / SSH stdio
                 |
       Session + RBAC
                 |
 Lexer -> Parser/AST -> Binder -> Plan/Optimizer
                 |
        Volcano Executor / SQL evaluator
                 |
 Catalog + TableHeap + BPlusTree
                 |
 Page -> BufferPool(LRU/FIFO) -> DiskManager
```

模块边界：`common` 只放稳定类型、配置与错误；`sql` 只负责编译和计划，不修改 Catalog；`storage` 负责固定页、槽式记录、缓存与索引；`engine` 负责目录、权限、会话、执行与服务协议。

## 页面与文件格式

- **页与 superblock**：默认 4096 字节页（仓库交付的 `data/showcase_v2.db` 同为 4096），页大小由 superblock 自动识别。第 0 页是带 CRC 的 superblock；页头 30 字节，末尾 4 字节是对齐保留区，使页内 6 字节槽目录项按视觉格对齐。
- **Catalog**：可链式 `CATALOG` 页保存 JSON 元数据；表数据用 `HEAP` 页保存 JSON 编码记录。
- **HEAP 负载**：`MSP2` 双向槽式布局，槽目录从页内头向后增长、记录区从页尾向前增长，槽项保存记录偏移、长度与删除标记，空闲区在两者之间。
- **DML 行为**：优先在现有空闲片段中增量分配，保持未受影响记录的物理 offset；定长缩短更新原地写入；删除保留槽号；只有找不到足够大的连续片段时才压缩当前页。
- **索引**：落盘 B+Tree，Catalog 保存根页号，`INDEX` 页保存叶子 key/RowId、内部节点分隔键、父子页关系与叶子链。创建大索引时按排序输入批量装载，DML 通过 BufferPool 增量维护，并在页容量变化时分裂、借位、合并。
- **权限目录**：`_sys_users`、`_sys_roles`、`_sys_role_members`、`_sys_privileges` 四张标记为 system 的隐藏 Heap 表；Catalog 只保存它们的定义与页链。

## 查询与优化

- **语法覆盖**：核心 DDL/DML，只读 `CREATE/DROP VIEW ... AS SELECT`，`WHERE`、`ORDER BY`、`LIMIT/OFFSET`、`DISTINCT`、`JOIN`、`GROUP BY`、`HAVING`、聚合、`IN` 子查询、`UNION`、`UPDATE`、索引、`DESC/DESCRIBE`、`SHOW COLUMNS/FIELDS`、`SHOW TABLES/VIEWS`、`SHOW INDEX/INDEXES`、`SHOW CREATE TABLE/VIEW`、`EXPLAIN`。
- **Binder**：检查目录中的表/视图/列、值数量、字面量类型与歧义引用。
- **视图**：只在 Catalog 保存定义 SQL 与输出 Schema；执行器把视图定义查询产生的内存行作为外层扫描输入，首版不支持任何视图写操作。
- **执行器**：Database 使用 Volcano 风格算子契约，并在满足联合索引最左前缀的等值、范围、`IN`、`BETWEEN` 及 AND/OR 组合谓词上选择 IndexScan。
- **单表选择规则**：小表（不超过 128 行）优先复用可用索引；大表在索引候选行数为 0 或不超过总行数 20% 时用 IndexScan，否则回退 SeqScan。该规则尤其针对 `SELECT *` 的回表成本。优化器先按索引元数据生成候选计划，再用当前 B+Tree 的候选行数校正大表单表计划；写入、删除与 DDL 会失效计划缓存，确保分布变化后重新判断。
- **持久化**：每个写语句完成后立即持久化目录与脏页；重新打开数据库时，Catalog、表数据与索引都直接从页面文件读回。

## 权限目录

四张 `_sys_*` Heap 表保持内部隐藏；`sys_users`、`sys_roles`、`sys_role_members`、`sys_privileges` 是 admin-only 的只读系统视图，访问时回读内部表且不分配独立数据页。`sys_users` 只公开用户名，不公开密码哈希；普通用户即使拥有全库 `SELECT` 也无法访问这些系统视图。

## 验收命令

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
& '.venv\Scripts\python.exe' -m benchmarks.run_benchbox_tpch --iterations 5 --force
& '.venv\Scripts\python.exe' -m benchmarks.compare_tpch_q6 --iterations 5
& '.venv\Scripts\python.exe' -m yoursql.cli --database data/showcase_v2.db --sql "SHOW TABLES; SHOW VIEWS;"
```

单元测试使用临时目录。第三方 benchmark 的 TPC-H 数据与数据库写入 `benchmarks/third_party/`、`benchmarks/results/`，跑分摘要写入 `benchmarks/reports/tpch_sf001_q6.json`（BenchBox 适配器口径）与 `benchmarks/reports/tpch_sf001_q6_engines.json`（SQLite/DuckDB 对照，裸 `execute()` 口径）；前两者不会进入源码提交。

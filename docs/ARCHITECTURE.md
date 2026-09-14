# YourSQL 系统架构与验收边界

## 数据流

```text
CLI / Python API / HTTP / SSH stdio
                 |
       Session + RBAC
                 |
Lexer -> Parser/AST -> Binder -> LogicalPlan
                 |
        Optimizer -> PhysicalPlan -> Executor
                 |
 Catalog + TableHeap + BPlusTree
                 |
 Page -> BufferPool(LRU/FIFO) -> DiskManager
```

模块边界：`common` 只放稳定类型、配置与错误；`sql` 只负责 SQL 语言前端和绑定；`planner` 负责逻辑/物理计划、优化和代价估算；`execution` 负责执行器与执行算子；`storage` 负责固定页、槽式记录、缓存与索引；`engine/runtime` 负责跨层协调，`engine/services` 负责服务协议。

## 后端目录约定

```text
yoursql/
├─ common/                 # 跨层类型、配置、错误和执行追踪
├─ sql/                    # Lexer / Parser / AST / Binder / Compiler
├─ planner/                # LogicalPlan / Optimizer / PhysicalPlan / Cost
├─ execution/              # 表达式求值、SELECT 查询适配和 Volcano 算子
│  ├─ evaluator.py         # 表达式编译、常量折叠、函数与三值逻辑
│  ├─ query.py             # 查询扫描、连接、索引访问、上下文和投影
│  └─ executor.py          # 与 SQL 无关的 Volcano 算子契约
├─ storage/                # Page / Heap / BufferPool / Disk / WAL / B+Tree
└─ engine/
   ├─ runtime/
   │  ├─ database.py       # Database 生命周期、事务编排、崩溃恢复与跨层协调
   │  └─ commands.py       # SQL 命令授权、DDL、DML 和索引变更
   ├─ concurrency/
   │  ├─ transaction.py    # 事务对象、页前像、事务状态机与事务管理器
   │  └─ lock_manager.py   # 表级 S/X 锁、严格两阶段封锁、等待图死锁检测
   ├─ catalog.py           # 表、视图、索引元数据
   ├─ security/            # RBAC、Session、AuditLog、安全管理器
   └─ services/            # HTTP、SSH、Workbench、存储检查协议
```

SQL 前端统一从 `sql` 导入，计划器从 `planner` 导入，执行器从 `execution` 导入；
安全和服务代码统一从 `engine.security`、`engine.services` 导入，数据库运行时
统一从 `engine.runtime.database` 导入。根目录不再保留旧入口转发模块。

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
- **执行器**：`execution/query.py` 负责把物理计划转换为扫描、连接和投影过程，并在满足联合索引最左前缀的等值、范围、`IN`、`BETWEEN` 及 AND/OR 组合谓词上选择 IndexScan；通用 Volcano 契约位于 `execution/executor.py`。
- **执行实现状态**：表达式能力位于 `execution/evaluator.py`，查询扫描、连接、索引访问和投影位于 `execution/query.py`，可复用的 Volcano 算子位于 `execution/executor.py`；`engine/runtime/commands.py` 负责 SQL 命令实现，`engine/runtime/database.py` 只保留跨层编排、生命周期和持久化，不再承载新的查询/命令细节。
- **单表选择规则**：小表（不超过 128 行）优先复用可用索引；大表在索引候选行数为 0 或不超过总行数 20% 时用 IndexScan，否则回退 SeqScan。该规则尤其针对 `SELECT *` 的回表成本。优化器先按索引元数据生成候选计划，再用当前 B+Tree 的候选行数校正大表单表计划；写入、删除与 DDL 会失效计划缓存，确保分布变化后重新判断。
- **持久化**：写语句在事务提交时持久化目录与脏页；重新打开数据库时，Catalog、表数据与索引都直接从页面文件读回。

## 事务、并发与预写日志

三项能力是同一套机制的三面：**事务定义语义，封锁提供并发，预写日志负责崩溃恢复**。

- **模型**：所有语句都跑在事务里。没有显式事务时开一条隐式事务（成功即提交、失败即回滚，等价自动提交）；只读语句**不开事务**，只取一次临时读锁，避免白写 BEGIN 日志、也避免误清计划缓存。事务状态存在 `threading.local` 上，同一个 `Database` 实例可被多线程共享，每个线程各开各的事务。
- **原子性 = 页级前像**：事务第一次改动某个页时，把修改前的整页内容留一份（`BufferPool.put_page` 里通过 `image_sink` 回调交给当前事务）。回滚时按相反顺序写回前像，于是堆表页、索引页、目录页一视同仁——不需要为每种页面各写一套逆向操作。事务新申请的页由 `DiskManager.allocate_hook` 单独记账，回滚时精确回收（而不是把 `next_page_id` 整体回退，那在并发下会与其它事务撞车）。
- **失败事务状态**：与 PostgreSQL 一致——显式事务里语句出错后事务进入 `failed`，继续持锁，后续语句与 `COMMIT` 一律被拒，只能 `ROLLBACK`。`DROP TABLE` 在显式事务内明确拒绝：它会把数据页立即归还空闲链表，页级前像救不回已被释放的页。
- **封锁**：表级 S/X 锁 + **严格两阶段封锁**（锁保持到事务结束，因此不会级联回滚、天然可串行化）。读申请 S、写申请 X，允许只在"自己是唯一持有者"时升级 S→X；锁按事务重入计数，所以 `INSERT` 内部扫表校验唯一约束不会自锁。等待期间用**等待图**找经过自己的环，命中则回滚事务号最大（最年轻）的那个牺牲者；等待超过 `lock_timeout_seconds` 抛 `ConcurrencyError`。`READ COMMITTED` 下语句结束就放掉 S 锁，X 锁仍保持到事务结束。
- **预写日志**：日志文件 `<db>.wal`（JSON Lines；文件头存 `next_lsn`，其后每行一条记录），记录类型 `begin/commit/abort/page/alloc/checkpoint`，`page` 记录携带整页前像。页头末尾原本的 4 字节对齐填充**复用为页 LSN**，结构体尺寸与磁盘布局不变，旧库文件可直接打开。脏页写回磁盘前先按页 LSN 把日志 fsync（写前日志规则）。
- **提交顺序即恢复策略**：先把目录与数据页全部刷盘并 fsync，**再**写 `commit` 记录。于是"有 commit 记录"等价于"改动全在磁盘上"，恢复只需**回滚未提交事务**，不需要 redo。恢复在 `Database.__init__` 里、**装载目录之前**执行：按页取该事务最早的前像覆盖回去 → 回收 `alloc` 记下的页 → 写 `abort` 留痕 → checkpoint 截断日志。恢复是幂等的。日志尾部被写坏的行按"尾部截断"容忍，中间坏行才报 `RecoveryError`。
- **可观测**：`Database.transaction_state()`（也挂在 `metrics()` 里）返回当前事务、事务计数、锁表与等待队列、WAL 统计、上次恢复报告；CLI `--txn-status` 直接打印；交互模式提示符在事务中显示 `yoursql(txn <id>)>`；崩溃恢复发生时会往 stderr 打一行提示。

## 权限目录

四张 `_sys_*` Heap 表保持内部隐藏；`sys_users`、`sys_roles`、`sys_role_members`、`sys_privileges` 是 admin-only 的只读系统视图，访问时回读内部表且不分配独立数据页。`sys_users` 只公开用户名，不公开密码哈希；普通用户即使拥有全库 `SELECT` 也无法访问这些系统视图。

## 验收命令

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
& '.venv\Scripts\python.exe' -m pytest tests/test_transactions.py tests/test_concurrency.py tests/test_wal.py -q
& '.venv\Scripts\python.exe' -m benchmarks.run_benchbox_tpch --iterations 5 --force
& '.venv\Scripts\python.exe' -m benchmarks.compare_tpch_q6 --iterations 5
& '.venv\Scripts\python.exe' -m yoursql.cli --database data/showcase_v2.db --sql "SHOW TABLES; SHOW VIEWS;"
& '.venv\Scripts\python.exe' -m yoursql.cli --database data/showcase_v2.db --txn-status
& '.venv\Scripts\python.exe' -m yoursql.cli --database data/showcase_v2.db --lock-mode none --sql "BEGIN; SELECT COUNT(*) FROM lineitem; COMMIT;"
```

单元测试使用临时目录。第三方 benchmark 的 TPC-H 数据与数据库写入 `benchmarks/third_party/`、`benchmarks/results/`，跑分摘要写入 `benchmarks/reports/tpch_sf001_q6.json`（BenchBox 适配器口径）与 `benchmarks/reports/tpch_sf001_q6_engines.json`（SQLite/DuckDB 对照，裸 `execute()` 口径）；前两者不会进入源码提交。

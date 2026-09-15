# 开发清单（完成状态）

本清单对应课程指导书与项目原始计划；已完成条目由测试、接口或 benchmark 覆盖。后端用 `python -m pytest -q` 复核，前端用 `cd web; npm test` 复核。

## 一期：基础架构

- [x] 基础设施模块：公共 ID、Value、Schema、统一异常、配置、日志和模块边界。
- [x] DBMS Engine：Database 统一入口、生命周期、编译/绑定/执行协调、上下文和 ExecutionResult。
- [x] SQL 编译器：Compiler、Lexer、关键词/标识符/常量/运算符、递归下降 Parser、AST、表达式、Binder、Logical/Physical Plan、基础优化器。
- [x] 存储基础：单文件与 superblock、全局 PageId、页分配/回收、Page Header/类型/读写/CRC 校验、SlottedPage、槽复用、BufferPool、Pin/Unpin、Dirty、LRU/FIFO、刷盘和统一接口。

## 二期：单表数据库闭环

- [x] Catalog/Meta Data：数据库、表、字段、约束、索引元数据，Catalog 页持久化、加载和更新。
- [x] 记录管理：编码/解码、TableHeap、TableScan、RowId、插入/读取/更新/删除/槽回收。
- [x] 执行计划：Volcano Executor、SeqScan、Filter、Project、Sort、Limit、DDL/DML 执行。
- [x] 单表 SQL：CREATE/DROP TABLE、INSERT、SELECT、UPDATE、DELETE、WHERE、ORDER BY、LIMIT、DISTINCT。
- [x] 基础 Catalog 查看：SHOW TABLES/TABLE、DESC/DESCRIBE、SHOW COLUMNS/FIELDS、SHOW INDEX/INDEXES、SHOW CREATE TABLE。
- [x] 只读逻辑视图：CREATE/DROP VIEW、SELECT 访问、SHOW CREATE VIEW，以及工作台对象栏查询定义展示。
- [x] 系统视图：为四张隐藏权限表提供 admin-only 的只读查询视图，并在工作台单独展示。
- [x] 会话与权限：Session、用户/角色、隐藏内部权限 Heap 表、RBAC、权限校验、JSON Lines 审计。
- [x] 前端与服务：CLI、HTTP JSON、统一响应、health、metrics、启动/停止、SSH stdio 适配。

## Web 工作台验收增量

- [x] 真实登录、权限、元数据、查询历史和 SQL 执行 REST API；保留 legacy `/sql` 兼容入口。
- [x] 数据库选择器：登录页可在认证前选择当前服务目录下真实 `.db` 文件；工作台内切换仍受 `SECURITY` 权限保护，切换实例时隔离旧会话并要求重新登录。
- [x] 大数据演示资产：`data/showcase_v2.db`（137,036 行、4096B 页共 4915 页、9 表/11 索引/3 用户视图，库内文本已统一为 YourSQL）、`examples/showcase_init.sql` 初始化脚本和 `examples/showcase_demo.md` 全功能演示清单，附确定性生成器（可复现的是内容、行序、索引条目与页数；文件哈希因 `_sys_users` 随机盐而不固定）。
- [x] 仓库迁移与命名统一：包目录、入口、`YOURSQL_*` 环境变量与前端构建输出统一为 `yoursql`；复现用例移到 `examples/test_example.md` 与 `examples/test_example.sql`，演示库移到 `data/showcase_v2.db`。
- [x] 结果区整合消息、原始 JSON 和编译流水线；Token 逐行表格，AST/Plan 轻量节点画板。
- [x] 独立存储模式：只读页面地图、`MSP2` 双向槽式页的页头/槽目录/空闲区/记录区可视化；每格按槽目录项宽度固定为 6 B，不足整格的边界使用分段格，网格外框显示位置索引，多格区域按“完整 Hex/ASCII → 具体格 → 汇总”循环查看并保留关联槽位；项目内数据库已统一迁移到 30 B 对齐页头，包含标题栏区域定位、Buffer Pool、索引和明确限制。
- [x] 存储检查增量刷新：选中页支持单页刷新；页面地图通过 BufferPool 变更游标合并页头增量；缓存和索引目录按当前页签刷新，SQL 完成和存储模式停留期间自动触发轻量更新。
- [x] 性能监控看板：采集查询排队/编译/执行/物化耗时、页 I/O、缓存命中与淘汰事件；提供慢查询 JSONL 日志、摘要/列表/详情接口和前端趋势看板。
- [x] 页面地图轻量化：`fields=map` 按 500 页/批只取画图必需的页头字段，逐页目录归属与整张索引绑定表不再计入加载路径，表名/索引名延后到选中页按需获取；4860 页演示库的全量加载从 25–45 s 降到约 2 s。
- [x] 前端构建、自动化接口验收和第三方 BenchBox TPC-H Q6 回归。
- [x] 多引擎对照：同一份 `lineitem.tbl` + 同一条 Q6 对比 SQLite（文件/内存）与 DuckDB，报告写入 `benchmarks/reports/tpch_sf001_q6_engines.json`；同语义组结果交叉校验为 `734493.7281`，并用 cProfile 定位到逐行行上下文重建与表达式解释为主要开销。
- [x] 逐行执行路径优化：行上下文模板化 + 按需列裁剪 + WHERE 预过滤（不通过的行不建上下文）+ 常量折叠 + 表达式编译为闭包；同口径背靠背对比 TPC-H Q6 从 3.1542 s 降到 0.5741 s（约 5.5×，对照脚本当次 0.2961 s），并新增 `tests/test_expression_compiler.py` 对编译闭包与解释器的等价性回归。
- [x] 差距归因（含阶段拆解）：单次 Q6 中 JSON 解析 0.180 s（48%）、槽目录反序列化 0.052 s、读页 0.016 s、谓词 0.06 s；与 SQLite 的 34× 差距主要来自“每行都产生 Python 对象 + JSON 行编码”，与 DuckDB 的 296× 还差列存与向量化。
- [x] BufferPool 淘汰路径优化：`_evict_one` 从 O(容量) 改为按 `OrderedDict` 维护的淘汰顺序取队首，并给对照脚本加 `--settle-seconds` 固定测量口径（同进程内依次测不同池容量会产生虚假的“池越大越慢”）。
- [x] BufferPool 访问策略实验：新增 2Q 冷/热队列以避免顺序扫描污染热点页，并增加 INDEX/CATALOG/SUPERBLOCK 页面类型保护开关；`examples/create_buffer_pool_lab.py` 与 `benchmarks/compare_buffer_pool_lab.py` 提供基线、单项和组合策略的固定顺序对照。
- [x] 索引构建与只读路径修复：`bulk_load` 分块改为按“条目编码长度”增量核算（5,000 行 7.97 s → 0.14 s，60,175 行约 2 s）；`DiskManager.peek` 先 flush 再读，避免被淘汰的页读成旧内容（新增 `tests/test_disk_peek.py`）；抽候选前先常量折叠，否则 `DATE(...)` 会让字符串列范围索引失效。
- [x] 索引实测（TPC-H Q6）：现场建 `l_shipdate` / `l_discount` / `(l_shipdate, l_discount)` / `l_quantity` 四个索引，结果 SeqScan 0.3591 s 快于 IndexScan 0.3926 s（1,728 候选）与 0.6233 s（800 候选）——现有“候选行 ≤ 20% 行数”的规则缺“触碰页数 × 随机访问惩罚”项。
- [x] 索引侧优化三项：候选集缓存（252.9 ms → 0.12 ms）、页级代价模型（正确拒绝 10,969 候选的低选择性索引）、`CREATE INDEX ... INCLUDE` + `IndexOnlyScan`（Q6 0.6138 s → 0.1358 s）；新增 `tests/test_index_cache.py` 与 `tests/test_covering_index.py`。
- [x] 流式执行与装载批量化：扫描/连接去掉全量物化并按 `LIMIT` 提前终止（`LIMIT 10` 的 `rows_examined` 60,175 → 10）；连接内单表谓词下推（连接聚合 234.68 s → 2.19 s，107×）；`append_batch` 批量写页 + 空索引延迟 `bulk_load` + 装载期集合取代 O(n²) 主键校验（60,175 行导入 51.3 s → 1.74 s，29.5×）；新增 `tests/test_streaming_and_batch_load.py`。
- [x] 多查询跨引擎对标（TPC-H SF0.01，8 条可编译查询，最终跑分）：装载 8 表 87k 行 **2.8 s**（SQLite 0.9 s / DuckDB 0.5 s）；单表 Q6 341 ms、Q1 1136 ms（SQLite 10 / 49 ms）；**8/8 全部可跑**，其中 **Q19 1217 ms 反超 SQLite 6653 ms（5.5×）**，带连接的 Q3/Q5/Q10/Q16/Q18 对 SQLite 慢 3–19×；报告见 `benchmarks/reports/tpch_sf001_compare.json`（含 `sample_match` 逐条值一致标记与 `value_notes`）。
- [x] 连接执行三策略：哈希连接（实测建表 0.22 µs/行、探测 0.05 µs/次，NULL 键永不匹配，一对多与 `RIGHT/FULL` 语义均保留）、索引嵌套循环（不物化右表，仅 `INNER/LEFT`，受 1.4 ms/次索引查找限制）、非等值回退嵌套循环；ON 条件拆分（连接键 + 残余谓词 + 单表下推），策略写入 `stats["joins"]`。
- [x] 连接键推断从“只认 ON”扩到“能看 `WHERE`”：6 条带 JOIN 的查询原本全超时，实跑定位到真因是 TPC-H 的 sqlite 方言用逗号连接（`FROM a, b WHERE a.x = b.y`，解析成 `CROSS` + 空 `ON`）→ 退化为笛卡尔积。现支持：逗号连接从 `WHERE` 推断等值键（仅 `INNER/CROSS`，外连接的 `WHERE` 不得提升）、未限定列名按两侧模式列名归属、键取值裸列名回退、残余谓词按“引用表是否已加入”分配层级、`CROSS` 无键时以残余作连接条件、表/视图通用取列名。
- [x] 另两类“跑不完”场景：不相关 `IN` 子查询只执行一次并物化（原 Q18 逐行重跑 15,000 次子查询，现 1471 ms；相关子查询仍逐行，`NOT IN` 含 `NULL` 的三值逻辑有回归）；`OR` 分支连接键取“所有分支共同等式”（Q19 1217 ms）。`tests/test_join_strategies.py` 扩到 11 条，并新增 8 条查询对 SQLite 的逐值对拍（行多重集全等，浮点 6 位容差）。
- [x] 存储交互修正：页面块一次点击即切换页面；右侧详情栏在页面页签常驻，开关块详情不再改变地图列数与滚动位置，且只在关闭按钮或 Esc 时收起；页详情刷新不再静默关闭抽屉。

## 查询扩展

- [x] 多表查询和 JOIN（INNER、LEFT、RIGHT、FULL、CROSS 的嵌套循环实现）。
- [x] GROUP BY、HAVING、COUNT/SUM/AVG/MIN/MAX 聚合函数。
- [x] IN (SELECT ...) 子查询和 UNION/UNION ALL。

## 索引存储

- [x] 落盘 B+Tree：INDEX 页、内部/叶子节点、父子关系、叶子链、批量装载、增量插入/删除、分裂/借位/合并、精确/范围扫描和 IndexScan。
- [x] 唯一索引、PRIMARY KEY/UNIQUE 约束和联合索引。

## 查询优化

- [x] SeqScan/IndexScan 选择、基础 Join/扫描计划、统计信息、代价模型和规范化 SQL 计划缓存；优化计划已接入 Database SELECT 扫描路径。
- [x] 具名重写规则框架（`RewriteRule` + `DEFAULT_RULES`）：`constant_folding` / `boolean_simplification` / `predicate_elimination` / `predicate_pushdown` / `index_selection` / **`join_reordering`** / **`limit_pushdown`** 共 **7 条**规则可枚举、可单独关闭（`Optimizer(disabled_rules=...)`、`Database(disabled_rules=...)`、CLI `--disable-rule`），命中情况逐条写进 `plan.properties["rules"]`；CLI `--rules` 打印规则清单，EXPLAIN 因此能回答“这条语句被优化了什么”。规则开关放在 `ContextVar` 上，既能被 `@classmethod` 的规则方法读到，也天然按调用栈隔离（多线程 / 嵌套调用不会互相污染）。`tests/test_optimizer_rules.py` 13 条。
- [x] 计划可视化后端通道：`PlanNode.label_lines()` / `to_mermaid()` / `to_dot()`，CLI `--plan {text,mermaid,dot,json}` 做到“只编译不出图也能看图”；`scripts/plan_visualize.py` 可导出含 Mermaid 图的自包含 HTML（示例产物 `docs/plan_demo.html`）。Web 工作台原有的 AST/Plan 画板保持不变。`tests/test_plan_visualization.py` 9 条。
- [x] 执行层三项性能改造（证据见 `docs/OPTIMIZATION_BACKLOG.md` 第 7 节）：
  - **连接策略三候选同层比较**：`hash / index_nested_loop / nested_loop` 按同一套代价模型取最小，结束“只要哈希装得下就永远选哈希”的闸门顺序，小表驱动大表时索引连接在默认预算下即可自然命中；哈希预算溢出退化为嵌套循环时写入 `stats["join_degrade"]` 给出可读原因。
  - **`limit_pushdown` 的运行时落地**：`ORDER BY` + 较小 `LIMIT` 时 Sort 上标注 `top_n`，运行时有界堆只保留前 k 行（lineitem 60,175 行实测 627.6 ms → 475.0 ms、峰值内存 55.5 MB → 1.16 MB，结果逐值一致）。`tests/test_streaming_and_batch_load.py`。
  - **分组聚合改流式累加器**：`dict[组键] → {count,sum,min,max}` 边扫边累积，内存由 O(行数) 降到 O(组数)（单组 SUM 峰值 35.5 MB → 1.15 MB），`COUNT(DISTINCT)` 用去重集合单独兜住。`tests/test_aggregation_streaming.py` 15 条固定 NULL / 空输入 / DISTINCT / 聚合出现在 HAVING 与 ORDER BY 的语义。
## 测试与性能

- [x] 公共层/存储/SQL/集成/服务/索引单元测试。
- [x] MiniOB 必修语句对标样例、重启持久化和错误场景测试。
- [x] 存储缓存性能统计、落盘索引页检查、索引前后路径对比和第三方 BenchBox/TPC-H Q6 跑分适配。
- [x] 跨引擎逐值对拍：`benchmarks/verify_tpch_values.py` 把 TPC-H 查询的结果与同数据的 SQLite 库做行多重集比对（数值归一到 6 位小数，DECIMAL/float 都能并排比较），不一致即以非零码退出。当前覆盖 **16 条**（Q1/Q3/Q5–Q14/Q16–Q19）全部一致，其中 Q6 记为 `KNOWN_DIFFERENCES`（DECIMAL 定点 vs SQLite 双精度浮点的口径差异，非错误）；Q2/Q4/Q15/Q20/Q21 因相关子查询逐行重跑超时、Q22 单条约 50 s，未纳入。报告里的 `sample_match` 字段提供同一信息的快速版本。

## 事务、并发与预写日志

三项能力共用一套机制落地：**事务定义语义，封锁提供并发，预写日志负责崩溃恢复**。

- [x] **事务（BEGIN / COMMIT / ROLLBACK）**
  - 语法：`BEGIN | BEGIN WORK | BEGIN TRANSACTION [ISOLATION LEVEL ...]`、`START TRANSACTION`、`COMMIT [WORK]`、`ROLLBACK [WORK]`、`SET TRANSACTION ISOLATION LEVEL {SERIALIZABLE | READ COMMITTED}`。
  - **原子性靠页级前像**：事务第一次修改某个页时把"修改前的整页内容"留存下来（同时写进 WAL）。回滚时按相反顺序把前像写回，堆表页、索引页、目录页一视同仁——不需要为每种页面单独写逆向操作。这一点由 `test_rollback_restores_index_lookups` 固定住：UPDATE 走的索引条目同样被撤销。
  - **自动提交**：所有语句一律跑在事务里。没有显式事务时开一条隐式事务，成功即提交、失败即回滚；只读语句不开事务（否则会白写 BEGIN 日志、还会误清计划缓存），只取一次临时读锁（负数编号，不与真实事务号冲突）。
  - **失败事务状态**：与 PostgreSQL 一致——显式事务里某条语句出错后事务进入 `failed`，继续持锁，后续语句与 COMMIT 一律被拒，只能 ROLLBACK。避免"半条语句"被后续语句读走。
  - **DDL 事务**：回滚时按 BEGIN 时的目录快照重建目录对象，并重建索引（不再存在的索引树连页一起释放）。`DROP TABLE` 在显式事务内**明确拒绝**——它会把数据页立即归还空闲链表，页级前像救不回已被释放的页，与其给出"能回滚"的错觉不如直接报错。
  - 事务状态绑定在**线程**上（`threading.local`），同一个 `Database` 实例可以被多线程共享，每个线程各开各的事务。
  - `tests/test_transactions.py` 16 条。

- [x] **并发控制（表级共享/排他锁 + 严格两阶段封锁）**
  - 读申请 **S 锁**、写申请 **X 锁**，S/S 相容、S/X 与 X/X 互斥；允许 **锁升级**（S→X，仅当自己是唯一持有者）；锁按事务重入计数，所以 `INSERT` 内部扫表校验唯一约束不会自锁。
  - **严格 2PL**：锁保持到事务结束，因此不会级联回滚，天然可串行化。`READ COMMITTED` 隔离级别下语句结束就放掉 S 锁（X 锁仍保持到事务结束），两种级别可用 `test_read_committed_releases_shared_locks_per_statement` 与 `test_serializable_holds_shared_locks_until_commit` 现场对比。
  - **死锁检测**：等待期间构造等待图（等待者 → 所需资源的持有者），深度优先找经过自己的环；命中则选**事务号最大（最年轻）** 的事务作牺牲者，标记后由其自行回滚并释放锁唤醒其余事务。等待超过 `lock_timeout_seconds`（默认 5 s）同样抛 `ConcurrencyError`。
  - 锁可按粒度关闭：`DatabaseConfig(lock_mode="none")` / CLI `--lock-mode none`，用于现场对照"有/无并发控制"的差别。
  - `tests/test_concurrency.py` 10 条（含 4 线程 × 6 行并发写入最终行数完整的压力用例）。并发用例全部以"事件 / 锁表状态"做同步点，不靠 sleep 猜时序。

- [x] **预写日志与崩溃恢复**
  - 日志文件 `<db>.wal`（JSON Lines：文件头存 `next_lsn`，其后每行一条记录），记录类型 `begin / commit / abort / page / alloc / checkpoint`；`page` 记录携带整页前像，`alloc` 记录用于回滚时精确回收事务新申请的页。
  - **页 LSN 落在页头**：页头末尾原本是 4 字节对齐填充（旧文件恒为 0），现复用为 LSN。结构体尺寸与磁盘布局没变，**旧数据库文件可直接打开**（`test_legacy_page_header_without_lsn_still_reads`）。
  - **写前日志规则**：缓冲池把脏页写回磁盘前，先 `ensure_persisted(page.lsn)` 把该页之前的日志 fsync；`test_wal_is_flushed_before_dirty_page_reaches_disk` 挂在 `DiskManager.write` 上断言"每个落盘页的 LSN ≤ 当时已 fsync 的 LSN"。
  - **提交顺序即恢复策略**：先把目录与数据页全部刷盘并 fsync，**再**写 `commit` 记录并 fsync。因此"有 commit 记录"等价于"改动全都在磁盘上"，恢复只需**回滚未提交事务**，不需要 redo——`test_recovery_only_undoes_incomplete_transactions` 用一份手工构造的日志（事务 91 有 commit、92 没有）验证了这一点。
  - 提交时若系统内没有其它活跃事务，直接做 **checkpoint** 截断日志（保留文件头与 LSN 计数）。
  - 崩溃恢复在 `Database.__init__` 里、**装载目录之前**执行：按页取该事务最早的一份前像覆盖回去，回收 `alloc` 记录里的页，写 `abort` 留痕并 checkpoint。恢复是幂等的（`test_recovery_is_idempotent`）。
  - 日志尾部被写了一半的行按"尾部截断"容忍；中间出现坏行才报 `RecoveryError`（两条用例各一）。
  - 可用 `DatabaseConfig(wal_enabled=False)` / CLI `--no-wal` 关闭，对照"没有日志时崩溃会丢什么"。
  - `tests/test_wal.py` 14 条，其中"崩溃"用 `_crash()` 模拟：先把脏页落盘（对应 steal 行为）再直接关句柄，不跑回滚、不补 commit。

- [x] **可观测入口**：CLI `--txn-status` 打印当前事务 / 锁表 / WAL / 上次恢复报告的 JSON；交互模式提示符在事务中显示 `yoursql(txn <id>)>`；`Database.transaction_state()` 同时挂进 `metrics()`。崩溃恢复发生时会往 **stderr** 打一行提示（不污染 `--json` 的标准输出）。

## 高级扩展逐项对照

指导书 17 项"高级扩展"的落点与证据（"已有"指本轮之前已实现，"本轮"指本次补齐）：

| # | 高级扩展 | 状态 | 落点与证据 |
| --- | --- | --- | --- |
| 1 | 多表连接 | 已有 | INNER / LEFT / RIGHT / FULL / CROSS，哈希 / 索引嵌套循环 / 嵌套循环三策略（写入 `stats["joins"]`）；`tests/test_join_strategies.py` |
| 2 | 子查询 | 已有 | `IN (SELECT ...)`、`EXISTS`/`NOT EXISTS`、标量/相关子查询、派生表、CTE；`tests/test_sql_features.py` |
| 3 | 聚集与分组 | 已有 | GROUP BY / HAVING / COUNT·SUM·AVG·MIN·MAX |
| 4 | 排序 | 已有 | ORDER BY 多列、ASC/DESC、NULLS FIRST/LAST |
| 5 | 去重 | 已有 | `SELECT DISTINCT` |
| 6 | LIMIT | 已有 | LIMIT / OFFSET，并参与流式执行的提前终止 |
| 7 | 集合运算 | 已有 | UNION / UNION ALL |
| 8 | 视图 | 已有 | CREATE/DROP VIEW、`SHOW CREATE VIEW` |
| 9 | 完整性约束 | 已有 | PRIMARY KEY / UNIQUE / NOT NULL + 唯一索引 |
| 10 | 授权与角色 | 已有 | CREATE ROLE/USER、GRANT/REVOKE、RBAC 与 JSON Lines 审计 |
| 11 | 数据类型 | 已有 | INT / FLOAT / **DECIMAL 定点** / BOOLEAN / VARCHAR / NULL；`tests/test_decimal.py` |
| 12 | NULL / LIKE / JOIN 语义 | 已有 | 三值逻辑、LIKE 通配、NULL 连接键不匹配、外连接补 NULL |
| 13 | 更新操作 | 已有 | INSERT（含批量）/ UPDATE / DELETE |
| 14 | 错误处理 | 已有 | Lexical / Syntax / Semantic / Execution / Storage 五类，统一 `at line <行>, column <列>` |
| 15 | EXPLAIN 与查询计划可视化 | 已有 + 上轮补齐 | 文本树 `PlanNode.explain()` 与 `to_dict()` JSON 已有；上轮补后端 Mermaid / DOT / HTML 通道（见"查询优化"节） |
| 16 | 错误恢复 | **已补齐** | `parse_recovering` / `ParseOutcome` / `Database.check_script` / CLI `--check`；`tests/test_error_recovery.py` |
| 17 | 算法规则框架 | **已补齐** | `RewriteRule` + `DEFAULT_RULES`（**7 条**具名规则，含 `join_reordering` / `limit_pushdown`）+ `plan.properties["rules"]` + CLI `--rules` / `--disable-rule`；`tests/test_optimizer_rules.py` |
| 18 | 事务 | **本轮补齐** | BEGIN/COMMIT/ROLLBACK、页级前像回滚（含索引页与目录页）、失败事务状态、DDL 事务；`tests/test_transactions.py` 16 条 |
| 19 | 并发 | **本轮补齐** | 表级 S/X 锁 + 严格两阶段封锁 + 锁升级 + 等待图死锁检测 + 锁超时 + 两种隔离级别；`tests/test_concurrency.py` 10 条 |
| 20 | WAL / 崩溃恢复 | **本轮补齐** | `<db>.wal` 日志文件、页头 LSN、写前日志规则、undo-only 恢复、checkpoint 截断；`tests/test_wal.py` 14 条 |

验收现场可以直接跑这几条命令取证：

```
python -m pytest -q                                  # 312 passed
python -m yoursql.cli --rules                        # 规则清单（7/7 启用）
python -m yoursql.cli --sql "SELECT 1; SELCT 2; SELECT @ FROM t;" --check
# <db> 换成任意已有库；接上 --disable-rule 即可现场对比同一语句的计划差异
python -m yoursql.cli --database <db> --sql "SELECT ... " --plan mermaid
python -m yoursql.cli --database <db> --sql "SELECT ... " --plan dot --disable-rule index_selection
python -m scripts.plan_visualize --database <db> --sql "SELECT ... " \
    --format html --out docs/plan_demo.html

# 查询优化三项改造的对照取证
python -m pytest tests/test_optimizer_rules.py tests/test_join_strategies.py \
    tests/test_streaming_and_batch_load.py tests/test_aggregation_streaming.py -q
# 同一语句开关 limit_pushdown，比较 plan 上的 top_n 与运行结果
python -m yoursql.cli --database <db> --plan json \
    --sql "SELECT l_orderkey FROM lineitem ORDER BY l_extendedprice LIMIT 10;"

# 事务 / 并发 / WAL：全链路取证
python -m pytest tests/test_transactions.py tests/test_concurrency.py tests/test_wal.py -q
python -m yoursql.cli --database tmp/txn.db --txn-status          # 事务 / 锁表 / WAL / 上次恢复报告
python -m yoursql.cli --database tmp/txn.db --lock-mode none --sql "BEGIN; ..."   # 关闭并发控制对照
python -m yoursql.cli --database tmp/txn.db --no-wal --sql "..."                  # 关闭预写日志对照
python -m yoursql.cli --database tmp/txn.db --isolation read_committed --sql "BEGIN; SELECT ...; COMMIT;"
```

## 下一步（按已实测到的缺口）

### 已完成（本轮）

- [x] DECIMAL 定点化：值层新增 `DataType.DECIMAL`（`NUMERIC/NUMBER` 别名），字面量与 `Value` 走 `decimal.Decimal`；落盘用 `$decimal` 标记无损往返（`yoursql/storage/codec.py`）。`compare_values` 刻意**不**做 Decimal↔float 对齐——定点保精确、FLOAT 保 IEEE-754。TPC-H Q6 现返回 `1193053.2253`，与 DuckDB 的 DECIMAL 列逐值一致（旧的双精度值是 `734493.7281`）。`tests/test_decimal.py` 8 条。
- [x] 连接键推断的 `OR` 分支泛化：`OR` 各分支的等式取并集作为候选连接键（原先只认"所有分支共有的等式"），`_join_key_pairs` 因此能覆盖 `a.x = b.y OR a.z = b.w` 这类写法。
- [x] 其余 14 条 TPC-H 查询的缺失特性：新增派生表 `FROM (SELECT ...) AS d`、CTE `WITH ... AS (...)`（内联为派生表）、`CASE`（searched + simple）、`CAST`、`EXISTS`/`NOT EXISTS`、标量子查询、相关子查询。**22/22 条 TPC-H 查询均可通过 parser + binder + planner 并执行**（此前 8/22 可编译）；其中 17 条与 SQLite 逐值一致（Q8 由上面的自连接歧义键修复打通），Q2/Q4/Q15/Q20/Q21 卡在相关子查询逐行重跑的性能上。`tests/test_sql_features.py` 11 条固定住结果值。
- [x] 顺带修掉四个静默错误（都是"能跑但结果错"，比报错更危险）：
  - 相关子查询被当成不相关：列上下文模板与相关性判定把「别名 + 原表名」都登记为本地，`FROM t AS u` 于是遮蔽了外层的 `t`，`u.id <= t.id` 退化成恒真。现按标准 SQL 只登记别名。
  - 相关子查询的 WHERE 被下推到只含本行的 `_RowView` 预过滤，拿不到外层列；现带外层作用域时关闭全部谓词下推（`_joined_contexts(pushdown=False)`）。
  - `_collect_column_ref_nodes` 漏判 `ExistsPredicate`，导致 `EXISTS` 子查询被下推。
  - **自连接的连接键退化成歧义裸列名**：`_join_key_pairs` 只记 `(左列, 右列)`、丢掉左列的限定符，取值时只能靠裸列名兜底；而 `_merge_context` 遇到同名裸列会标成歧义值，哈希探测于是静默失配。TPC-H Q8 的 `nation AS n1, nation AS n2, region` 正是如此：结果随 FROM 子句顺序在 29 行与 0 行之间跳变（`region` 在 `n2` 之后连接就一定归零）。现键对改为 `(左归属, 左列, 右列)`，`_column_owner` 用「限定符 → 列名集合」定出未限定列的归属，`_key_is_usable` 再把 NULL/缺失/歧义值一并排除在连接键之外。
- [x] 单表扫描的逐行解码优化（Q1/Q6 实测 **1.46–1.55×**）：
  - `codec.loads` 不再在 `json.loads` 之后跑一遍递归 `_revive`，改用 C 扫描器的 `object_hook` 就地还原 `$decimal`（代码对象调用数 723 万 → 416 万）；
  - 解码统一走 `json.JSONDecoder.raw_decode`，绕开 `json.loads` 每次都要付的 Python 包装层（两次 `WHITESPACE.match`）——这一项单独就是 1.5–1.8×；
  - 索引节点读路径把 O(n) 的排序校验挪出热路径（`from_page(verify=False)` 默认只做 O(1) 结构自检），单页解码 **3.44×**（0.059 → 0.017 ms/页），全量校验留在 `index_page_info` 与 `validate()`。`tests/test_storage_hot_path.py` 6 条。

- [x] 只关心行数的扫描不再解码记录（`SELECT COUNT(*) FROM lineitem` 全表 **571 → 66 ms（8.7×）**，`SELECT 1 FROM lineitem` 330 → 66 ms（5.0×））：
  - `COUNT(*)` 里的 `Star` 只是"数行数"的占位，与 `SELECT *` 不是一回事。早先两者共用同一个递归收集器，于是被判定为"需要全部列"，每行白构造 32 键上下文（16 列的表：16 个限定名 + 16 个裸列名）。现把聚合参数里的 `*` 单独处理，`needed` 收敛为空集。
  - 连带收益：`SELECT COUNT(*) FROM t WHERE 索引列 < ...` 不再被"需要全部列"挡住覆盖索引直读，访问路径从 `IndexScan` 变成 `IndexOnlyScan`（`tests/test_decimal.py::test_decimal_range_index_scan` 的断言与取值一并更新）。
  - 新增 `SlottedPage.live_count()` 与 `TableHeap.count()`：只数槽目录的活槽，不切记录、不跑 JSON 解码；`_scan_contexts` 在"不需要任何列值 + 无谓词"时走该快路径，用同一个只读上下文重复产出，行数与 `rows_examined` 语义不变。
  - 顺带修 `SlottedPage._from_binary` 每页把 `sorted(ranges)` 算两遍（宽表全表扫描每页白排一次），以及 `TableHeap.scan` 每行重复构造 `PageId`。
  - 测量口径：`benchmarks/bench_scan_paths.py`，每个用例独立进程 × 重复取最小值。同进程连测会被堆状态与 GC 干扰（同一配置实测能差 30%，Q6 曾因此被误判成"变慢 30%"），该脚本强制隔离。
  - 回归：`tests/test_scan_fast_path.py` 10 条（结果一致、删除后按活槽计数、带 WHERE/交叉连接/分组/HAVING/空表、存储层 `count()` 与 `scan()` 同口径）；另外与原始 `.tbl` 行数（8 张表全对）和同数据 SQLite 库（16 条 lineitem 聚合语句逐值）对拍一致；全量 `pytest` **251 条通过**。

- [x] 查询优化三项改造（证据与口径见 `docs/OPTIMIZATION_BACKLOG.md` 第 7 节）：
  - **`join_reordering` 规则（计划层）**：对全 INNER/CROSS 的等值连接按 `TableStats.row_count` 贪心重排——先取行数最小的表，再逐张接入"与已连接集合有等值键"的表，避免首层退化成笛卡尔积。实测同一个三表连接 6 种 `FROM` 写法：改前 **2/6 顺序 >60 s 超时**，改后 **6/6 全部 1.18–1.65 s**，结果指纹完全一致。表名/别名与统计键的映射是坑：别名的 `row_count` 会查成 0，必须用真实表名取统计，任一表不在统计里就整体跳过重排。另一个坑是 `EXPLAIN` 包裹：`Explain` 是独立语句类型，规则若只认顶层 `Select`，`EXPLAIN SELECT ...` 会跳过重排、与实际执行不一致（现由 `_reorder_joins_in_statement` 穿透）。
  - **`limit_pushdown` 规则 + 运行时 top-N**：`ORDER BY` + 较小 `LIMIT` 时给 Sort 标 `top_n`，运行时有界堆只留前 k 行（`_compile_order_keys` 把排序键编译一次后复用）。lineitem 60,175 行实测 **627.6 ms → 475.0 ms（1.32×）、峰值内存 55.5 MB → 1.16 MB（48×）**，7 组组合用例（`NULLS FIRST/LAST`、多键、`DESC`、`OFFSET`）与全量排序逐值一致；关掉该规则 `top_n` 即消失。
  - **分组聚合改流式累加器（执行层）**：`dict[组键] → {count,sum,min,max}` 边扫边累积，内存 **O(行数) → O(组数)**（单组 `SUM` 峰值 35.5 MB → 1.15 MB，三组 `GROUP BY` 27.8 MB → 1.24 MB）；聚合采集改走 `_walk_expressions`，HAVING / ORDER BY 里不在投影的聚合也能查回预计算值；`COUNT(DISTINCT)` 用去重集合单独兜住。`tests/test_aggregation_streaming.py` 15 条固定 NULL / 空输入 / DISTINCT / 位置语义。
  - **连接策略三候选同层比较**：`hash / index_nested_loop / nested_loop` 用同一套代价模型取最小，修正"只要哈希装得下就永远选哈希"的闸门顺序。左 2 行 × 右 20,000 行且右表有索引时，**默认预算下即可选中 `IndexNestedLoop`**（此前要 monkeypatch 把预算压到 46,400 B）；哈希预算溢出退化为嵌套循环时写入 `stats["join_degrade"]`，不再静默降级。
  - 全量 `pytest` **312 条通过**（新增/更新：`test_aggregation_streaming.py` 15、`test_optimizer_rules.py` 13、`test_join_strategies.py` 18、`test_streaming_and_batch_load.py` 5）。

### 待办（已用实测数据重写过方向）

- [ ] **不要按原计划做"索引节点/行记录二进制化"**——实测证伪：纯 Python 手写二进制解码比 C 实现的 `json` 慢 **2–4×**（2000 行 16 列：`json.loads` 4.0 ms vs 定标二进制 8.5 ms），体积还大 41%（161 B/行 vs 114 B/行）。索引节点里 JSON 只占解码耗时的 **15–19%**（0.014 ms 页内 JSON vs 0.067 ms 整页解码），行记录里 `json.loads` 也只占 40%。二进制只有在**能支持页内随机访问**（定宽键 + 页内二分，不解码整页）时才值得做，那是另一个量级的工作。
- [ ] **需要列值**的全表扫描仍以 JSON 逐行解码为硬性下限（`COUNT(*)` 那一档已用"干脆不解码"解决；`COUNT(列)`/`SUM(列)`/纯投影实测只在噪声内浮动）。实测口径：lineitem 一行解码 **2.00 µs**，其中 `$decimal` 还原器占 **1.06 µs**——同一批行换成纯 C 解码是 **0.94 µs**。要压这一档，**只解 WHERE/投影用到的列**是唯一还有量级空间的方向，但代价明确：
  1. 需要把"哪些列是 DECIMAL"从 schema 透传到 `TableHeap`（它当前不持有 schema）；
  2. 未解码的 DECIMAL 列会以标记字典的形式留在行里，任何消费点读到它就是**静默错误**（本项目已修过四个同类问题），必须先把消费边界钉死；
  3. 即便是完美实现，收益上限也只是把 2.00 µs/行压到 1.1 µs/行左右（约 18%），远小于 `COUNT(*)` 那一档的量级。
- [ ] 连接键推断仍不覆盖：子查询内部的连接键、`OR` 各分支结构不同（非等式）的情形。
- [ ] 连接与聚合的绝对耗时仍是最大头（Q1 ~0.96 s / Q6 ~0.43 s，对 SQLite 约 20–30×；Q6 本轮实测仍为 370–420 ms 区间，扫描路径改动落在噪声内）；下一步优先级高于继续压解码。
- [ ] 相关子查询现在**逐行重跑**（Q2 在 SF0.01 上单条 >45 s 未跑完）。真正的解法是去相关变换（把 `min(ps_supplycost)` 这类改写成派生表 + `GROUP BY` 连接），不是微优化。

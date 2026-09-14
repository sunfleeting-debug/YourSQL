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
- [x] 页面地图轻量化：`fields=map` 按 500 页/批只取画图必需的页头字段，逐页目录归属与整张索引绑定表不再计入加载路径，表名/索引名延后到选中页按需获取；4860 页演示库的全量加载从 25–45 s 降到约 2 s。
- [x] 前端构建、自动化接口验收和第三方 BenchBox TPC-H Q6 回归。
- [x] 多引擎对照：同一份 `lineitem.tbl` + 同一条 Q6 对比 SQLite（文件/内存）与 DuckDB，报告写入 `benchmarks/reports/tpch_sf001_q6_engines.json`；同语义组结果交叉校验为 `734493.7281`，并用 cProfile 定位到逐行行上下文重建与表达式解释为主要开销。
- [x] 逐行执行路径优化：行上下文模板化 + 按需列裁剪 + WHERE 预过滤（不通过的行不建上下文）+ 常量折叠 + 表达式编译为闭包；同口径背靠背对比 TPC-H Q6 从 3.1542 s 降到 0.5741 s（约 5.5×，对照脚本当次 0.2961 s），并新增 `tests/test_expression_compiler.py` 对编译闭包与解释器的等价性回归。
- [x] 差距归因（含阶段拆解）：单次 Q6 中 JSON 解析 0.180 s（48%）、槽目录反序列化 0.052 s、读页 0.016 s、谓词 0.06 s；与 SQLite 的 34× 差距主要来自“每行都产生 Python 对象 + JSON 行编码”，与 DuckDB 的 296× 还差列存与向量化。
- [x] BufferPool 淘汰路径优化：`_evict_one` 从 O(容量) 改为按 `OrderedDict` 维护的淘汰顺序取队首，并给对照脚本加 `--settle-seconds` 固定测量口径（同进程内依次测不同池容量会产生虚假的“池越大越慢”）。
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

## 测试与性能

- [x] 公共层/存储/SQL/集成/服务/索引单元测试。
- [x] MiniOB 必修语句对标样例、重启持久化和错误场景测试。
- [x] 存储缓存性能统计、落盘索引页检查、索引前后路径对比和第三方 BenchBox/TPC-H Q6 跑分适配。
- [x] 跨引擎逐值对拍：`benchmarks/verify_tpch_values.py` 把 8 条可编译 TPC-H 查询的结果与同数据的 SQLite 库做行多重集比对（浮点 6 位容差），不一致即以非零码退出；报告里的 `sample_match` 字段提供同一信息的快速版本。

## 下一步（按已实测到的缺口）

- [x] 内部返回类型可读性：全面审查存储及相关内部接口（重点是 `Page`/`SlottedPage`、`BufferPool`、`DiskManager`、`BPlusTree` 和执行辅助函数），为包含多个异构字段的结果定义带字段名的专用 `dataclass`/类型，逐步替代多层嵌套的匿名 `tuple`/`dict` 返回值；同质数据集合和明确表示坐标的简单 tuple 可保留。
- [ ] DECIMAL 定点化：目前是浮点实现，`0.06 ± 0.01` 得到 `0.06999999999999999`，漏掉 `l_discount = 0.07` 的行，使 Q6 与 TPC-H 官方答案（DuckDB 值）不一致——这是正确性缺口，不只是性能。
- [ ] 连接键推断广度：目前只做顶层 `AND` 与“所有 `OR` 分支共有的等式”；分支各自不同键、子查询内部的连接键都不推。
- [ ] 其余 14 条 TPC-H 查询的缺失特性：派生表、CTE、`CASE WHEN`、子查询表达式（Q2/Q4/Q7/Q8/Q9/Q12/Q13/Q14/Q15/Q17/Q20/Q21/Q22）。
- [ ] 索引节点二进制化（现每次等值查找 1.4 ms、候选集计算 82–272 ms 均花在 JSON 节点解码）与全定长二进制行编码。
- [ ] 单表扫描仍是最大头的绝对耗时：Q1 1136 ms / Q6 341 ms，对 SQLite 约 23–35×，瓶颈是逐行 JSON 解码 + Python 对象（详见 README 拆解）。

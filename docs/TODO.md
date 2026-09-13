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
- [x] 大数据演示资产：`data/showcase_v2.db`（137,036 行、4096B 页共 4858 页、9 表/11 索引/3 用户视图，库内文本已统一为 YourSQL）、`examples/showcase_init.sql` 初始化脚本和 `examples/showcase_demo.md` 全功能演示清单，附确定性生成器。
- [x] 仓库迁移与命名统一：包目录、入口、`YOURSQL_*` 环境变量与前端构建输出统一为 `yoursql`；复现用例移到 `examples/test_example.md` 与 `examples/test_example.sql`，演示库移到 `data/showcase_v2.db`。
- [x] 结果区整合消息、原始 JSON 和编译流水线；Token 逐行表格，AST/Plan 轻量节点画板。
- [x] 独立存储模式：只读页面地图、`MSP2` 双向槽式页的页头/槽目录/空闲区/记录区可视化；每格按槽目录项宽度固定为 6 B，不足整格的边界使用分段格，网格外框显示位置索引，多格区域按“完整 Hex/ASCII → 具体格 → 汇总”循环查看并保留关联槽位；项目内数据库已统一迁移到 30 B 对齐页头，包含标题栏区域定位、Buffer Pool、索引和明确限制。
- [x] 存储检查增量刷新：选中页支持单页刷新；页面地图通过 BufferPool 变更游标合并页头增量；缓存和索引目录按当前页签刷新，SQL 完成和存储模式停留期间自动触发轻量更新。
- [x] 页面地图轻量化：`fields=map` 按 500 页/批只取画图必需的页头字段，逐页目录归属与整张索引绑定表不再计入加载路径，表名/索引名延后到选中页按需获取；4860 页演示库的全量加载从 25–45 s 降到约 2 s。
- [x] 前端构建、自动化接口验收和第三方 BenchBox TPC-H Q6 回归。
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

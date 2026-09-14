# YourSQL 项目地图（目录与文件职责）

> 版本：`4c9d5fb`（本地 main 已合并远端 refactor 后的重构版）。
> 说明：重构后代码全部在 `yoursql/`，初版的 `database_system/` 已移除。

## 零、一句话总览

`yoursql/` 是后端（SQL 前端 → 计划 → 执行 → 存储四层 + 引擎协调层），
`web/` 是 React 工作台（编译产物落到 `yoursql/workbench_static/`），
`tests/` `benchmarks/` `examples/` `docs/` 是验证与文档，`data/` `*.db` 是运行时数据。

---

## 一、顶层文件

| 文件 | 职责 |
| --- | --- |
| `README.md` | 352 行主文档：快速开始、分层结构、支持范围、测试与基准、多引擎（SQLite/DuckDB）对标结论、服务接口、工作台、验收状态 |
| `AGENTS.md` | 开发规范：后端目录归属、注释规范、前端 UI 密度原则、提交规范、SQL 测试约束 |
| `pyproject.toml` | 打包与入口：`yoursql`（CLI）、`yoursql-web`（HTTP 工作台）两个 console script；pytest 配置 |
| `requirements.txt` | 开发依赖：pytest、pytest-benchmark、benchbox、duckdb；运行时代码零第三方依赖 |
| `.env.example` | 配置模板：库路径、监听地址、会话/超时/结果行数上限、页大小、缓存策略、CLI 默认账号；复制为 `.env` 使用 |
| `.gitattributes` | 统一 LF 行尾；`*.db`/`*.pdf`/图片/字体标为二进制 |
| `.gitignore` | 忽略 `__pycache__`、`*.db`、`data/*`、`dist/`、`yoursql/workbench_static/`、`.workbuddy/`、`draft_*/`、`项目目标/` |
| `mini.db` | 根目录遗留的 12KB 小库（与 `data/mini.db` 重复，已被 git 忽略） |
| `.pytest_cache/` | pytest 运行缓存 |

## 二、顶层目录

| 目录 | 职责 |
| --- | --- |
| `yoursql/` | 后端全部实现（49 个 py 文件），见第三节 |
| `web/` | Vite + React 19 工作台前端，见第四节 |
| `tests/` | pytest 用例（21 个文件），见第五节 |
| `benchmarks/` | TPC-H SF0.01 基准与跨引擎对拍，见第五节 |
| `examples/` | 可复现示例与演示库生成脚本 |
| `scripts/` | 辅助脚本：页头格式迁移、查询计划可视化导出 |
| `docs/` | 架构、设计记录、TODO、项目地图（本文件） |
| `data/` | 运行时数据库：`mini.db`（小库）、`showcase_v2.db`（20MB 演示库，显式纳入版本控制） |
| `项目目标/` | 课程原始资料：指导书 PDF、两份实习 PPT（非实现代码，已忽略） |
| `.workbuddy/` | 项目记忆（`memory/`）与计划书备份（`backup/`） |

---

## 三、后端 `yoursql/`

### 3.1 入口

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 公共入口，用 `__getattr__` 惰性导出，避免层间循环导入 |
| `cli.py` | 命令行：单条 SQL、脚本文件、交互模式；含结果格式化 |
| `web.py` | 启动带认证的本地工作台 + 兼容 REST 服务（`yoursql-web` 入口） |

### 3.2 `common/` — 跨层共享（无业务逻辑）

| 文件 | 职责 |
| --- | --- |
| `types.py` | 核心值对象：`DataType` `TableId` `PageId` `RowId` `Value` `Column` `Schema` `TableStats` `ExecutionResult`，以及 `compare_values`/`sql_truth` |
| `errors.py` | 统一异常层次：`YourSQLError` → Lexer/Parser/Binder/Catalog/Storage/Execution/Authorization Error（错误信息带行列） |
| `config.py` | 配置与日志：`.env` 读取、`DatabaseConfig`（页大小/缓存/替换策略）、`RuntimeConfig`（会话与请求边界）、`configure_logging` |
| `contracts.py` | 跨边界值类型别名（SQL 结果、HTTP JSON 的动态数据协议） |
| `trace.py` | `ExecutionTrace`：按请求采集真实执行路径，普通 SQL 调用不启用 |

### 3.3 `sql/` — SQL 语言前端（不碰存储）

| 文件 | 职责 |
| --- | --- |
| `lexer.py` | 词法分析，输出带行列位置与解码字面量的 Token |
| `parser.py` | 递归下降解析器，支持单条与脚本多条；`parse_recovering` 提供 panic-mode 错误恢复（语句级 + 投影项级同步点） |
| `ast.py` | AST 节点定义：只描述语句，不做目录访问与执行 |
| `binder.py` | 语义绑定与基础校验：名称解析、类型/聚合合法性，基于目录只读协议 |
| `compiler.py` | 串联 Lexer → Parser → Binder → 计划生成的编译入口（`compile_sql`） |
| `diagnostics.py` | `Diagnostic`（阶段/错误码/位置/期望）与 `ParseOutcome`（语句 + 诊断，可渲染文本或 JSON） |
| `__init__.py` | 前端公共导出 |

### 3.4 `planner/` — 计划与优化

| 文件 | 职责 |
| --- | --- |
| `logical.py` | 由绑定 AST 构造逻辑计划（`plan_from_statement`） |
| `optimizer.py` | 核心优化器（1118 行）：`StatisticsStore` 统计、`PlanCache` 计划缓存、代价值对象，以及**具名规则框架**（`RewriteRule` / `DEFAULT_RULES` 五条规则、`disabled_rules` 开关、命中规则写入 `plan.properties["rules"]`） |
| `cost.py` | 代价模型公共值对象与校准常数（`CostEstimate`） |
| `physical.py` | 物理计划节点 `PlanNode`/`PhysicalPlanNode`、可解释序列化与 `as_physical`，以及图形化输出 `label_lines()` / `to_mermaid()` / `to_dot()` |
| `__init__.py` | 计划层导出 |

### 3.5 `execution/` — 执行

| 文件 | 职责 |
| --- | --- |
| `executor.py` | Volcano 算子：Values/SeqScan/Filter/Project/Sort/Limit/NestedLoopJoin/Aggregate（不依赖 SQL AST） |
| `evaluator.py` | 表达式编译、常量折叠与运行时求值（含 LIKE、日期函数、聚合） |
| `query.py` | 最重的模块（2292 行）：SELECT 的连接、扫描、索引访问、投影编排；哈希连接/索引嵌套循环落在这里 |
| `__init__.py` | 执行层导出 |

### 3.6 `storage/` — 页式存储

| 文件 | 职责 |
| --- | --- |
| `page.py` | 固定大小页、页头、槽目录（`PageType`/`Page`/`SlottedPage`，662 行） |
| `disk.py` | 单文件页式磁盘管理与 superblock（`DiskManager`/`SingleFileDatabase`） |
| `buffer.py` | 固定容量页缓存，支持 LRU/FIFO（`BufferFrame`/`BufferPool`） |
| `heap.py` | 基于槽式页的变长记录堆表（`TableHeap`） |
| `index.py` | 可持久化 B+Tree 与索引管理器（1825 行，`MBIX` JSON 负载）+ `index_page_info` |
| `__init__.py` | 存储层导出 |

### 3.7 `engine/` — 引擎协调与目录

| 文件 | 职责 |
| --- | --- |
| `catalog.py` | 系统目录：表/字段/索引/视图元数据及其 JSON 表示（`Catalog`、`TableMetadata`、`IndexMetadata`、`ViewMetadata`） |
| `system_catalog.py` | 基于 Heap 的内部权限表 + admin-only 只读系统视图（角色、用户、授权） |
| `runtime/database.py` | `Database` 运行时协调器：编译、优化、执行、页式持久化与生命周期 |
| `runtime/commands.py` | 1170 行：SQL 语句的授权、DDL、DML、索引变更处理（`DatabaseCommandMixin`） |
| `security/auth.py` | 教学版 RBAC：用户、角色、对象权限、口令哈希 |
| `security/manager.py` | 兼容名称，薄封装 RBAC + 审计日志（`SecurityManager`） |
| `security/session.py` | 会话身份与权限入口（`Session`） |
| `security/audit.py` | 结构化 JSON Lines 审计日志（`AuditLog`） |
| `services/http.py` | 标准库 HTTP 服务与工作台 REST API，默认只监听本机（690 行） |
| `services/workbench.py` | 工作台服务：有界任务队列、持久化 RBAC 认证、查询历史（984 行） |
| `services/workbench_sql.py` | 工作台 SQL 边界：语句切分、脱敏、错误锚点、阶段采集、结果列类型 |
| `services/inspection.py` | 存储只读检查：页头总览、Buffer Pool 快照、索引快照、单页/索引详情 |
| `services/ssh.py` | 无第三方依赖的 stdio SSH 协议适配，可挂到 OpenSSH 强制命令 |

---

## 四、前端 `web/`

| 文件 | 职责 |
| --- | --- |
| `package.json` | React 19 + Vite 7 + CodeMirror 6 + lucide-react；`dev`/`build`/`test`/`format` 脚本 |
| `vite.config.ts` / `tsconfig.json` / `index.html` | 构建配置与入口页 |
| `public/favicon.svg` | 站点图标 |
| `src/main.tsx` | React 挂载点 |
| `src/App.tsx` | 顶层编排：状态、请求编排、模式切换（查询 / 存储） |
| `src/api.ts` | REST 客户端（`api`、`ApiError`、`upload`）与耗时格式化 |
| `src/sql.ts` | 语句切分（字符串/引用名/注释规则与后端 Lexer 对齐） |
| `src/view-classes.ts` | 条件类名拼接纯函数，避免超长模板字符串（AGENTS.md 强制） |
| `src/storage-selection.ts` | 页面地图点击语义（一次点击切换、不打断选中） |
| `src/app/types.ts` / `constants.ts` | App 级状态模型与默认值工厂 |
| `src/types/{common,catalog,query,storage}.ts` | 各领域的后端响应模型 |
| `src/components/auth/Login.tsx` | 登录页与登录前数据库入口 |
| `src/components/database/DatabasePickerDialog.tsx` + `utils.ts` | 选库/新建/导入流程与路径规则 |
| `src/components/layout/*` | 页头、权限弹窗、Toast、底部状态栏 |
| `src/components/query/*` | `QueryWorkspace`（布局编排）、`SqlEditor`（CodeMirror）、`ResultPanel`、`ExplainResult`、`PipelinePanel`（流水线）、`StageCanvas`（计划 SVG 拓扑）、`AstTree`、`JsonTree`、`HistoryPanel` |
| `src/components/schema/SchemaBrowser.tsx` | 表/视图/列目录浏览器 |
| `src/components/storage/*` | `StoragePanel`（页面地图/缓存/索引/详情）、`PageGrid`（字节可视化）、`StorageTooltip`、`model.ts` |
| `src/styles/*.css` | `index.css` 汇总，`layout.css`（壳层）、`workbench.css`（工作台增量）、`schema-browser.css`（对象树） |
| `src/*.test.ts` | vitest 单测：类名拼接、页面点击语义、布局常量、选库工具 |

> 注意：`yoursql/workbench_static/`（HTTP 服务读取的前端产物目录）当前**不存在**，
> 需要先 `cd web && npm run build`，再把 `dist/` 内容放到 `yoursql/workbench_static/`
> （`tests/test_services.py::test_workbench_static_directory_matches_vite_output` 校验这个约定）。
> 未构建时 `yoursql-web` 只能提供 API，开发期请用 `npm run dev` + CORS 白名单。

---

## 五、测试、基准与示例

### `tests/`（21 个文件，pytest）

| 文件 | 覆盖点 |
| --- | --- |
| `test_sql.py` | 词法/语法错误、AST 节点构建、编译入口 |
| `test_common.py` | Schema 大小写不敏感与取值转换 |
| `test_config.py` | 运行环境配置回归 |
| `test_storage.py` | 页、磁盘、缓存、堆表基础行为 |
| `test_storage_hot_path.py` | 存储热路径：codec 解码、索引页读取（`from_page(verify=False)`） |
| `test_disk_peek.py` | 只读 peek 必须看到未 flush 的缓冲写入 |
| `test_database.py` | 端到端 SQL：建表、增删改查、连接、函数、别名、DISTINCT（20 个用例，380 行） |
| `test_planner_layers.py` | 编译/优化/执行层的目录边界回归 |
| `test_optimizer_rules.py` | 优化规则结果正确性与访问路径变化；规则清单可枚举、可单独关闭、命中记录、缓存命中回读 |
| `test_plan_visualization.py` | 计划图形化：Mermaid/DOT 图结构与实体转义、标签截断、HTML 导出、CLI `--plan`/`--rules`/`--disable-rule` |
| `test_error_recovery.py` | 多错误报告：词法/语法诊断收集、两条恢复入口的契约与只读性 |
| `test_decimal.py` | DECIMAL 定点数的字面量、比较、聚合与落盘往返 |
| `test_sql_features.py` | 派生表/CTE/CASE/CAST/EXISTS/相关子查询与自连接歧义键回归 |
| `test_covering_index.py` | 覆盖索引（`CREATE INDEX ... INCLUDE`）与 IndexOnlyScan |
| `test_index_cache.py` | 索引候选集缓存：命中、写入失效、数据变化 |
| `test_join_strategies.py` | 哈希连接 / 索引嵌套循环 / 嵌套循环的正确性与选择 |
| `test_streaming_and_batch_load.py` | 流式执行、连接谓词下推、批量装载 |
| `test_expression_compiler.py` | 表达式编译闭包与逐行解释器等价性 |
| `test_extensions.py` | B+Tree 精确/范围/唯一/复合索引等扩展能力 |
| `test_services.py` | HTTP 服务、SSH 适配、工作台静态目录约定 |
| `test_workbench.py` | 真实 HTTP + 临时库 + 持久化权限验收工作台（608 行） |

### `benchmarks/`

| 文件 | 职责 |
| --- | --- |
| `compare_tpch_q6.py` | 同一份 TPC-H SF0.01 数据、同一条 Q6 对照 YourSQL / SQLite / DuckDB |
| `compare_tpch_queries.py` | 多查询版跨引擎对标 |
| `run_tpch_queries.py` / `run_tpch_query.py` | 多查询基准与子进程计时执行器 |
| `run_tpch_coverage.py` | 22 条查询的编译/执行覆盖率报告（`--mode compile\|exec`） |
| `run_benchbox_tpch.py` | 用 BenchBox 真实 workload 测 YourSQL |
| `verify_tpch_values.py` | 逐值对拍：YourSQL 结果 vs 同数据 SQLite（16 条，Q6 记为已知口径差异） |
| `reports/*.json` | 上述基准的历史结果快照（4 份） |

### `examples/` 与 `scripts/`

| 文件 | 职责 |
| --- | --- |
| `create_showcase_db.py` | 生成覆盖工作台主要能力的演示数据库 |
| `showcase_init.sql` / `showcase_demo.md` | 全功能演示脚本与 13 节演示手册 |
| `test_example.sql` / `test_example.md` | 优化规则的最小可复现用例与预期结果（AGENTS.md 要求同步维护） |
| `scripts/migrate_page_header.py` | 一次性迁移：页头 26B → 带 4B 对齐保留区的 30B |
| `scripts/plan_visualize.py` | 把优化后的计划导出为 text / Mermaid / DOT / JSON / 自包含 HTML（`--disable-rule` 可现场对比规则开关） |

### `docs/`

| 文件 | 职责 |
| --- | --- |
| `ARCHITECTURE.md` | 系统架构与验收边界：数据流、目录约定、页/文件格式、查询与优化、验收命令 |
| `WORKBENCH_DESIGN.md` | 工作台设计记录：产品形态、视觉基准、组件划分、存储刷新策略 |
| `TODO.md` | 分期开发清单与完成状态、下一步缺口 |
| `PROJECT_MAP.md` | 本文件 |
| `《大型平台软件设计实习》指导书.pdf` | 课程原件 |

---

## 六、值得注意的几点

1. **`yoursql/workbench_static/` 缺失**：打包配置已声明该 package-data，但仓库里没有产物；纯 `pip install` 后 Web 前端不可用，需先构建前端。
2. **数据库文件冗余**：根 `mini.db` 与 `data/mini.db` 内容疑似重复，均为被忽略的本地产物；`data/showcase_v2.db`（20MB）被显式纳入版本控制，体积偏大。
3. **`draft_6ce81db8_folder/`** 是 PPT 生成的临时目录，已由 `.gitignore` 的 `draft_*/` 覆盖，可随时清理。
4. **两个 2000 行级热点文件**：`storage/index.py`(1825) 与 `execution/query.py`(1904)，是后续改动与评审的重点区域。
5. **分层边界已被测试固化**：`tests/test_planner_layers.py` 会拦截跨层乱依赖（这正是 AGENTS.md 的硬约束）。

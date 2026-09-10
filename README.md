# YourSQL (MiniSQL)

一个从零实现的小型数据库系统，用纯 Python 编写、零第三方依赖，贯通编译原理、操作系统与数据库三门课程。

A from-scratch database system in pure Python, covering the full path from SQL text to disk.

## 快速开始

```bash
# 交互式 REPL
python -m database_system.cli.main --db data/mini.db

# 执行脚本
python -m database_system.cli.main --file database_system/demo.sql --plan

# 全链路演示（Token → AST → Plan → 优化前后 → 执行结果）
python -m database_system.cli.pipeline_demo

# 全部测试（96 项）
python -m database_system.tests.run_all
```

要求 Python 3.10+，无第三方依赖。

## 数据库图形工作台

队友首次使用请阅读 **[前端启动与团队协作指南](database_system/web/README.md)**，包含克隆后的启动步骤、示例数据、目录分工及常见问题。

提供类似 SQLyog 的本地浏览器界面，直接连接本项目的 MiniSQL 引擎：

```bash
python -m database_system.web.server --db data/workbench.db
```

浏览器打开 http://127.0.0.1:8765 。使用 `--db` 指定已有数据库文件，使用
`--port 8766` 修改端口。关闭服务按 Ctrl+C。避免其他 CLI 或服务同时写入同一个数据库文件。

- 左侧搜索数据表，单击查看字段结构，双击生成查询；结构视图可直接查询前 100 行。
- 多查询标签、SQL 文件打开和保存；Ctrl / Command + Enter 执行，优先执行选中内容。
- 支持多条 SQL 的结果切换、分阶段错误提示、优化前后执行计划和当前页面查询历史。
- 查询结果抽屉提供编译调试 Tab，可依次查看 Token、AST、语义检查、优化前计划、优化后计划和执行器输出；多条 SQL 可通过语句选择器逐条查看。
- 表格展示与 CSV 导出最多包含前 1,000 行，并提示总行数；引擎仍会执行完整查询。
- 新数据库为空，右上角“演示用例”下拉框可以载入不同功能的 SQL 测试脚本，由用户选择后执行。

该界面操作 MiniSQL 数据文件，不连接 MySQL。支持范围与引擎一致，暂无 JOIN、聚合、
事务回滚和表格内直接编辑。多条语句可能部分成功；执行出错后应检查每条语句的结果。
查询标签和历史仅保留在当前页面，刷新前可通过“保存 SQL”下载脚本。

## 一条完整链路

```
SQL → Token → AST → 语义检查 → 逻辑计划 → 优化 → 执行引擎 → 数据页 / 磁盘
```

| 层 | 模块 | 内容 |
|---|---|---|
| 编译器前端 | `sql_compiler/` | 词法分析、递归下降语法分析、语义分析与类型检查、Catalog（符号表）、逻辑计划生成、规则式优化（6 条） |
| 存储系统 | `storage/` | 4KB 定长页与槽位目录、磁盘页分配 / 空闲页链表、LRU / FIFO 缓冲池与命中统计 |
| 执行引擎 | `engine/` | 记录二进制序列化、表堆页链表、系统目录作为特殊表持久化、火山模型算子（SeqScan / Filter / Project / OrderBy / Limit / Insert / Delete / Update） |
| 命令行 | `cli/` | REPL、脚本模式、全链路输出演示 |
| 图形工作台 | `web/` | 本地 HTTP 服务、SQL 编辑器、表结构浏览、查询结果与计划、CSV 导出 |
| 测试 | `tests/` | 词法 / 语法 / 语义 / 存储 / 端到端 / Fuzz |

## 支持范围

CREATE TABLE / INSERT（多行）/ SELECT / DELETE / UPDATE / DROP TABLE / EXPLAIN；
WHERE、DISTINCT、ORDER BY、LIMIT、别名、`IS NULL`、算术与比较表达式、AND / OR / NOT 与括号。
类型：INT、VARCHAR(n)、BOOL、NULL。不支持 JOIN、GROUP BY、聚合与子查询。

## 文档

- 详细设计说明：[`database_system/README.md`](database_system/README.md)
- 文法与语义规则：[`database_system/grammar.md`](database_system/grammar.md)
- 项目简介（中 / 英）：[`database_system/ABOUT.md`](database_system/ABOUT.md)
- 需求与验收标准：[`《大型平台软件设计实习》项目计划书.md`](《大型平台软件设计实习》项目计划书.md)
- 端到端演示脚本：[`database_system/demo.sql`](database_system/demo.sql)

## 测试

```bash
python -m database_system.tests.run_all
```

96 个用例全部通过，覆盖正常执行、词法 / 语法 / 语义错误定位、边界输入、重启持久化、
小缓冲池多页扫描、Web 工作台接口，以及随机 SQL 与字符级变异的 Fuzz 测试（不崩溃、不误收、不误拒）。

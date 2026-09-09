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

# 全部测试（92 项）
python -m database_system.tests.run_all
```

要求 Python 3.10+，无第三方依赖。

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

92 个用例全部通过，覆盖正常执行、词法 / 语法 / 语义错误定位、边界输入、重启持久化、
小缓冲池多页扫描，以及随机 SQL 与字符级变异的 Fuzz 测试（不崩溃、不误收、不误拒）。

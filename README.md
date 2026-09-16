# YourSQL

面向数据库内核学习与课程设计的可持久化迷你数据库。项目使用 Python 实现 SQL 前端、页式存储、Buffer Pool、B+Tree 索引、执行引擎、RBAC，以及 CLI、HTTP/SSH 和 Web 工作台。

YourSQL 适合学习、实验和演示，不是面向生产部署的数据库。

## 快速开始

环境要求：Python 3.11 或更高版本。核心运行时仅依赖 Python 标准库；测试和基准依赖见 [`requirements.txt`](requirements.txt)。

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

启动交互式 CLI：

```powershell
python -m yoursql.cli --database .\data\demo.db
```

执行单条 SQL 或脚本：

```powershell
python -m yoursql.cli `
  --database .\data\demo.db `
  --sql "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT); INSERT INTO student VALUES (1, 'Alice', 20); SELECT * FROM student;"

python -m yoursql.cli `
  --database .\data\demo.db `
  --file .\examples\test_example.sql `
  --json
```

Python API：

```python
from yoursql.engine.runtime.database import Database

with Database("data/demo.db") as database:
    database.execute("CREATE TABLE greeting(id INT, text VARCHAR);")
    database.execute("INSERT INTO greeting VALUES (1, 'hello');")
    result = database.execute("SELECT * FROM greeting;")
    print(result.rows)
```

CLI 还支持 `--user`、`--password` 和 SSH stdio 模式 `--stdio`。数据库文件首次打开时创建，写入完成后持久化，重新打开同一个文件即可恢复数据。

## SQL 与权限能力

当前支持的主要能力：

- DDL/DML：`CREATE/DROP TABLE`、只读 `CREATE/DROP VIEW ... AS SELECT`、`CREATE/DROP INDEX`、`INSERT`、`UPDATE`、`DELETE`；
- 查询：`SELECT`、`WHERE`、`ORDER BY`、`LIMIT/OFFSET`、`DISTINCT`、聚合、`GROUP BY/HAVING`；
  支持 `INNER/LEFT/RIGHT/FULL/CROSS JOIN`、CTE、派生表、`CASE WHEN`、`CAST`、
  `IN/NOT IN` 子查询、`UNION/UNION ALL` 和 `EXPLAIN`；
- 目录查看：`SHOW TABLES/VIEWS/COLUMNS/INDEXES/CREATE`、`DESC/DESCRIBE`；
- 权限管理：用户、角色、`GRANT`、`REVOKE` 和 `SHOW GRANTS`。权限目录使用隐藏系统表保存，并提供 admin-only 的只读系统视图。

事务使用 `BEGIN`、`COMMIT`、`ROLLBACK` 和 `SET TRANSACTION ISOLATION LEVEL`；默认自动提交，
支持 `serializable`（默认）与 `read_committed`。并发控制采用表级锁和严格两阶段封锁，
支持锁升级、死锁检测与超时。页级 WAL 提供未提交事务回滚和崩溃恢复。

视图只保存定义与输出模式，不分配数据页；访问时重新执行定义查询，不支持写操作或建索引。`DECIMAL` 已提供定点语义并可无损落盘，但整体仍属于教学实现。TLS、角色继承、列级权限和行级权限等生产级安全特性尚未实现。

## Web 工作台

工作台由同一个标准库 HTTP 进程托管，默认监听 `http://127.0.0.1:8080`。首次运行时构建前端：

```powershell
Copy-Item .env.example .env
cd web
npm ci
npm run build
cd ..

python -m yoursql.web --database .\data\showcase_v2.db --port 8080
```

浏览器打开 <http://127.0.0.1:8080>，演示库默认管理员为 `admin / admin`。工作台提供 SQL 编辑与执行、结果分页和导出、查询历史、AST/计划/执行统计查看、权限查看、页面与索引检查，以及受权限保护的性能诊断。

修改 `web/` 源码时，可运行 `npm run dev` 启动 Vite 开发服务器，并在后端追加：

```powershell
python -m yoursql.web `
  --database .\data\showcase_v2.db `
  --allow-origin http://127.0.0.1:5173
```

前端构建产物写入 `yoursql/workbench_static/`，该目录属于本地构建产物，不提交到 Git。

标准库服务还提供 `GET /health`、`GET /metrics` 和 `POST /sql`（JSON body 为 `{"sql": "..."}`）；现代工作台接口位于 `/api/*`，需要认证。

## 演示数据与示例

仓库自带 [`data/showcase_v2.db`](data/showcase_v2.db)，包含 9 张业务表、137,036 行、用户视图、索引和多个演示账号。生成器会重建同样的内容和结构：

```powershell
python -m examples.create_showcase_db --force
```

生成器使用 4096B 页和 128 页 Buffer Pool；密码哈希带随机盐，因此重建后文件 SHA-256 不保证相同。完整演示步骤、账号权限和 SQL 见 [`examples/showcase_demo.md`](examples/showcase_demo.md)。

SQL 语义、优化规则、连接策略、子查询复用和预期结果的最小复现用例见
[`examples/test_example.md`](examples/test_example.md)。Buffer Pool 实验见
[`examples/buffer_pool_lab_test.sql`](examples/buffer_pool_lab_test.sql) 和
[`benchmarks/compare_buffer_pool_lab.py`](benchmarks/compare_buffer_pool_lab.py)。
其中 [`examples/buffer_pool_2q_effect.sql`](examples/buffer_pool_2q_effect.sql)
专门用于在 SQL 工作台中对照 2Q 热点保留与页面类型保护，默认使用 512 行的
`data/buffer_pool_lab_demo.db`，两个区段需要分别重置统计后执行；原有的
`buffer_pool_lab_large.db` 仍可用于压力测试，但不适合观察策略差异。

## 配置

复制 [`.env.example`](.env.example) 后按需修改。配置优先级为：命令行参数 > 系统环境变量 > 根目录 `.env` > 程序默认值。

常用配置包括：

- Web：`YOURSQL_DATABASE`、`YOURSQL_HOST`、`YOURSQL_PORT`、`YOURSQL_ALLOWED_ORIGINS`；
- 查询边界：`YOURSQL_QUERY_TIMEOUT_SECONDS`、`YOURSQL_MAX_RESULT_ROWS`、`YOURSQL_MAX_SQL_CHARS`；
- 存储：`YOURSQL_PAGE_SIZE`、`YOURSQL_BUFFER_POOL_SIZE`、`YOURSQL_REPLACEMENT_POLICY`；
- 存储格式：`YOURSQL_PAYLOAD_CODEC=json|manual`。新数据库默认使用 `json`；已有数据库以文件内记录为准；
- 兼容入口：`YOURSQL_LEGACY_ANONYMOUS=true` 可为旧教学客户端开启匿名 `/sql`，工作台默认关闭。

事务相关配置包括 `wal_enabled`、`lock_mode`、`lock_timeout_seconds` 和 `default_isolation`；CLI 可通过 `--no-wal` 与 `--txn-status` 控制或查看事务状态。

## 架构概览

```text
CLI / Python API / HTTP / SSH
              │
        Session + RBAC
              │
Lexer → Parser/AST → Binder → Logical/Physical Plan → Executor
                                                        │
                                  Catalog + TableHeap + B+Tree
                                                        │
                                  Page → BufferPool → Disk
```

| 目录 | 职责 |
| --- | --- |
| `yoursql/sql` | 词法、语法、AST、绑定和编译入口 |
| `yoursql/planner` | 逻辑/物理计划、优化规则、统计和代价估算 |
| `yoursql/execution` | 表达式求值、查询编排和 Volcano 执行算子 |
| `yoursql/storage` | Page、Heap、Disk、Buffer Pool 和 B+Tree |
| `yoursql/engine` | Catalog、运行时协调、安全和 HTTP/SSH/Workbench 服务 |
| `web` | React/Vite 数据库工作台 |
| `tests` | 后端单元与集成测试 |

## 验证与基准

后端测试：

```powershell
python -m pytest -q
```

前端检查：

```powershell
cd web
npm test
npm run format:check
npm run build
cd ..
```

可选的 TPC-H SF0.01 实验：

```powershell
python -m benchmarks.run_benchbox_tpch --iterations 5 --force
python -m benchmarks.compare_tpch_q6 --iterations 5
```

结果写入 `benchmarks/reports/`。这些脚本用于学习和引擎对照，不代表完整 TPC-H 官方成绩；不同引擎的语义、运行环境和计时口径请以报告为准。

## 文档

- [系统架构与验收边界](docs/ARCHITECTURE.md)
- [开发清单与未完成项](docs/TODO.md)
- [工作台设计记录](docs/WORKBENCH_DESIGN.md)
- [全功能 Showcase](examples/showcase_demo.md)
- [SQL/优化复现用例](examples/test_example.md)

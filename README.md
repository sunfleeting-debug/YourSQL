# YourSQL

面向《大型平台软件设计实习》的可持久化迷你数据库：用 Python 标准库实现 SQL 编译器、页式存储、缓存、执行引擎、索引与服务接口。

## 快速开始

需要 Python ≥ 3.11，依赖用 `uv` 管理：

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
python -m yoursql.cli --database demo.db
```

交互模式里直接执行 SQL：

```sql
CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT);
INSERT INTO student VALUES (1, 'Alice', 20);
SELECT id, name FROM student WHERE age > 18;
```

也可以走 Python API：

```python
from yoursql.engine.database import Database

with Database("demo.db") as db:
    db.execute("CREATE TABLE t(id INT, value VARCHAR);")
    db.execute("INSERT INTO t VALUES (1, 'hello');")
    print(db.execute("SELECT * FROM t;").rows)
```

## 分层结构

| 模块 | 职责 |
| --- | --- |
| `yoursql.common` | 类型、Schema、RowId、执行结果、统一异常 |
| `yoursql.sql` | Lexer → Parser → Binder → Plan，输出可解释的计划 |
| `yoursql.storage` | 固定页、单文件磁盘、槽式页、LRU/FIFO BufferPool、TableHeap、B+Tree |
| `yoursql.engine` | Catalog、Volcano Executor、优化器、RBAC、Session、HTTP/SSH |
| `tests` | 单元与集成测试（持久化、权限、工作台 HTTP） |
| `benchmarks` | BenchBox TPC-H 数据生成与 Q6 workload |

## 支持范围

**SQL**：`CREATE/DROP TABLE`、只读 `CREATE/DROP VIEW ... AS SELECT`、`INSERT`、`SELECT`、`UPDATE`、`DELETE`；支持条件、排序、分页、`DISTINCT`、连接、`GROUP BY/HAVING`、聚合、`IN (SELECT ...)`、`UNION`、索引与 `EXPLAIN`。

**Catalog 查看**：`DESC/DESCRIBE`、`SHOW TABLES/VIEWS`、`SHOW COLUMNS/FIELDS`、`SHOW INDEX/INDEXES`、`SHOW CREATE TABLE/VIEW`，均按目标对象的 `SELECT` 权限校验：

```sql
DESC student;
SHOW INDEXES FROM student;
SHOW CREATE TABLE student;
```

**视图**：只持久化定义与输出模式，不分配数据页，访问时重新执行定义查询；不支持写操作或建索引。

**权限**：默认账号 `admin/admin`；用户、角色与授权持久化在四张隐藏 Heap 表（`_sys_users`、`_sys_roles`、`_sys_role_members`、`_sys_privileges`），不进入 `SHOW TABLES` 与用户 SQL 命名空间。admin 另可通过只读系统视图 `sys_users`、`sys_roles`、`sys_role_members`、`sys_privileges` 查看权限目录，`sys_users` 不含 `password_hash`；系统视图不分配数据页、不可写。

```sql
CREATE ROLE reader;
CREATE USER alice IDENTIFIED BY 'secret' DEFAULT ROLE reader;
GRANT SELECT ON student TO ROLE reader;
SHOW GRANTS FOR USER alice;
```

当前不含 TLS、角色继承、列/行级权限等生产级安全特性。

**持久化**：库与目录都落在页面文件里，重新打开同一个 `.db` 即可读回数据；页大小由 superblock 识别，不依赖调用方传 `DatabaseConfig`。

## 测试与基准

```powershell
python -m pytest -q                                     # 87 passed
cd web; npm test                                        # 4 passed
python -m benchmarks.run_benchbox_tpch --iterations 5 --force
```

`pytest` 覆盖公共层、存储、SQL、索引、服务与工作台。SQL 语义与优化规则的对外复现用例维护在 [`examples/test_example.md`](examples/test_example.md)（配套 [`examples/test_example.sql`](examples/test_example.sql)），按文档步骤可复现全部断言。

第二条命令用第三方 BenchBox 0.4.0 跑 TPC-H SF0.01 Q6：数据来自 `TPCH.generate_data()`，SQL 来自 `TPCH.get_query(6, dialect="sqlite")`，YourSQL 只负责加载与执行。结果写入 `benchmarks/reports/tpch_sf001_q6.json`；这是 Q6 子集实验，不等同官方 QphH@Size 成绩。数据集与运行产物落在 `benchmarks/third_party/`、`benchmarks/results/`，不入库。

同一份数据另有一份仓库外的 MiniOB `3259c37` 对照：`tmp/miniob` 未随仓库提供，需在本机重建才能复现。关闭逐行 TRACE 日志后 MiniOB 平均 `0.2164s`，YourSQL 平均 `1.9396s`，约慢 `8.96x`；两者结果 `734493.63` 与 `734493.7281`，差异来自 DECIMAL→FLOAT 映射。详见 [MiniOB 对照报告](benchmarks/reports/tpch_sf001_q6_miniob_compare.json)。

## 服务接口

标准库 HTTP 服务提供 `GET /health`、`GET /metrics` 与 `POST /sql`（JSON body 为 `{"sql": "..."}`）：

```powershell
python -c "from yoursql.engine.database import Database; from yoursql.engine.http import serve_http; serve_http(Database('demo.db'))"
```

SSH 场景执行 `python -m yoursql.cli --database demo.db --stdio`，由 OpenSSH 强制命令转发逐行 SQL。

## Web 数据库工作台

构建后的前端由同一个标准库进程托管，原有 `/health`、`/metrics`、`/sql` 保持不变。

```powershell
Copy-Item .env.example .env
cd web; npm install; npm run build; cd ..
python -m yoursql.web --database data/showcase_v2.db --port 8080
# 浏览器打开 http://127.0.0.1:8080，初始账号 admin / admin
```

不传 `--database` 时默认 `./data/workbench.db`；`npm run build` 输出到 `yoursql/workbench_static`。改前端源码时用 `npm run dev`（5173），另一个终端给后端加 `--allow-origin http://127.0.0.1:5173`。前端格式化用 `npm run format`，校验用 `npm run format:check`（Prettier 配置在 `web/.prettierrc.json`）。

配置优先级为命令行参数 > 系统环境变量 > 根目录 `.env` > 程序默认值；边界参数统一 `YOURSQL_*` 前缀，模板见 [`.env.example`](.env.example)（其中 `YOURSQL_TRACE_MAX_STEPS` 留空表示不限流水线步数）。

**工作区**：可折叠数据库对象栏（表 / 用户视图 / 系统视图分组）、CodeMirror SQL 编辑器、当前语句与全部语句执行、结果分页与 CSV/JSON 导出、查询历史、权限查看、错误行列定位。结果区“流水线”页签显示后端 Token 表；AST、逻辑计划、优化计划与物理阶段提供可缩放算子图和 JSON/文本视图。物理阶段仍复用 PlanNode，但优化出的 SeqScan/IndexScan 会进入实际扫描路径。

**存储检查**（需要全库 `SELECT` + `SECURITY`）：页面页签常驻右侧详情栏，开关块详情不改变地图列数与滚动位置。页面地图按真实字节边界画固定小方格（当前槽目录项 6 B/格），页头、`MSP2` 双向槽式页头、槽目录、空闲区与记录区分别着色，并按 `free_space / page_size` 编码使用率；点击多格区域按“区域 Hex/ASCII → 具体格 → 区域汇总”循环，单格直接看详情，HEAP 页支持槽位下拉联动定位。地图仅在首次进入或手动刷新时全量加载，按 `fields=map` 以 500 页/批拉取，只取画图必需字段（表名/索引名在选中页时补齐，4860 页演示库约 2 s）；其余刷新走单页、页头变更游标、缓存或索引目录接口。索引基于落盘 B+Tree，选中表会懒加载其索引物理页并在地图上联动高亮根、内部与叶子页。内部权限页默认只对 `admin` 显示原始字节，其他安全审计会话显示 `MASKED`。

**数据库面板**：可用路径或本机文件选择器打开已有库，也可新建空白库并自选页大小、缓存页数与 LRU/FIFO 淘汰策略。登录前只能选当前服务目录中的 `.db`；登录后切换需要 `SECURITY` 权限，执行中的任务会阻止切换，切换后旧会话失效。选中本机文件会先复制到当前数据库目录再切换。选择器列出的是当前库所在目录下的 `.db`。

**演示库**：[`data/showcase_v2.db`](data/showcase_v2.db) 含 9 张表、137,036 行、4096B 页（4858 页）、11 个索引、3 个用户视图与 `analyst`/`support`/`auditor` 账号；`python -m examples.create_showcase_db --force` 可重建出同规格的库。配套 [`examples/showcase_init.sql`](examples/showcase_init.sql) 与 [`examples/showcase_demo.md`](examples/showcase_demo.md)。

**API**（统一返回 `{ok, request_id, data, error}`）：

- 身份：`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/session`、`GET /api/permissions`
- 元数据：`GET /api/dialect`、`GET /api/databases`、`GET /api/databases/available`、`POST /api/databases/select|create|import`、`GET /api/tables/{name}`
- 登录前：`GET /api/databases/available-before-login`、`POST /api/databases/select-before-login|import-before-login`
- SQL：`POST /api/validate`、`POST /api/queries`、`GET /api/queries/{id}`、`GET /api/queries/{id}/results/{index}`、`POST /api/queries/{id}/cancel`
- 历史：`GET /api/history`
- 存储：`GET /api/storage`、`GET /api/storage/changes`、`GET /api/storage/pages/{page_id}`、`GET /api/storage/cache`、`POST /api/storage/cache/policy`、`GET /api/storage/indexes`、`GET /api/storage/indexes/{name}`

切换与新建库用 `path` 字段（新建还可传 `page_size`、`buffer_pool_size`、`replacement_policy`）；导入用二进制 `.db` 文件体加 `X-YourSQL-File-Name` 文件名头。缓存响应的 `buffer_pool.eviction_order` 是当前策略下的升序淘汰队列，Pin 中的页不进入队列；拥有 `SECURITY` 权限时可在线切换 LRU/FIFO，切换不清空已有缓存帧，从下一次淘汰开始生效。

## 验收状态

- 后端 `python -m pytest -q`：87 passed。
- 前端 `npm test`：4 passed（块点击语义、常驻右栏布局）；`npm run build` 成功。
- 演示库 `data/showcase_v2.db`：CLI（`SHOW TABLES`、分组聚合、`EXPLAIN` 走 `IndexScan`、`analyst` 权限登录）与工作台 HTTP 链路（登录 → `/api/queries` 异步任务 → 结果分页 → 存储快照）均通过。
- BenchBox TPC-H SF0.01 Q6：PASS，`734493.7281`，平均 `2.2167s`、中位数 `2.2323s`（CPython 3.12.13 / Windows 11），摘要见 `benchmarks/reports/tpch_sf001_q6.json`。

## 设计文档

[系统架构](docs/ARCHITECTURE.md) · [开发清单](docs/TODO.md) · [工作台设计](docs/WORKBENCH_DESIGN.md) · 课程指导书（`docs/` 内 PDF）

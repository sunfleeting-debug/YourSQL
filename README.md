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

也可以执行单条 SQL 或脚本：

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

CLI 还支持 `--user`、`--password` 和 SSH stdio 模式 `--stdio`。数据库文件会在首次打开时创建；写入完成后持久化，重新打开同一个文件即可恢复数据。

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

**事务/并发/恢复**：`BEGIN [TRANSACTION] [ISOLATION LEVEL ...]` / `COMMIT` / `ROLLBACK` / `SET TRANSACTION ISOLATION LEVEL ...`；缺省自动提交（每条写语句自成事务）。并发采用**表级 S/X 锁 + 严格两阶段封锁**，支持锁升级与可重入，等待图检测死锁（回滚年龄最小者）并带锁超时；两种隔离级别 `serializable`（默认）与 `read_committed`。崩溃恢复基于**页级预写日志**（`<db>.wal`，JSON-Lines）：页头记录 LSN，脏页落盘前先刷日志，重启时按 undo 回滚未提交事务、回收其分配页。

```sql
BEGIN;
INSERT INTO student VALUES (3, 'carol', 90);
ROLLBACK;                     -- carol 不会保留
BEGIN ISOLATION LEVEL read_committed;
INSERT INTO student VALUES (4, 'dave', 88);
COMMIT;
```

配置项：`wal_enabled`（`--no-wal`）、`lock_mode`（`table`/`none`）、`lock_timeout_seconds`、`default_isolation`；`--txn-status` 可在 CLI 查看当前事务状态。

## 测试与基准

```powershell
python -m pytest -q                                     # 312 passed
cd web; npm test                                        # 4 passed
python -m benchmarks.run_benchbox_tpch --iterations 5 --force
```

`pytest` 覆盖公共层、存储、SQL、索引、事务、并发、故障恢复、服务与工作台。SQL 语义与优化规则的对外复现用例维护在 [`examples/test_example.md`](examples/test_example.md)（配套 [`examples/test_example.sql`](examples/test_example.sql)），按文档步骤可复现全部断言。

第二条命令用第三方 BenchBox 0.4.0 跑 TPC-H SF0.01 Q6：数据来自 `TPCH.generate_data()`，SQL 来自 `TPCH.get_query(6, dialect="sqlite")`，YourSQL 只负责加载与执行。结果写入 `benchmarks/reports/tpch_sf001_q6.json`；这是 Q6 子集实验，不等同官方 QphH@Size 成绩。数据集与运行产物落在 `benchmarks/third_party/`、`benchmarks/results/`，不入库。

同一份数据另有一份仓库外的 MiniOB `3259c37` 对照：`tmp/miniob` 未随仓库提供，需在本机重建才能复现。关闭逐行 TRACE 日志后 MiniOB 平均 `0.2164s`，YourSQL 平均 `1.9396s`，约慢 `8.96x`；两者结果 `734493.63` 与 `734493.7281`，差异来自 DECIMAL→FLOAT 映射。详见 [MiniOB 对照报告](benchmarks/reports/tpch_sf001_q6_miniob_compare.json)。

### 多引擎对照（SQLite / DuckDB）

```powershell
python -m benchmarks.compare_tpch_q6 --iterations 5
```

三个引擎装载**同一份** `lineitem.tbl`，跑**同一条** Q6，预热 1 次、静置 5 s 后计时；只计查询耗时，装载单独记录。报告写入 `benchmarks/reports/tpch_sf001_q6_engines.json`：

| 引擎 | 谓词语义 | 装载 | 平均查询 | 中位 | 对 YourSQL 的倍率 |
| --- | --- | --- | --- | --- | --- |
| YourSQL（行存 · 逐行 Python 求值） | double | 36.471 s | 0.2961 s | 0.2957 s | 1× |
| SQLite 文件库 | double | 0.481 s | 0.0086 s | 0.0086 s | 34× |
| SQLite 内存库 | double | 0.419 s | 0.0046 s | 0.0046 s | 64× |
| DuckDB（列存 · 向量化） | double | 0.157 s | 0.0010 s | 0.0009 s | 296× |
| DuckDB（DECIMAL 列，参考） | decimal | 0.151 s | 0.0009 s | 0.0008 s | 329× |

读数时注意四点：

- **同语义可比**：SQLite/YourSQL/DuckDB(DOUBLE) 的 Q6 结果都是 `734493.7281`（脚本会交叉校验，不一致就报错退出）。DuckDB 默认把 `0.06 ± 0.01` 精确折叠为 DECIMAL `0.05/0.07`，会多命中 `l_discount = 0.07` 的 391 行（结果 `1193053.2253`），因此对照时给它加了显式 `CAST(... AS DOUBLE)`，DECIMAL 变体只作参考。
- **口径差异**：上表是裸 `database.execute()` 计时；`benchmarks/reports/tpch_sf001_q6.json` 里的 `2.2167 s` 走 BenchBox 适配器（含结果封装与校验），两个数字都保留。
- **环境波动**：同一份代码在不同时段复测会落在 **0.30–0.57 s**（本机受后台负载/风扇降频影响），表内取的是对照脚本那一次的 0.2961 s；比较倍率只看数量级，不要拿小数点后两位当结论。
- **定位差异**：DuckDB 是列存向量化 OLAP，Q6 正是它的主场；SQLite 与我们同为行存逐行求值，`34×` 才是同类差距。

差距来源已用 cProfile 定位（`SF0.01` 单次 Q6，相对占比）：

| 热点 | 次数 | 说明 |
| --- | --- | --- |
| `_table_context` | 60,175（每行 1 次） | 每行重建一次行上下文（列名 → 值） |
| `_eval_expr` / `isinstance` | 114 万 / 540 万次 | 逐行解释表达式树，类型派发占大头 |
| `_eval_date_function` | 120,350 次 | `DATE('1994-01-01')` 这类常量每行重算 |
| `json.raw_decode` | 60,175 次 | 逐行 JSON 解码（仅约 2% 耗时） |

据此的优化优先级：常量折叠预求值 → 行上下文模板化与按需列裁剪 → WHERE 预过滤 → 表达式树预编译为闭包 → 淘汰路径去 O(容量)。

### 逐行执行路径优化（已完成）

按 cProfile 定位的每行开销逐项处理，均保持语义与错误信息不变（含三值逻辑、NULL 传播与错误行列）：

| 优化 | 做法 | 效果（SF0.01 Q6） |
| --- | --- | --- |
| 行上下文模板化 | 把列名小写、限定键拼接、`__schemas__` 提到查询级模板，行内用 `itemgetter` + `dict(zip(...))` 组装 | `_table_context` 1.328 s → 0.192 s（60,175 次） |
| 按需列裁剪 | 从语句里收集被引用的列名，只把用到的列放进上下文（`SELECT *` 时退回全列） | 16 列 → Q6 只需 4 列 |
| WHERE 预过滤 | 无 JOIN 时先用只读行视图（`_RowView`）过谓词，不通过的行根本不建上下文 | 建上下文次数 60,175 → 800 |
| 常量折叠 | 执行前把 `DATE('1994-01-01')`、`0.06 - 0.01` 这类常量子树预求值为 `Literal`；求值失败则保留原节点 | `_eval_date_function` 120,350 次 → 热点榜消失 |
| 表达式编译为闭包 | `_compile_expr` 把表达式树编成闭包，去掉逐行 `isinstance` 派发；LIKE 正则在编译期建好 | `_eval_expr` 114.5 万次调用，不再是热点 |
| 淘汰路径 O(容量) → O(1) | `BufferPool._evict_one` 改用 `OrderedDict` 维护的淘汰顺序取队首未 pin 页，不再每次构建候选列表 + `min` | 全表扫描下几乎每页都触发淘汰；256 页池实测约 +4%，并消除随容量增长的开销 |

结果（**同口径背靠背**：回退引擎文件前后各测一次，同一探针、同一静置参数）：

| 版本 | 平均 | 中位 | 最快 |
| --- | --- | --- | --- |
| 优化前 | 3.1542 s | 3.1933 s | 3.0074 s |
| 优化后 | 0.5741 s | 0.5598 s | 0.5311 s |

即 **5.5×**（中位 5.7×）；对照脚本那一次跑出的是 0.2961 s，属同一量级、受环境波动影响。`pytest` 118 项全绿（新增 `tests/test_expression_compiler.py`，用 29 个表达式 × 3 种上下文逐例对照编译闭包与解释器）。

两个反直觉发现也一并记录，避免以后重复踩：

- **把比较运算符内联成 `operator.eq/lt/...` 实测无收益**（0.4043 s vs 0.3943 s，在噪声内），已回退，不留无用复杂度。
- **“缓冲池越大越慢”是测量污染**：同进程内依次测 64/256/1024 页曾出现 0.38 / 0.46 / 0.60 s 的递增；换成独立进程逐个复测后，三者都是 **0.36–0.40 s**（无容量效应）。因此对照脚本新增 `--settle-seconds`（默认 3 s）：装载后先静置再计时，否则刚导完数据时同库同配置会测出 0.72 s（OS 回写未结束）对 0.40 s（稳定态）的两倍差距。

仍然存在的开销（实测拆解，单次 Q6 共 0.372 s）：

| 阶段 | 耗时 | 占比 | 说明 |
| --- | --- | --- | --- |
| 读 533 页（8.3 MB） | 0.016 s | 4% | OS 缓存命中；与缓冲池容量无关（64/256/1024 页同速） |
| 槽目录反序列化（533 页） | 0.052 s | 14% | 每页一次 `SlottedPage.from_page` |
| **逐行 JSON 解析（60,175 条）** | **0.180 s** | **48%** | 3.1 µs/条，单条平均 138 B |
| 其余扫描开销（生成器/tuple/迭代） | 0.03 s | 8% | |
| 谓词求值（删译闭包 × 4 谓词） | 0.06 s | 16% | 已优化：原为逐行 `isinstance` 解释 |
| 上下文构建 + 投影 + 聚合 | 0.04 s | 12% | Q6 只对 800 行建上下文 |

对照同一份数据、同一条 Q6：SQLite 文件库 `0.0086 s`（0.14 µs/行）· DuckDB `0.0010 s`（0.017 µs/行）· YourSQL `0.2961 s`（4.9 µs/行）。差距来自三个层面，而不是某个具体 bug：

1. **存储编码**：每行是 JSON 文本，必须 `json.loads` 才能用；SQLite 是紧凑二进制记录（C 循环解析，`COUNT(*)` 全表仅 0.0047 s），DuckDB 是列存 + 压缩（同样数据只占 1.76 MB，而且 Q6 不读的列根本不碰）。
2. **每行都产生 Python 对象**：解析出 list/tuple/str/float，再组装成上下文 dict；SQLite/DuckDB 在 C/C++ 里直接对缓冲区/列向量求值，没有逐行对象分配。这是“Python 逐行引擎 vs C 引擎”的固有差距。
3. **执行模型**：DuckDB 一次处理 2048 个值的向量（可 SIMD），我们一次一个 Python 对象；即使把表达式编成闭包（本轮已做），仍是每行 4–8 次 Python 函数调用。

如果继续沿 Python 逐行架构，各条路的实测上限（已写原型对比，见下表）：

| 方案 | 解码成本 | 对现状 |
| --- | --- | --- |
| 现状 `json.loads` + tuple | 2.64 µs/行 | 1× |
| 二进制（逐列 Python 循环解码） | 4.98 µs/行 | 0.53×（**更慢**） |
| 二进制（批量 `struct.unpack` + 逐列位测试） | 3.63 µs/行 | 0.68×（更慢） |
| **全定长行 + 每行一次 `struct.unpack`（字符串保持 bytes）** | **0.60 µs/行** | **4.4×** |
| 同上但每行还要 decode 被引用列 | 1.55 µs/行 | 1.7× |

结论：**“JSON vs 二进制”不是关键变量，“每行执行多少 Python 字节码”才是**——`json.loads` 是一次 C 调用，任何在 Python 里逐列循环的解码都会比它慢；只有全定长行能把解码压成“每行 1 次 C 调用”。因此换编码的前提是先让 binder/catalog 保留 `VARCHAR(n)/CHAR(n)` 长度（现在丢了长度，benchbox 的 `CHAR(25)` 被映射成无长度 VARCHAR），否则收益会从 4.4× 缩到 1.7×。另一条不用改存储格式的路是**减少扫描行数**（给 Q6 的日期/折扣建索引，命中 9,484 行而非 60,175 行），收益同量级且不动兼容性。

## 服务接口

标准库 HTTP 服务提供 `GET /health`、`GET /metrics` 与 `POST /sql`（JSON body 为 `{"sql": "..."}`）：

```powershell
python -c "from yoursql.engine.runtime.database import Database; from yoursql.engine.services.http import serve_http; serve_http(Database('demo.db'))"
```

SSH 场景执行 `python -m yoursql.cli --database demo.db --stdio`，由 OpenSSH 强制命令转发逐行 SQL。

## Web 数据库工作台

构建后的前端由同一个标准库进程托管，原有 `/health`、`/metrics`、`/sql` 保持不变。

```powershell
Copy-Item .env.example .env
cd web
npm ci
npm run build
cd ..

python -m yoursql.web --database .\data\showcase_v2.db --port 8080
```

浏览器打开 <http://127.0.0.1:8080>，演示库默认管理员为 `admin / admin`。工作台提供：

- SQL 编辑、单条/批量执行、结果分页、CSV/JSON 导出和查询历史；
- Token、AST、逻辑计划、优化计划、物理阶段和执行统计查看；
- 表、用户视图和系统视图浏览，以及权限信息查看；
- 页面地图、HEAP/INDEX 页详情、Buffer Pool 和索引检查；
- 查询性能监控、慢查询记录和受权限保护的诊断数据。

修改 `web/` 源码时，可运行 `npm run dev` 启动 Vite 开发服务器，并在后端追加：

```powershell
python -m yoursql.web --database .\data\showcase_v2.db --allow-origin http://127.0.0.1:5173
```

前端构建产物写入 `yoursql/workbench_static/`，该目录属于本地构建产物，不提交到 Git。

## SQL 与权限能力

当前支持的主要能力：

- DDL/DML：`CREATE/DROP TABLE`、`CREATE/DROP VIEW`、`CREATE/DROP INDEX`、`INSERT`、`UPDATE`、`DELETE`；
- 查询：`SELECT`、`WHERE`、`ORDER BY`、`LIMIT/OFFSET`、`DISTINCT`、聚合、`GROUP BY/HAVING`、`INNER/LEFT/RIGHT/FULL/CROSS JOIN`、`IN/NOT IN` 子查询、`UNION/UNION ALL` 和 `EXPLAIN`；
- 目录查看：`SHOW TABLES/VIEWS/COLUMNS/INDEXES/CREATE`、`DESC/DESCRIBE`；
- 权限管理：用户、角色、`GRANT`、`REVOKE` 和 `SHOW GRANTS`。权限目录使用隐藏系统表保存，并提供 admin-only 的只读系统视图。

边界需要明确：

- View 只保存定义和输出模式，不支持写操作或建索引；
- 已支持 CTE、派生表、`CASE WHEN`、`CAST` 与常见子查询；相关子查询仍可能受逐行执行成本限制；
- `DECIMAL` 使用定点语义并无损落盘，但仍属于教学实现；
- 已提供事务、表级并发控制和 WAL 崩溃恢复；TLS、角色继承、列级权限与行级权限等生产级安全特性仍未实现。

## 演示数据与示例

仓库自带 [`data/showcase_v2.db`](data/showcase_v2.db)，包含 9 张业务表、137,036 行、用户视图、索引和多个演示账号。生成器会重建同样的内容和结构：

```powershell
python -m examples.create_showcase_db --force
```

生成器使用 4096B 页和 128 页 Buffer Pool；密码哈希带随机盐，因此重建后文件 SHA-256 不保证相同。完整演示步骤、账号权限和 SQL 见 [`examples/showcase_demo.md`](examples/showcase_demo.md)。

优化规则、连接策略、子查询复用和预期结果的最小复现用例见 [`examples/test_example.md`](examples/test_example.md)。Buffer Pool 实验见 [`examples/buffer_pool_lab_test.sql`](examples/buffer_pool_lab_test.sql) 和 [`benchmarks/compare_buffer_pool_lab.py`](benchmarks/compare_buffer_pool_lab.py)。

## 配置

复制 [`.env.example`](.env.example) 后按需修改。配置优先级为：命令行参数 > 系统环境变量 > 根目录 `.env` > 程序默认值。

常用配置包括：

- Web：`YOURSQL_DATABASE`、`YOURSQL_HOST`、`YOURSQL_PORT`、`YOURSQL_ALLOWED_ORIGINS`；
- 查询边界：`YOURSQL_QUERY_TIMEOUT_SECONDS`、`YOURSQL_MAX_RESULT_ROWS`、`YOURSQL_MAX_SQL_CHARS`；
- 存储：`YOURSQL_PAGE_SIZE`、`YOURSQL_BUFFER_POOL_SIZE`、`YOURSQL_REPLACEMENT_POLICY`；
- 存储格式：`YOURSQL_PAYLOAD_CODEC=json|manual`。新数据库默认使用 `json`；已有数据库以文件内记录为准，不能直接切换格式；
- 兼容入口：`YOURSQL_LEGACY_ANONYMOUS=true` 可为旧教学客户端开启匿名 `/sql`，工作台默认关闭。

HTTP 接口分为两组：现代工作台接口使用 `/api/*`，包含登录、查询、历史、监控、存储检查和数据库管理；兼容接口保留 `GET /health`、`GET /metrics` 与 `POST /sql`。除登录和登录前数据库接口外，现代接口需要认证；匿名兼容入口需显式开启。

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
.\.venv\Scripts\python.exe -m pytest -q
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
- 后端 `python -m pytest -q`：312 passed（含事务 16、并发 10、WAL 14、聚合流式语义 15、连接策略 18、优化规则 13）。
- 前端 `npm test`：4 passed（块点击语义、常驻右栏布局）；`npm run build` 成功。
- 演示库 `data/showcase_v2.db`：CLI（`SHOW TABLES`、分组聚合、`EXPLAIN` 走 `IndexScan`、`analyst` 权限登录）与工作台 HTTP 链路（登录 → `/api/queries` 异步任务 → 结果分页 → 存储快照）均通过。
- BenchBox TPC-H SF0.01 Q6：PASS，`734493.7281`，平均 `2.2167s`、中位数 `2.2323s`（CPython 3.12.13 / Windows 11），摘要见 `benchmarks/reports/tpch_sf001_q6.json`。

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run_benchbox_tpch --iterations 5 --force
.\.venv\Scripts\python.exe -m benchmarks.compare_tpch_q6 --iterations 5
```

结果写入 `benchmarks/reports/`。当前可编译的 TPC-H 查询子集为 8/22；这些脚本是学习和对照实验，不代表完整 TPC-H 官方成绩。由于 `DECIMAL` 的浮点实现，Q6 与精确十进制引擎的结果可能不同，详见报告中的 `value_notes`。

## 文档

- [系统架构与验收边界](docs/ARCHITECTURE.md)
- [开发清单与未完成项](docs/TODO.md)
- [工作台设计记录](docs/WORKBENCH_DESIGN.md)
- [全功能 Showcase](examples/showcase_demo.md)
- [SQL/优化复现用例](examples/test_example.md)

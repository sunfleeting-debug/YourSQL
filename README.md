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

**演示库**：[`data/showcase_v2.db`](data/showcase_v2.db) 含 9 张表、137,036 行、4096B 页（4915 页）、11 个索引、3 个用户视图与 `analyst`/`support`/`auditor` 账号；`python -m examples.create_showcase_db --force` 可重建。重建的可复现性边界：用户可见内容（9 表行集与行序、11 个索引条目、3 个视图、页数 4915）逐次一致，但**文件 SHA256 不固定**——内部 `_sys_users.password_hash` 用 `secrets.token_bytes(16)` 随机盐 + PBKDF2-HMAC-SHA256（12 万轮），盐每次不同（安全设计）。配套 [`examples/showcase_init.sql`](examples/showcase_init.sql) 与 [`examples/showcase_demo.md`](examples/showcase_demo.md)。

**API**（统一返回 `{ok, request_id, data, error}`）：

- 身份：`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/session`、`GET /api/permissions`
- 元数据：`GET /api/dialect`、`GET /api/databases`、`GET /api/databases/available`、`POST /api/databases/select|create|import`、`GET /api/tables/{name}`
- 登录前：`GET /api/databases/available-before-login`、`POST /api/databases/select-before-login|import-before-login`
- SQL：`POST /api/validate`、`POST /api/queries`、`GET /api/queries/{id}`、`GET /api/queries/{id}/results/{index}`、`POST /api/queries/{id}/cancel`
- 历史：`GET /api/history`
- 存储：`GET /api/storage`、`GET /api/storage/changes`、`GET /api/storage/pages/{page_id}`、`GET /api/storage/cache`、`POST /api/storage/cache/policy`、`GET /api/storage/indexes`、`GET /api/storage/indexes/{name}`

切换与新建库用 `path` 字段（新建还可传 `page_size`、`buffer_pool_size`、`replacement_policy`）；导入用二进制 `.db` 文件体加 `X-YourSQL-File-Name` 文件名头。缓存响应的 `buffer_pool.eviction_order` 是当前策略下的升序淘汰队列，Pin 中的页不进入队列；拥有 `SECURITY` 权限时可在线切换 LRU/FIFO，切换不清空已有缓存帧，从下一次淘汰开始生效。

## 索引与 Q6 实测

索引是真实落盘 B+Tree：叶页存 `(key, RowId)` 并按 key/RowId 排序、带前后叶子链；内部页存分隔键与子页号；`bulk_load` 按页容量分块一次建树；查询侧 `search` / `range_scan` / `range_scan_prefix`。优化器从 `AND` 原子里抽单列约束（`=` / `IN` / `BETWEEN` / 范围比较 / `IS NULL`），按索引列从左往右走：只有**前导列是等值**时才能继续用后面的列；前导列是范围时只做一次 `range_scan_prefix` 就返回。多个索引的候选集取交集，再按 RowId 回表读堆页。

在 TPC-H SF0.01（60,175 行 / 533 页 / 缓冲池 256 页）上现场建索引实测 Q6：

| 索引 | 建索引 | Q6 最快 | 计划 | 候选行 | 库大小 |
| --- | --- | --- | --- | --- | --- |
| 无 | - | **0.3591 s** | SeqScan | 全表 60,175 | 8.42 MB |
| `l_shipdate` | 1.98 s | 0.3472 s | SeqScan | 9,484 | 9.95 MB |
| `l_discount` | 2.02 s | 0.3926 s | IndexScan | 1,728 | 11.00 MB |
| `(l_shipdate, l_discount)` | 2.30 s | 0.4270 s | IndexScan | 1,728 | 12.81 MB |
| `l_quantity`（四索引共存） | 2.09 s | **0.6233 s** | IndexScan | 800 | 13.86 MB |

**结论：这个查询上索引反而更慢。** 原因不是索引写错了，而是代价模型缺一项：

- 顺序扫描只读 **533 页**（8.5 MB）并顺序解码 60,175 行；
- 索引扫描即便候选只剩 **800 行**，也要付：每个被命中索引的一次树遍历（索引页是 JSON 节点，读一页就是一次 `json.loads`）、多个索引候选集的交集，以及 **800 次随机回表**（每次一个 16 KB 堆页）。
- 现有规则只看“候选行数是否 ≤ 表行数 20%”（外加小表特例），没有任何“触碰页数 × 随机访问惩罚”的项，所以在 800/60,175 = 1.3% 时毫不犹豫选了 IndexScan——实测比 SeqScan 慢 1.7×。

实测同时暴露并修掉了三个问题：

| 问题 | 现象 | 修复 |
| --- | --- | --- |
| `bulk_load` 分块 O(行数 × 叶大小) | 每行都复制当前块并调 `_node_fits`（该函数会 `json.dumps` 整个候选节点）；5,000 行建索引 7.97 s，外推 60,175 行约 19 分钟（实测 400 s 未完成） | 改为按“条目编码长度”增量核算 → 5,000 行 **7.97 s → 0.14 s**，60,175 行约 **2 s** |
| `DiskManager.peek` 看不到缓冲写入 | 只读路径用独立句柄，页面被缓冲池淘汰后读回**旧内容**（33 页索引 + 32 帧时会读到空叶页） | `peek` 前先 `flush()`（不改变游标与 I/O 计数）；新增 `tests/test_disk_peek.py` 回归 |
| 候选集看不到常量折叠 | 计划下推的谓词仍含 `DATE('1994-01-01')`，而候选抽取只认字面量 → 字符串列范围索引直接失效 | 抽候选前先 `_fold_constants` |

下一步建议（按收益）：给代价模型加“预计触碰页数 × 随机系数 + 候选行解码成本”与“表页数 × 顺序成本 + 全表解码成本”的对比；候选集按索引+约束缓存，避免每次执行重走索引树；再进一步是**索引覆盖**（把投影列写进索引），那才能让这类查询真正变快。

### 索引优化改进（已完成）

| 项 | 做法 | 实测效果 |
| --- | --- | --- |
| 候选集缓存 | 按 `(索引名, 约束签名)` 缓存单索引候选、按参与索引集合缓存交集；写入与索引 DDL 后整体失效 | 同一 Q6 的候选集计算 **252.9 ms → 0.12 ms**（三索引）；写入后正确失效并重算 |
| 页级代价模型 | 用实测常数替换“候选行 ≤ 20%”规则：顺序页 30 µs、顺序解码 4.2 µs/行、随机取页 94 µs（回表单价按“表页数是否超出缓存”折算） | 正确拒绝 `l_discount` 单索引（10,969 候选，模型 0.68 s vs 顺序 0.27 s，实测慢 3 倍），保留 800 候选的交集路径 |
| 索引覆盖 `IndexOnlyScan` | `CREATE INDEX ... INCLUDE (列)`：定长目录字段 + 叶页平行 `payloads` 数组（旧页无该字段仍可读）；查询引用列全覆盖时直接由叶子条目组装行视图，不回表 | Q6 建 `(l_shipdate) INCLUDE (l_discount, l_quantity, l_extendedprice)` 后 **0.6138 s → 0.1358 s**（约 4.5×），执行算子标记为 `IndexOnlyScan` |

覆盖索引的成本单独定价（`INDEX_ONLY_ENTRY_COST = 4.6 µs/条`，实测叶子链扫 9,504 条约 44 ms）：它不回表，因此不能沿用含随机回表代价的 `should_use_index`——最初直接复用会把它误判为不划算（实测 Q6 仍回退 SeqScan）。

### 多引擎多查询对标（TPC-H SF0.01）

```powershell
python -m benchmarks.compare_tpch_queries --iterations 3 --query-timeout 180
python -m benchmarks.verify_tpch_values   # 逐值对拍：8 条查询结果 vs 同数据的 SQLite 库
```

同一份 `.tbl`、同一批 SQL，三个引擎各自原生装载路径；每个引擎预热 1 次、3 轮取均值（报告：`benchmarks/reports/tpch_sf001_compare.json`）：

| 查询 | 类型 | YourSQL | SQLite | DuckDB | YourSQL 连接策略 |
| --- | --- | --- | --- | --- | --- |
| 装载 8 表 / 87k 行 | — | 2.8 s | 0.9 s | 0.5 s | — |
| Q1 | 单表聚合 | 1136 ms | 49 ms | 4 ms | — |
| Q6 | 单表聚合（区间） | 341 ms | 10 ms | 1 ms | — |
| Q3 | 三表连接 | 531 ms | 27 ms | 6 ms | HashJoin ×2 |
| Q5 | 六表连接 | 644 ms | 110 ms | 6 ms | HashJoin ×5 |
| Q10 | 四表连接 | 441 ms | 14 ms | 15 ms | HashJoin ×3 |
| Q16 | 连接 + 子查询 | 75 ms | 4 ms | 12 ms | HashJoin |
| Q18 | 连接 + 子查询聚合 | 1471 ms | 40 ms | 8 ms | HashJoin ×2 |
| Q19 | 双表连接 + 大 OR 谓词 | **1217 ms** | 6653 ms | 3 ms | HashJoin |

结论分三层：

- **8/8 全部跑通**。上一轮 6 条带 JOIN 的查询全部超时，根因是连接键只从 `ON` 里找，而 TPC-H 用逗号连接（`FROM a, b WHERE a.x = b.y`，被解析成 `CROSS` + 空 `ON`）→ 退化成笛卡尔积。补上“从 `WHERE` 推断连接键”后全部可执行。
- **Q19 首次反超 SQLite**：1217 ms vs 6653 ms（约 5.5×），因为 SQLite 对这条大 `OR` 谓词选不到可用索引，而我们的哈希连接把 `part ⋈ lineitem` 从 1.2 亿次嵌套迭代降到一次建表 + 2,000 次探测。
- **单表仍慢**：Q1 1136 ms、Q6 341 ms，对 SQLite 约 23–35×，瓶颈仍是逐行 JSON 解码 + Python 对象（见下节拆解）。带连接的查询对 SQLite 慢 3–19×，对 DuckDB 差距更大——DuckDB 是列存向量化 OLAP，这类查询正是它的主场。

**结果一致性**：报告里的 `sample_match` 字段逐条标记样本值是否一致；本批 YourSQL 与 SQLite **8/8 一致**，DuckDB 仅 Q6 不同且原因明确：`0.06 ± 0.01` 在 SQLite/YourSQL 里求值为二进制浮点 `0.06999999999999999`，会漏掉 `l_discount = 0.07` 的行，而 DuckDB 精确折叠为 `0.07`。按 TPC-H 的 DECIMAL 语义 **DuckDB 才是正确答案**，根因是我们的 DECIMAL 目前是浮点实现（详见报告 `value_notes` 与下文“尚可优化的方向”）。

**未纳入本批的 14 条查询**：Q2/Q4/Q7/Q8/Q9/Q12/Q13/Q14/Q15/Q17/Q20/Q21/Q22 需要派生表、CTE、`CASE WHEN` 或子查询表达式，这些特性尚未实现，因此不计入对标（不是超时，也不是失败）。

### 流式执行与装载批量化（已完成）


| 项 | 做法 | 实测效果 |
| --- | --- | --- |
| 流式执行 | 扫描与连接不再 `list(contexts)` 全量物化：左表流式、右表物化；无排序/去重/聚合时按 `LIMIT` 提前终止；统计改用已消费行的计数器 | `SELECT * FROM lineitem LIMIT 10` 的 `rows_examined` 从 60,175 降到 **10**（0.0024 s）；连接 + `LIMIT 5` 从 **OOM** 变为 0.15 s（`rows_examined=5`） |
| 连接内单表谓词下推 | 把 `WHERE` 拆成 AND 原子，只引用单表的原子直接下推到该表扫描（原实现带 JOIN 时只在连接后过滤） | 20 客户 × 15,000 订单的连接聚合 **234.68 s → 2.19 s（107×）** |
| 装载批量化 | `TableHeap.append_batch`：记录累积到写满一页才序列化一次；`insert_rows` 按 4096 行分块；目录统计改为每包一次更新 | 60,175 行导入 **51.3 s → 1.74 s（29.5×，1,173 → 34,644 行/秒）**，逐行回读与源数据逐值一致、重开一致 |
| 唯一性校验去 O(n²) | “有主键/唯一列但无索引”的表原来每行都要全表扫描；改为装载期一次性建集合后 O(1) 查询 | 显式库这类表的重建从 >6 分钟降到分钟级以内 |
| 空索引延迟建树 | 目标是空表的整套索引先只登记条目，装载结束后一次 `bulk_load`（避开每行一次叶子重写）；失败时回滚本批堆行，保证堆与索引一致 | 见下节演示库生成耗时 |

**批量写入必须成对回滚**：批量写页后若索引维护失败，会把整批堆行删除并把已插入的索引条目回退；否则会出现“堆里有行、索引里没有”的不可见数据（这个不一致在旧逐行路径上也存在，本轮一并修掉）。

### 连接执行（已完成）

原来只有嵌套循环（左行 × 右行），Q19 的 `part ⋈ lineitem` 是 2,000 × 60,175 ≈ 1.2 亿次 Python 迭代，SF0.01 都要超时。现在按代价选三种策略：

| 策略 | 适用 | 实现要点 |
| --- | --- | --- |
| **哈希连接** | 等值连接且建侧装得下内存 | 右表（建侧）按连接键建 `dict[键] → [(序号, 上下文)]`，左表流式探测；NULL 键两侧都跳过（SQL 里永不匹配）；一对多自然支持；`RIGHT/FULL` 用桶内序号标记未匹配右行 |
| **索引嵌套循环** | 建侧超内存预算且外层很小时 | 左行取值直接作为等值约束交给已有索引候选路径；**不物化右表**（省内存）；仅支持 `INNER/LEFT` |
| **嵌套循环** | 非等值连接（`<`、范围）或前两者都不划算 | 保留原路径 |

代价常数均实测标定：哈希建表 **0.22 µs/行**（60,175 行 13 ms）、探测 **0.05 µs/次**（2,000 次 0.1 ms）、索引等值查找 **1.4 ms/次**、嵌套循环每对候选 **10.4 µs**；哈希建侧内存按上下文 **464 B/行** 估算，超预算（默认 256 MB）即不再选哈希。

顺带三件事：ON 条件被拆成「连接键 + 残余谓词」，只引用单表的 ON 原子下推到对应表扫描；连接策略写入 `stats["joins"]`（`HashJoin` / `IndexNestedLoop` / `NestedLoop`），便于断言与诊断；**逗号连接**（`FROM a, b WHERE a.x = b.y`）会从 `WHERE` 推断等值键（仅 `INNER/CROSS`，外连接的 `WHERE` 不得提升为连接条件），未限定列名按两侧模式列名归属，残余谓词按“引用表是否已加入”分配层级。

WHY 这一步是决定性的：本批 8 条查询里 6 条带连接，而 TPC-H 的 sqlite 方言全部写成逗号连接；不推断 `WHERE` 键就只能跑笛卡尔积（1,500 × 15,000 × 60,175）。

另外两类原来让查询跑不完的场景也一并解决：

- **不相关 `IN` 子查询复用**：`WHERE x IN (SELECT … GROUP BY … HAVING …)` 原来逐行重跑子查询（Q18 是 15,000 次 × 60k 行聚合），现在不相关时只执行一次并物化结果；相关子查询仍走逐行路径，`NOT IN` 的三值逻辑（子查询含 `NULL` 时不返回任何行）保持原样。
- **`OR` 分支里的连接键**：Q19 把连接键写在每个 `OR` 分支里，取“所有分支共同要求的等式”作为哈希建表键（必要条件，不会漏行），`OR` 整体仍作为残余谓词生效。

注意：**索引连接的胜出场景目前很窄**，因为它被我们自己的索引实现拖累——每次等值查找 1.4 ms（索引节点是 JSON，逐层 `Page.from_bytes` + `json.loads`）。把索引节点改成二进制后，索引连接与候选集计算（现 82–272 ms）会同时受益。

### 尚可优化的方向（按收益排序）

| 方向 | 实测依据 | 预期 |
| --- | --- | --- |
| 索引节点二进制化 | 每次等值查找 1.4 ms、候选集计算 82–272 ms 均花在 JSON 节点解码 | 同时改善索引连接与索引候选路径 |
| 全定长二进制行编码 | 原型实测：全定长行解码 0.60 µs/行 vs `json.loads` 2.64 µs/行（4.4×）；但逐列 Python 解码只有 0.53×（更慢） | 扫描段 0.180 s → ~0.04 s；需先让 binder/catalog 保留 `VARCHAR(n)/CHAR(n)` 长度 |
| 位置化行 VM（免 dict 上下文） | 上下文构建 0.113 s（4 列裁剪后） | 再省 0.06–0.11 s |
| 页内列式布局（只读引用列） | 目前解码阶段仍解析全部 16 列 | 只读 4 列时字节量降到 1/4 |
| 连接策略继续调优 | 左侧行数目前按首表统计粗估，尚无直方图 | 更准的哈希/索引选择 |
| 连接键推断的广度 | 目前只做顶层 `AND` 与“所有 `OR` 分支共有的等式”；分支各自不同键、子查询内部的连接键都不推 | 更多 TPC-H 查询能走哈希连接 |
| DECIMAL 定点化 | 目前 DECIMAL 是浮点实现：TPC-H Q6 的 `0.06 ± 0.01` 得到 `0.06999999999999999`，漏掉 `l_discount = 0.07` 的行，与 TPC-H 官方答案不一致 | 与 DuckDB/官方答案对齐（也是正确性问题，不只是性能） |

已排除的方向（负结果，勿重复投入）：比较运算符内联（无收益）、逐列 Python 二进制解码（比 C 的 `json.loads` 慢）、“缓冲池越大越慢”（测量污染，独立进程复测无容量效应）、连接后过滤 WHERE（已改为按表下推，效果 107×）。


## 验收状态

- 后端 `python -m pytest -q`：120 passed。
- 前端 `npm test`：4 passed（块点击语义、常驻右栏布局）；`npm run build` 成功。
- 演示库 `data/showcase_v2.db`：CLI（`SHOW TABLES`、分组聚合、`EXPLAIN` 走 `IndexScan`、`analyst` 权限登录）与工作台 HTTP 链路（登录 → `/api/queries` 异步任务 → 结果分页 → 存储快照）均通过。
- BenchBox TPC-H SF0.01 Q6：PASS，`734493.7281`，平均 `2.2167s`、中位数 `2.2323s`（CPython 3.12.13 / Windows 11），摘要见 `benchmarks/reports/tpch_sf001_q6.json`。

## 设计文档

[系统架构](docs/ARCHITECTURE.md) · [开发清单](docs/TODO.md) · [工作台设计](docs/WORKBENCH_DESIGN.md) · 课程指导书（`docs/` 内 PDF）

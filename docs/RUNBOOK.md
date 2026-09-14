# YourSQL 启动运行指南

> 本文所有命令均在 2026-09-14 于本机（Windows / Git Bash）**实际执行并验证通过**，
> 关键输出附在文末「实测记录」。项目根目录约定为 `F:/MyProject/SQL编译器`。

---

## 0. 前置要求

| 组件 | 版本要求 | 本机实测 |
| --- | --- | --- |
| Python | ≥ 3.11（`pyproject.toml`） | 3.13.12 / 3.13.7 |
| Node.js | ≥ 20.19（Vite 7 要求） | 22.22.2 |
| npm | 随 Node | 10.9.7 |
| uv（可选） | 任意 | 0.12.1 |

运行时代码**零第三方依赖**（只用标准库）；`requirements.txt` 里只有测试与基准工具。

---

## 1. 首次准备

### 1.1 后端环境（二选一）

```bash
cd "F:/MyProject/SQL编译器"

# 方式 A：uv（README 推荐）
uv venv
source .venv/Scripts/activate        # PowerShell: .\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt

# 方式 B：标准 venv
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
```

> 只想跑数据库本身、不跑测试的话，**这一步可以跳过**：`python -m yoursql.cli` 和
> `python -m yoursql.web` 都不需要任何第三方包，用系统 Python 直接跑即可。

### 1.2 前端构建（要 Web 界面才需要）

```bash
cd web
npm install          # 首次约 3 分钟，实测 78 个包
npm run build        # 约 40 秒，产物直接输出到 ../yoursql/workbench_static
cd ..
```

`web/vite.config.ts` 里已配置 `build.outDir: '../yoursql/workbench_static'`，
**构建产物不需要手动拷贝**，路径正好是后端 HTTP 服务读取的位置。

### 1.3 可选：把命令装进 PATH

```bash
pip install -e .
yoursql --help        # CLI 入口
yoursql-web --help    # 工作台入口
```

安装后可从任意目录调用（因为 CLI 默认数据库路径是相对 cwd 的）。
不安装就用 `python -m yoursql.cli` / `python -m yoursql.web`，**必须在项目根目录执行**。

---

## 2. 启动 Web 工作台（验收 / 演示推荐）

```bash
cd "F:/MyProject/SQL编译器"
python -m yoursql.web --database data/showcase_v2.db --port 8080
```

预期输出：

```
YourSQL 工作台 http://127.0.0.1:8080 · showcase_v2.db
```

浏览器打开 <http://127.0.0.1:8080>，初始账号 **admin / admin**。

- 不传 `--database` 时默认 `./data/workbench.db`（不存在会自动新建）。
- 打开已有库时，页大小自动沿用该库自身格式，环境变量只能影响新库。
- 演示库 `data/showcase_v2.db`：9 张表、137,036 行、4915 页、11 个索引、3 个视图，
  另有 `analyst` / `support` / `auditor` 账号（见 `examples/showcase_demo.md`）。
- 重建演示库：`python -m examples.create_showcase_db --force`。
- 单独验活：`curl http://127.0.0.1:8080/health`。

**`.env` 不是必需的**。不创建 `.env` 时走程序默认值 + 命令行参数，功能完整。
需要改默认端口/超时/结果行数上限时再：

```bash
cp .env.example .env    # 按需修改，密码类敏感值别提交
```

配置优先级：**命令行参数 > 系统环境变量 > `.env` > 程序默认值**，变量统一 `YOURSQL_*` 前缀。

---

## 3. 只跑 CLI（最快验证数据库本身）

命令统一写成**单行**，bash 与 PowerShell 都能直接粘贴执行（多行语句靠 SQL 内部的分号分隔，
不依赖 shell 的续行符）。

```bash
cd "F:/MyProject/SQL编译器"

# 单条 SQL / 脚本（整段用双引号包住，内部换行也可以）
python -m yoursql.cli --database demo.db --sql "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT); INSERT INTO student VALUES (1,'Alice',20),(2,'Bob',22); SELECT id, name FROM student WHERE age > 18;"

# 执行 SQL 文件
python -m yoursql.cli --database demo.db --file examples/test_example.sql

# JSON 输出（便于脚本消费）
python -m yoursql.cli --database demo.db --sql "SELECT * FROM student;" --json

# 交互模式（每行一条语句，quit / exit / \q 退出）
python -m yoursql.cli --database demo.db

# SSH 强制命令用的 stdio 协议：每行 SQL → 每行 JSON
printf "SELECT 1;\n" | python -m yoursql.cli --database demo.db --stdio
```

- **别用 `\` 折行**：`\` 是 bash 的续行符，PowerShell 不认（它用反引号 `` ` ``）。
  在 PowerShell 里写 `... demo.db \` + 换行，`\` 会被当成一个普通参数传进去，
  报 `cli.py: error: unrecognized arguments: \`；紧接着的 `--sql "..."`
  会被 PowerShell 当成新命令，`--sql` 被解析成自减运算符，
  报 `一元运算符 '--' 后缺少表达式`。要在 PowerShell 里折行请用它自己的反引号：

  ```powershell
  python -m yoursql.cli --database demo.db `
    --sql "SELECT 1;"
  ```

- 默认登录凭据 `admin / admin`，可用 `--user` / `--password` 或
  `YOURSQL_CLI_USER` / `YOURSQL_CLI_PASSWORD` 覆盖。
- 交互模式**只按行解析**，多行语句请用 `--sql` / `--file`。
- `demo.db` 是演示库，只预置了 `t` / `departments` / `employees`。
  想跑 `student` 那组示例，先执行上面的 `CREATE TABLE student ...`，
  或者换一个空的数据库文件名（例如 `--database /tmp/try.db`）。
- 出错时按约定打印 `[PARSER_ERROR] at line 1, column 14: 表名不合法`，退出码 1。

---

## 4. 前端开发模式（改前端源码时用）

开两个终端：

```bash
# 终端 1：后端（保持默认 8080，Vite 的 proxy 指向它）
python -m yoursql.web --database data/showcase_v2.db --port 8080

# 终端 2：Vite dev server（5173，改代码即时热更新）
cd web && npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。

`vite.config.ts` 已把 `/api` 与 `/health` 代理到 `http://127.0.0.1:8080`，
所以**开发模式下不需要额外配 CORS**（实测 5173 → 8080 代理正常）。
`.env` 白名单里默认也含 5173/5174，属于双保险。

改完前端记得重新 `npm run build`，否则 `python -m yoursql.web` 托管的仍是旧产物。

---

## 5. 测试与基准

```bash
# 后端：145 个用例，约 53 秒
python -m pytest -q

# 前端：4 个文件 9 个用例
cd web && npm test

# 前端格式校验（提交前必须通过）
cd web && npm run format:check     # 自动修复用 npm run format

# TPC-H 基准与跨引擎对拍
python benchmarks/run_tpch_queries.py
python benchmarks/compare_tpch_queries.py    # 需要按需安装 duckdb
```

---

## 6. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 打开首页返回 `404 {"error":"NOT_FOUND","message":"页面不存在；请先在 web 目录运行 npm run build"}` | 前端产物缺失。执行 `cd web && npm install && npm run build`。**不需要重启后端**，静态文件按请求实时读取，构建完刷新即可 |
| `ModuleNotFoundError: No module named 'yoursql'` | cwd 不在项目根目录。`cd` 到项目根，或 `pip install -e .` |
| `npm run dev` 报端口占用 | `strictPort: true`，5173 被占就必须先释放，不会自动换端口 |
| `Address already in use`（后端） | 换 `--port`，或结束占用进程 |
| 前端能打开但接口全 401 | 未登录，正常行为；在登录页用 admin/admin 登录 |
| 登录后切库被拒绝 | 切库/新建需要 `SECURITY` 权限，且执行中的任务会阻塞切换 |
| 存储检查页看不到原始字节 | 内部权限页只对 `admin` 显示明文，其他会话显示 `MASKED` |
| TPC-H 相关脚本报缺 duckdb | `pip install duckdb` |

---

## 7. 最短路径（复制即用）

```bash
cd "F:/MyProject/SQL编译器"
cd web && npm install && npm run build && cd ..      # 只需首次
python -m yoursql.web --database data/showcase_v2.db --port 8080
```

---

## 附：实测记录（2026-09-14）

| 项 | 命令 | 结果 |
| --- | --- | --- |
| CLI 建表插入查询 | `python -m yoursql.cli --sql "...;...;SELECT..."` | 表格化输出 2 行，退出码 0 |
| CLI 交互模式 | `printf "SELECT COUNT(*) FROM student;\nquit\n" \| ... cli` | `yoursql>` 提示符，返回 count=2 |
| CLI JSON | `--json` | 单行 JSON，含 `stats.operator=SeqScan` |
| CLI 脚本文件 | `--file demo.sql` | `CREATE TABLE t2` / `INSERT 1` / 表格结果 |
| CLI 错误定位 | `--sql "SELECT * FROM"` | `[PARSER_ERROR] at line 1, column 14: 表名不合法`，退出码 1 |
| stdio 协议 | `printf "..." \| ... cli --stdio` | 每行一条 JSON，错误也走 JSON |
| 兼容 HTTP | `serve_http(db, port=8899)` | `/health`、`/metrics`、`POST /sql` 全部正常 |
| 工作台（无产物） | `python -m yoursql.web --port 8898` | 首页 404 + 明确提示；`/health` 200；登录 admin/admin 成功返回 `permissions:["*"]` |
| 前端安装 | `cd web && npm install` | 78 包，3m15s，2 moderate 漏洞（仅 devDependencies 链路，无运行影响） |
| 前端构建 | `npm run build` | 1625 模块，20.12s，输出 `workbench_static/index.html` + 801KB JS + 141KB CSS |
| 构建后托管 | `curl :8080/` | 200 text/html 532B；`/assets/index-*.js` 200，801108B |
| Vite 代理 | `curl :5173/api/dialect` | 透传到 8080，返回 401 UNAUTHENTICATED（代理链路正常） |
| 后端测试 | `python -m pytest -q` | **145 passed in 53.14s** |
| 前端测试 | `npm test` | **9 passed**（4 个文件） |
| 格式校验 | `npm run format:check` | All matched files use Prettier code style |
| 可编辑安装 | `pip install -e .` | `yoursql --help` / `yoursql-web --help` 均可用 |

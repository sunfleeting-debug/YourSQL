<div align="center">
  <img src=".github/assets/icon.png" width="108" alt="YourSQL" />
  <h1>YourSQL</h1>
  <p><b>A persistent mini database written from scratch in pure Python</b><br /><sub>SQL frontend → logical plan → execution operators → paged storage, end to end</sub></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB" alt="Python 3.11+">
    <img src="https://img.shields.io/badge/core%20deps-stdlib%20only-0F6E56" alt="Core deps: stdlib only">
    <img src="https://img.shields.io/badge/storage-4KB%20pages%20%C2%B7%20buffer%20pool-1D9E75" alt="Storage: 4KB pages + buffer pool">
    <img src="https://img.shields.io/badge/index-B%2BTree-0F6E56" alt="Index: B+Tree">
    <img src="https://img.shields.io/badge/workbench-React%20%2B%20Vite-61DAFB" alt="Workbench: React + Vite">
  </p>
  <p>
    <a href="README.md">简体中文</a> ·
    <b>English</b>
  </p>
</div>

A persistent mini database built for studying database internals and for course projects. It implements the SQL frontend, paged storage, buffer pool, B+Tree indexes, the execution engine and RBAC in Python, plus a CLI, HTTP/SSH access and a web workbench.

YourSQL is built for learning, experiments and demos — **it is not a production database**.

## At a glance

| | |
| --- | --- |
| **Shape** | Single-process persistent database · pure Python |
| **Runtime deps** | The core path uses **only the standard library** (`dependencies = []`); test/benchmark extras live in [`requirements.txt`](requirements.txt) |
| **Storage** | 4096 B pages · slotted page format · buffer pool (LRU / 2Q) · page-level WAL |
| **Index** | B+Tree |
| **Concurrency** | Table-level locks + strict two-phase locking · lock escalation · deadlock detection and timeout |
| **Transactions** | `BEGIN` / `COMMIT` / `ROLLBACK` · `serializable` (default) / `read_committed` |
| **Security** | Users, roles, `GRANT` / `REVOKE`; the privilege catalog lives in hidden system tables |
| **Interfaces** | Interactive CLI · single SQL / script · Python API · stdlib HTTP · SSH stdio · web workbench |
| **Demo database** | `data/showcase_v3.db` — 9 tables, 137,036 rows |

---

## ✨ Features

### SQL and privileges

- **DDL/DML**: `CREATE/DROP TABLE`, read-only `CREATE/DROP VIEW ... AS SELECT`, `CREATE/DROP INDEX`, `INSERT`, `UPDATE`, `DELETE`;
- **Queries**: `SELECT`, `WHERE`, `ORDER BY`, `LIMIT/OFFSET`, `DISTINCT`, aggregates, `GROUP BY/HAVING`; supports `INNER/LEFT/RIGHT/FULL/CROSS JOIN`, CTEs, derived tables, `CASE WHEN`, `CAST`, `IN/NOT IN` subqueries, `UNION/UNION ALL` and `EXPLAIN`;
- **Catalog inspection**: `SHOW TABLES/VIEWS/COLUMNS/INDEXES/CREATE`, `DESC/DESCRIBE`;
- **Privileges**: users, roles, `GRANT`, `REVOKE`, `SHOW GRANTS`. The privilege catalog is stored in hidden system tables and exposed through admin-only read-only system views.

### Transactions and concurrency

Transactions use `BEGIN`, `COMMIT`, `ROLLBACK` and `SET TRANSACTION ISOLATION LEVEL`; auto-commit is the default, with `serializable` (default) and `read_committed` supported. Concurrency control uses **table-level locks and strict two-phase locking**, with lock escalation, deadlock detection and timeouts. A **page-level WAL** provides rollback of uncommitted transactions and crash recovery.

### Deliberate boundaries

Views store only a definition and an output schema — they allocate no data pages; accessing one re-executes its query, and views are read-only and cannot be indexed. `DECIMAL` has fixed-point semantics and round-trips losslessly, but the whole thing remains a teaching implementation. Production-grade security features — TLS, role inheritance, column-level and row-level privileges — are **not implemented**.

---

## 🚀 Getting started

Requirements: Python 3.11 or newer. The core runtime depends only on the Python standard library; test and benchmark dependencies are in [`requirements.txt`](requirements.txt).

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

Start the interactive CLI:

```powershell
python -m yoursql.cli --database .\data\demo.db
```

Run a single statement or a script:

```powershell
python -m yoursql.cli `
  --database .\data\demo.db `
  --sql "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT); INSERT INTO student VALUES (1, 'Alice', 20); SELECT * FROM student;"

python -m yoursql.cli `
  --database .\data\demo.db `
  --file .\examples\test_example.sql `
  --json
```

Python API:

```python
from yoursql.engine.runtime.database import Database

with Database("data/demo.db") as database:
    database.execute("CREATE TABLE greeting(id INT, text VARCHAR);")
    database.execute("INSERT INTO greeting VALUES (1, 'hello');")
    result = database.execute("SELECT * FROM greeting;")
    print(result.rows)
```

The CLI also supports `--user`, `--password` and an SSH stdio mode (`--stdio`). A database file is created on first open, persisted after writes, and reopened from the same path to recover its data.

---

## 🖥 Web workbench

The workbench is served by the same stdlib HTTP process, listening on `http://127.0.0.1:8080` by default. Build the frontend once:

```powershell
Copy-Item .env.example .env
cd web
npm ci
npm run build
cd ..

python -m yoursql.web --database .\data\showcase_v3.db --port 8080
```

Open <http://127.0.0.1:8080>; the demo database's default admin is `admin / admin`. The workbench offers SQL editing and execution, paginated and exportable results, query history, AST / plan / execution-stat inspection, privilege inspection, page and index inspection, and privilege-gated performance diagnostics.

When changing `web/` sources, run `npm run dev` for the Vite dev server and start the backend with:

```powershell
python -m yoursql.web `
  --database .\data\showcase_v3.db `
  --allow-origin http://127.0.0.1:5173
```

Frontend build output goes to `yoursql/workbench_static/`, which is a local build artifact and is not committed.

The stdlib server also exposes `GET /health`, `GET /metrics` and `POST /sql` (JSON body `{"sql": "..."}`); the modern workbench API lives under `/api/*` and requires authentication.

---

## 📦 Demo data and examples

The repo ships [`data/showcase_v3.db`](data/showcase_v3.db) with 9 business tables, 137,036 rows, user views, indexes and several demo accounts. The generator rebuilds the same content and structure:

```powershell
python -m examples.create_showcase_db --force
```

The generator uses 4096 B pages and a 128-page buffer pool; password hashes carry random salts, so a rebuilt file is not guaranteed to have the same SHA-256. Full demo steps, account privileges and SQL are in [`examples/showcase_demo.md`](examples/showcase_demo.md).

Minimal reproducible cases for SQL semantics, optimization rules, join strategies, subquery reuse and expected results are in [`examples/test_example.md`](examples/test_example.md). Buffer pool experiments live in [`examples/buffer_pool_lab_test.sql`](examples/buffer_pool_lab_test.sql) and [`benchmarks/compare_buffer_pool_lab.py`](benchmarks/compare_buffer_pool_lab.py). [`examples/buffer_pool_2q_effect.sql`](examples/buffer_pool_2q_effect.sql) is specifically for comparing 2Q hot-page retention against page-type protection inside the SQL workbench, defaults to the 512-row `data/buffer_pool_lab_demo.db`, and requires resetting stats between its two sections; the older `buffer_pool_lab_large.db` remains useful for stress testing but is unsuitable for observing policy differences.

---

## ⚙️ Configuration

Copy [`.env.example`](.env.example) and adjust as needed. Precedence: **CLI arguments > environment variables > root `.env` > program defaults**.

Common settings:

- **Web**: `YOURSQL_DATABASE`, `YOURSQL_HOST`, `YOURSQL_PORT`, `YOURSQL_ALLOWED_ORIGINS`;
- **Query limits**: `YOURSQL_QUERY_TIMEOUT_SECONDS`, `YOURSQL_MAX_RESULT_ROWS`, `YOURSQL_MAX_SQL_CHARS`;
- **Storage**: `YOURSQL_PAGE_SIZE`, `YOURSQL_BUFFER_POOL_SIZE`, `YOURSQL_REPLACEMENT_POLICY`;
- **Storage format**: `YOURSQL_PAYLOAD_CODEC=json|manual`. New databases default to `json`; existing ones follow the record inside the file;
- **Compatibility**: `YOURSQL_LEGACY_ANONYMOUS=true` re-enables anonymous `/sql` for legacy teaching clients (off by default for the workbench).

Transaction settings include `wal_enabled`, `lock_mode`, `lock_timeout_seconds` and `default_isolation`; the CLI exposes `--no-wal` and `--txn-status` to control or inspect transaction state.

---

## 🧱 Architecture

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

| Directory | Responsibility |
| --- | --- |
| `yoursql/sql` | Lexer, parser, AST, binding and the compile entry point |
| `yoursql/planner` | Logical/physical plans, optimization rules, statistics, cost estimation |
| `yoursql/execution` | Expression evaluation, query orchestration, Volcano operators |
| `yoursql/storage` | Page, heap, disk, buffer pool and B+Tree |
| `yoursql/engine` | Catalog, runtime coordination, security and the HTTP/SSH/workbench services |
| `web` | React/Vite database workbench |
| `tests` | Backend unit and integration tests |

---

## ✅ Tests and benchmarks

Backend tests:

```powershell
python -m pytest -q
```

Frontend checks:

```powershell
cd web
npm test
npm run format:check
npm run build
cd ..
```

Optional TPC-H SF0.01 experiments:

```powershell
python -m benchmarks.run_benchbox_tpch --iterations 5 --force
python -m benchmarks.compare_tpch_q6 --iterations 5
```

Results are written to `benchmarks/reports/`. These scripts are for learning and engine comparison and **are not official full TPC-H results**; semantics, environment and timing methodology differ per engine — read the reports.

---

## 📚 Documentation

- [Architecture and acceptance boundaries](docs/ARCHITECTURE.md)
- [TODO and unfinished items](docs/TODO.md)
- [Workbench design notes](docs/WORKBENCH_DESIGN.md)
- [Full showcase](examples/showcase_demo.md)
- [SQL/optimizer reproducible cases](examples/test_example.md)

## 📄 License

This repository **does not currently ship an open-source license**. If you want to republish, quote or reuse any of the implementation, please contact the author first.

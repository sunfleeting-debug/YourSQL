# 基础查询优化复现用例

本用例覆盖：常量折叠、恒真/恒假条件消除、`AND` / `OR` 规则化简、常量表达式索引匹配、显式谓词下推、逗号连接的连接键推断与不相关子查询复用、限行下推（`limit_pushdown`）、连接顺序重排（`join_reordering`）。

## 运行

本用例的 SQL 脚本与本文件同目录：[`test_example.sql`](./test_example.sql)。在项目根目录执行：

```powershell
Remove-Item -LiteralPath .\tmp\optimizer-example.db -Force -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db --file .\examples\test_example.sql
```

也可以使用 `uv run --no-sync yoursql` 替换 `.venv\Scripts\python.exe -m yoursql`（`--no-sync` 避免 uv 重新同步环境时卸掉 pytest 等开发依赖）。

## 预期结果

- `SELECT 1 + 2` 返回 `3`，优化计划中的投影表达式是 `Literal(3)`。
- `TRUE AND active = TRUE OR FALSE` 返回员工 `1、3`。
- `WHERE FALSE` 返回空结果，优化计划包含 `EmptyScan`。
- `id = 1 + 2` 返回员工 `3`，执行统计中的访问算子为 `IndexScan`。
- 最后的 `EXPLAIN` 中，`employees` 和 `departments` 两侧各有一个下推的 `Filter`；`departments.id` 一侧使用 `IndexScan`，连接条件 `e.department_id = d.id` 保留在 `Join` 上方。
- 逗号连接（`FROM employees AS e, departments AS d WHERE e.department_id = d.id`）返回与显式 `JOIN … ON` 相同的 3 行（`1/Engineering`、`2/Engineering`、`3/Sales`）。执行统计里连接算子是 `HashJoin`（`ExecutionResult.stats["joins"]`）：优化器从 `WHERE` 推断出 `department_id = id`，否则 `CROSS` 连接只会产生笛卡尔积。
- 把连接键写在 `OR` 分支里（`(e.department_id = d.id AND d.id = 1) OR (… AND d.id = 2)`）同样走 `HashJoin` 并返回 3 行；`OR` 整体作为残余谓词在连接后过滤。
- 不相关 `IN` 子查询（`department_id IN (SELECT id FROM departments)`）返回 `1、2、3`，且子查询只执行一次；`NOT IN` 遇到子查询结果含 `NULL` 时不返回任何行（三值逻辑，详见 `tests/test_join_strategies.py`）。

上述访问路径（`HashJoin` / `IndexNestedLoop` / `NestedLoop`）与边界语义的自动化断言位于 [`tests/test_join_strategies.py`](../tests/test_join_strategies.py)：

自动化断言位于 [`tests/test_optimizer_rules.py`](../tests/test_optimizer_rules.py)，可运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_optimizer_rules.py tests/test_join_strategies.py -q
```

## 具名优化规则

规则清单本身是静态信息，不需要数据库：

```powershell
.venv\Scripts\python.exe -m yoursql.cli --rules
```

预期输出 7 条规则（`constant_folding` / `boolean_simplification` / `predicate_elimination` / `predicate_pushdown` / `index_selection` / `join_reordering` / `limit_pushdown`），每条带执行阶段与说明，开头为 `优化规则 7/7 条启用：`。

### 开关一条规则，观察计划变化

```powershell
# 开：WHERE id = 1 命中 idx_employees_id → IndexScan
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees WHERE id = 1;" --plan text

# 关：同一条语句退回 SeqScan
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees WHERE id = 1;" --plan text --disable-rule index_selection
```

预期（实际输出）：

```text
Project [..., rules=['index_selection', 'predicate_pushdown']]
  Filter [predicate=… id = 1 …, pushed=True]
    IndexScan [table='employees', index_column='id']
```

关掉 `index_selection` 后，末行变为 `SeqScan [table='employees', pushed_predicate=…]`，根节点的 `rules` 只剩 `['predicate_pushdown']`。

`--plan` 只编译不执行，所以不会写库；取值 `text` / `mermaid` / `dot` / `json`。命中规则同时会写进 `plan.properties["rules"]`，EXPLAIN 的 JSON（`to_dict()`）里也能看到。

### limit 下推（`limit_pushdown`）

排序 + 限行时，`Sort` 上会被标注 `top_n`，运行时只用有界堆保留前 `k` 行，不再全量排序：

```powershell
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees ORDER BY id DESC LIMIT 2;" --plan text
```

预期（实际输出；结果为 `3, 2`）：

```text
Limit [limit=2, offset=0, rules=['limit_pushdown']]
  Sort [order_by=[… id …, descending=True, nulls_first=None], top_n=2]
    Project [items=[… id …], distinct=False]
      SeqScan [table='employees', alias=None]
```

关掉规则后 `top_n` 消失，`Sort` 退回全量排序（其余计划形状不变）：

```powershell
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees ORDER BY id DESC LIMIT 2;" --plan text --disable-rule limit_pushdown
```

规模上的差别在 `lineitem`（60,175 行）上才明显：`ORDER BY l_extendedprice LIMIT 10` 由 **627.6 ms / 峰值 55.5 MB** 降到 **475.0 ms / 1.16 MB**（内存降 48 倍），结果逐值一致。覆盖 `NULLS FIRST/LAST`、多键、`DESC`、`OFFSET` 的组合断言见 [`tests/test_streaming_and_batch_load.py`](../tests/test_streaming_and_batch_load.py)。

### 连接顺序重排（`join_reordering`）

对全 INNER/CROSS 的等值连接，按表行数贪心重排：先取最小的表，再逐张接入"与已连接集合有等值键"的表。

```powershell
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT a.v, c.w FROM jr_a AS a, jr_c AS c, jr_b AS b WHERE a.k = b.a_k AND b.k = c.a_k;" --plan text
```

书写顺序是 `jr_a, jr_c, jr_b`（`jr_a` 与 `jr_c` 之间没有直接等值键，按书写顺序首层会退化成 `3 × 2` 的笛卡尔积）。优化后的执行顺序变为 `(jr_c ⋈ jr_b) ⋈ jr_a`（`jr_c` 2 行最小，`jr_b` 与它有等值键 `b.k = c.a_k`）：

```text
Project [… rules=['join_reordering', 'predicate_pushdown']]
  Filter [predicate=… b.k = c.a_k AND a.k = b.a_k …]
    Join [join_type='CROSS', on=None]
      Join [join_type='CROSS', on=None]
        SeqScan [table='jr_c', alias='c']
        SeqScan [table='jr_b', alias='b']
      SeqScan [table='jr_a', alias='a']
```

关掉规则后计划保持书写顺序（`jr_a` 与 `jr_c` 先连）：

```powershell
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT a.v, c.w FROM jr_a AS a, jr_c AS c, jr_b AS b WHERE a.k = b.a_k AND b.k = c.a_k;" `
    --plan text --disable-rule join_reordering
```

预期（实际输出）：

```text
Project […]
  Filter [predicate=… a.k = b.a_k AND b.k = c.a_k …]
    Join [join_type='CROSS', on=None]
      Join [join_type='CROSS', on=None]
        SeqScan [table='jr_a', alias='a']
        SeqScan [table='jr_c', alias='c']
      SeqScan [table='jr_b', alias='b']
```

两种顺序的结果都是 `(x, p)`、`(x, q)`。真正拉开差距的是三张大表：`lineitem ⋈ orders ⋈ customer` 的 6 种 `FROM` 写法里，重排关闭时有 **2 种 >60 s 超时**，开启后 **6 种全部在 1.18–1.65 s 完成**且结果指纹一致。

> **注意** `EXPLAIN` 也能看到重排：`EXPLAIN SELECT ...` 的计划会穿透 `EXPLAIN` 节点取到内层 SELECT，因此脚本里的 `EXPLAIN` 与实际执行的连接顺序一致。

### 计划可视化

```powershell
# 终端里直接出 Mermaid 图
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees WHERE id = 1;" --plan mermaid

# 落成可浏览的 HTML（Mermaid 从 CDN 加载，示例产物见 docs/plan_demo.html）
.venv\Scripts\python.exe -m scripts.plan_visualize --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees WHERE id = 1;" --format html --out .\tmp\plan.html

# Graphviz DOT（可用 dot -Tsvg 渲染）
.venv\Scripts\python.exe -m scripts.plan_visualize --database .\tmp\optimizer-example.db `
    --sql "SELECT id FROM employees WHERE id = 1;" --format dot
```

预期 Mermaid 输出：

```text
flowchart TD
    n0["Project<br/>items=id<br/>distinct=FALSE<br/>rules=index_selection, predicate_pushdown"]
    n1["Filter<br/>predicate=id = 1<br/>pushed=TRUE"]
    n2["IndexScan<br/>table=employees<br/>pushed_predicate=id = 1<br/>index_column=id"]
    n0 --> n1
    n1 --> n2
```

DOT 输出以 `digraph YourSQLPlan {` 开头、`}` 结尾，同样的标签用 `\n` 换行。标签里的 `<`、`>`、`&`、`"` 会被转义（`&lt;` / `&gt;` / `&amp;` / `#quot;`），否则会被 Mermaid 当成语法或截断标签。

### 规则框架的自动化断言

```powershell
.venv\Scripts\python.exe -m pytest tests/test_optimizer_rules.py tests/test_plan_visualization.py -q
```

覆盖：规则清单可枚举、未知规则名报错、命中规则写进计划、关规则后计划真的变了、规则开关不跨实例泄漏、计划缓存命中后仍能读回命中规则，以及 Mermaid/DOT 的图结构与实体转义。

## 脚本错误恢复（一次报全）

```powershell
.venv\Scripts\python.exe -m yoursql.cli --sql "SELECT 1; SELCT 2; SELECT @ FROM t;" --check
```

预期输出（实际输出，3 条诊断，退出码 1）：

```text
发现 3 处错误（已成功解析 1 条语句）：
  1. [PARSER_ERROR] at line 1, column 11: 不支持的语句起始符号（期望 CREATE/INSERT/SELECT/UPDATE/DELETE）
  2. [LEXER_ERROR] at line 1, column 27: 非法字符 '@'
  3. [PARSER_ERROR] at line 1, column 29: 需要表达式（期望 标识符/字面量/'('）
```

`SELECT 1` 本身是合法语句，因此被保留（"已成功解析 1 条语句"）；三条诊断各带行列位置，`--check` 只做词法与语法检查，不连数据库、不建表。合法脚本退出码为 0。

对应断言在 [`tests/test_error_recovery.py`](../tests/test_error_recovery.py)。

## 只数行数的扫描（免解码快路径）

`COUNT(*)` 只需要行数，不需要任何列值。这类查询现在按页槽目录统计活槽，既不切出记录也不做
JSON 解码，`SELECT 常量 FROM t` 同理。上下文裁剪也随之真正收敛到空集，于是
`SELECT COUNT(*) FROM t WHERE 索引列 < ...` 不再被"需要全部列"挡住覆盖索引直读，访问路径
从 `IndexScan` 变成 `IndexOnlyScan`。

耗时基线（口径：每个用例独立进程 × 重复取最小值）：

```powershell
.venv\Scripts\python.exe -m benchmarks.bench_scan_paths
```

预期输出（TPC-H SF0.01 的 `lineitem`，60,175 行；实测一次）：

```text
COUNT(*) 全表   :     57.1 ms   operator=SeqScan  rows_examined=60175
COUNT(列)      :    347.4 ms   operator=SeqScan  rows_examined=60175
SUM(列)        :    344.3 ms   operator=SeqScan  rows_examined=60175
纯投影 单列     :    367.2 ms   operator=SeqScan  rows_examined=60175
纯投影 常量     :     64.2 ms   operator=SeqScan  rows_examined=60175
```

同样的隔离口径下，改动前 `COUNT(*)` 为 **571 ms**、`SELECT 1` 为 **330 ms**，即约 **8.7× / 5.0×**；
需要列值的三条基本不动（347/344/367 ms）——它们撞的是 60,175 行 JSON 逐行解码这个下限
（2.00 µs/行，其中 `$decimal` 还原器占 1.06 µs），不是上下文构造。

> 口径很重要：同一个进程里连着测几条语句会被堆状态与 GC 干扰，同一配置实测能差 30%
> （Q6 曾因此被误判成"变慢 30%"）。这张表必须用 `bench_scan_paths.py` 的隔离口径复现。

自动化断言：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_scan_fast_path.py -q
```

覆盖：与逐行扫描结果一致、删除后按活槽计数、带 WHERE 时不能走快路径、`SELECT *` 仍要展开全部
列、交叉连接 / `GROUP BY` / `HAVING` / 空表，以及存储层 `TableHeap.count()` 与 `scan()` 永远同口径。

## 事务、并发与预写日志

脚本末尾三组语句即可复现最基本的事务语义（[`test_example.sql`](./test_example.sql)）：

```sql
CREATE TABLE IF NOT EXISTS txn_demo (id INT PRIMARY KEY, note VARCHAR(20));

BEGIN;                                        -- BEGIN / BEGIN WORK / BEGIN TRANSACTION 等价
INSERT INTO txn_demo VALUES (1, 'rolled-back');
ROLLBACK;                                     -- 上面这一行必须消失

BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;
INSERT INTO txn_demo VALUES (2, 'committed');
COMMIT;

SELECT id, note FROM txn_demo ORDER BY id;    -- 只应看到 (2, 'committed')
```

预期输出：

```text
BEGIN serializable (txn 1)
INSERT 1
ROLLBACK 1
BEGIN read_committed (txn 2)
INSERT 1
COMMIT 2
+----+-----------+
| id | note      |
+----+-----------+
| 2  | committed |
+----+-----------+
```

### 事务语义要点

- **语法**：`BEGIN | BEGIN WORK | BEGIN TRANSACTION [ISOLATION LEVEL ...]`、`START TRANSACTION`、`COMMIT [WORK]`、`ROLLBACK [WORK]`、`SET TRANSACTION ISOLATION LEVEL {SERIALIZABLE | READ COMMITTED}`。
- **自动提交**：不写 `BEGIN` 时每条语句各自成事务，成功即提交、失败即回滚。所以 `INSERT INTO t VALUES (2,'b'), (1,'dup'), (3,'c')` 撞主键时，前半批也不会留下。
- **原子性靠页级前像**：事务第一次改动某页时留存"修改前的整页内容"，回滚按相反顺序写回。堆表页、索引页、目录页一视同仁——`UPDATE` 走的索引条目同样被撤销。
- **失败事务状态**：显式事务里语句报错后事务进入 `failed`，继续持锁；后续语句与 `COMMIT` 一律被拒，只能 `ROLLBACK`（与 PostgreSQL 一致），"半条语句"不会泄漏给别的语句。
- **DDL 也能回滚**：`CREATE TABLE` / `CREATE INDEX` 在事务内执行后 `ROLLBACK`，目录快照会把它撤掉，索引连页一起释放。
- **`DROP TABLE` 在显式事务内被明确拒绝**：数据页会被立即归还空闲链表、内容随即被覆盖，页级前像救不回来，因此不给出"能回滚"的错觉。

### 并发：封锁与死锁

并发语义用 Python 多线程在同一个 `Database` 实例上演示（事务状态绑定在线程上）。下面这段可以证明"写者持 X 锁时，另一个写者必须等"：

```python
import threading
from yoursql.engine.runtime.database import Database

with Database("tmp/lock.db") as db:
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    a_locked, release_a = threading.Event(), threading.Event()

    def writer_a():
        db.execute("BEGIN")
        db.execute("UPDATE t SET v = 'A' WHERE id = 1")   # 拿到 t 的 X 锁
        a_locked.set()
        release_a.wait(10)
        db.execute("COMMIT")

    def writer_b():
        a_locked.wait(10)
        db.execute("BEGIN")
        db.execute("UPDATE t SET v = 'B' WHERE id = 1")   # 阻塞，直到 A 提交
        db.execute("COMMIT")

    ta = threading.Thread(target=writer_a)
    tb = threading.Thread(target=writer_b)
    ta.start(); tb.start()
    a_locked.wait(10)
    print(db.lock_manager.snapshot()["resources"])   # t 被 A 独占
    print(db.lock_manager.waiters("t"))              # B 的事务号在等待队列里
    release_a.set()
    ta.join(10); tb.join(10)
    print(db.execute("SELECT v FROM t").rows)        # [('B',)]
```

命令行可以直接看到事务 / 锁表 / 日志状态：

```powershell
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db --txn-status
.venv\Scripts\python.exe -m yoursql.cli --database .\tmp\optimizer-example.db --lock-mode none `
    --sql "BEGIN; SELECT COUNT(*) FROM employees; COMMIT;"     # 关闭并发控制做对照
```

死锁用"两个事务交叉抢占两张表"复现：等待图检出环后回滚**事务号最大（最年轻）** 的那个，
另一个拿到锁继续提交；锁等待超时（`--lock-timeout`，默认 5 s）同样抛 `ConcurrencyError`。

### WAL 与崩溃恢复

日志文件与数据库同目录，名为 `<db>.wal`。写前日志规则是"脏页写回磁盘前，先把它对应的日志 fsync"，
页头末尾 4 字节的对齐保留位现在承载该页的 LSN（旧库文件末 4 字节恒为 0，按 LSN=0 读，格式向后兼容）。

崩溃恢复的验证思路是**手工制造一次断电**：把脏页落盘（对应缓冲池的 steal 行为），
然后直接关闭文件句柄——不跑回滚、也不补 `commit` 记录：

```python
from yoursql.engine.runtime.database import Database

db = Database("tmp/crash.db")
db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
db.execute("INSERT INTO t VALUES (1)")
db.execute("BEGIN")
db.execute("INSERT INTO t VALUES (2)")     # 未提交
db.buffer_pool.flush_all()                 # 脏页已经落到磁盘
db.disk.close(); db.wal.close()            # 断电：没有任何事务收尾

with Database("tmp/crash.db") as recovered:
    print(recovered.recovery_report.rolled_back)          # (2,) —— 未提交事务被撤销
    print(recovered.execute("SELECT id FROM t").rows)     # [(1,)]
```

要点：

- **提交顺序即恢复策略**：先把目录与数据页全部刷盘并 fsync，**再**写 `commit` 记录。
  于是"有 commit 记录"等价于"改动都在磁盘上"，恢复只需回滚未提交事务，**不需要 redo**。
- 事务新申请的页由 `alloc` 记录记账，回滚时精确回收；恢复是幂等的（重复打开结果一致）。
- 日志尾部被写了一半的行按"尾部截断"容忍；中间出现坏行才报 `RecoveryError`。
- 关闭日志做对照：`--no-wal`（或 `DatabaseConfig(wal_enabled=False)`）——事务语义仍成立（回滚靠内存前像），但断电后无法撤销已落盘的未提交改动。
- 崩溃恢复确实发生时，CLI 会往 **stderr** 打一行 `[recovery] 回滚了 N 个未提交事务…`（不污染 `--json` 输出）。

### 自动化断言

```powershell
.venv\Scripts\python.exe -m pytest tests/test_transactions.py tests/test_concurrency.py tests/test_wal.py -q
```

- `tests/test_transactions.py`（16 条）：提交 / 回滚 / 自动提交、更新与删除的撤销、**索引条目一并回滚**、失败事务状态、DDL 事务（建表 / 建索引回滚）、`DROP TABLE` 守卫、脚本级事务、隔离级别切换、约束校验不自锁。
- `tests/test_concurrency.py`（10 条）：X 锁串行化、读等写、死锁检测选牺牲者、锁超时、`lock_mode=none`、两种隔离级别的 S 锁持有范围、4 线程 × 6 行并发写入行数完整。
- `tests/test_wal.py`（14 条）：页 LSN 落位、**写前日志规则**（挂在 `DiskManager.write` 上断言"落盘页 LSN ≤ 已 fsync 的 LSN"）、checkpoint 截断、崩溃回滚、已提交数据不丢、页回收、只撤销未提交事务、恢复幂等、残缺尾行容忍、坏行报错、关闭日志、旧页头兼容。

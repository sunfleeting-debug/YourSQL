# 基础查询优化复现用例

本用例覆盖：常量折叠、恒真/恒假条件消除、`AND` / `OR` 规则化简、常量表达式索引匹配、显式谓词下推、逗号连接的连接键推断与不相关子查询复用。

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

预期输出 5 条规则（`constant_folding` / `boolean_simplification` / `predicate_elimination` / `predicate_pushdown` / `index_selection`），每条带执行阶段与说明，开头为 `优化规则 5/5 条启用：`。

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

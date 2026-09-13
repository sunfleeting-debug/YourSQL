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

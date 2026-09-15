# YourSQL 项目导览

> 面向"完整了解 + 现场验收"的阅读路线。所有输出均为 2026-09-14 在本机实际运行所得。
> 配套文档：`docs/PROJECT_MAP.md`（逐文件职责）、`docs/RUNBOOK.md`（启动指南）、`docs/ARCHITECTURE.md`（架构边界）。

---

## 0. 心智模型

一句话：**一条单向的编译流水线**，SQL 文本从左边进，结果从右边出。

```
SQL 文本 → Token 流 → AST → 绑定 → 逻辑计划 → 优化改写 → 物理计划 → Volcano 执行 → 结果
```

三条铁律贯穿全项目：

1. **上游只依赖下游**。`sql`（语言前端）不许 import `engine` / `storage`；前端连存储长什么样都不知道。
2. **每个阶段只回答一个问题**，越界即错。
   - Lexer：这是不是一个合法的词？
   - Parser：这些词的组合合不合文法？
   - Binder：这些名字在目录里存不存在？
   - Optimizer：走哪条路最便宜？
   - Executor：怎么把计划跑出来？
3. **错误必须能定位到行列**，因为每个 Token 从出生就带 `line` / `column`。

---

## 1. 三十分钟速通路线

| 顺序 | 做什么 | 命令 |
| --- | --- | --- |
| 1 | 装环境 + 构建前端 | `cd web && npm install && npm run build && cd ..` |
| 2 | 起工作台看界面 | `python -m yoursql.web --database data/showcase_v2.db` |
| 3 | 看链路每一段长什么样 | 见下方第 2 节脚本 |
| 4 | 跑全量测试建立信心 | `python -m pytest -q`（312 passed / 约 100s） |
| 5 | 读 `docs/ARCHITECTURE.md` | 78 行，是全项目最浓缩的设计说明 |

工作台里最值得点的三个地方（验收演示用）：

- **结果区「流水线」页签**：同一棵算子树的 Token 表 → AST → 逻辑计划 → 优化计划 → 物理阶段，可缩放查看。
- **「存储」模式**：页面地图按真实字节边界画方格（页头 / MSP2 页头 / 槽目录 / 空闲区 / 记录区分别着色），点开看单页 Hex。
- **`EXPLAIN SELECT ...`**：直接看优化后的访问路径。

---

## 2. 全链路八阶段（带真实输出）

测试数据：`student(id, name, age, city)` 共 400 行；查询 `SELECT name FROM student WHERE age = 30;`

### ② Token 流 —— `yoursql/sql/lexer.py`

```json
{"kind": "SELECT",     "lexeme": "SELECT", "line": 1, "column": 1}
{"kind": "IDENTIFIER", "lexeme": "name",   "line": 1, "column": 8}
{"kind": "WHERE",      "lexeme": "WHERE",  "line": 1, "column": 26}
```

`TokenKind` 是 `str` 枚举，枚举值就是词面，报错时可直接回显。关键字成员顺序必须与 `_KEYWORD_NAMES` 一致（**增删关键字要同步两处**，代码里有注释提醒）。

### ③ AST —— `yoursql/sql/parser.py` + `ast.py`

```json
{"node": "Select",
 "items": [{"node": "SelectItem", "expression": {"node": "ColumnRef", "name": "name"}}],
 "from_table": {"node": "TableRef", "name": "student"},
 "where": {"node": "BinaryOp", "operator": "=",
           "left": {"node": "ColumnRef", "name": "age"},
           "right": {"node": "Literal", "value": 30}}}
```

解析器是**标准递归下降分层**，优先级由调用层次决定（`parse`(L649) 往下）：

```
_expression → _or → _and → _not → _comparison → _additive → _multiplicative → _unary → _primary
```

**加运算符就改这里**：先改 `lexer.py` 的 `TokenKind`，再在对应层次的 `_comparison` / `_additive` 里接上。

### ④ 绑定 —— `yoursql/sql/binder.py`

```json
{"output_columns": ["name"], "insert_indexes": []}
```

查 Catalog 确认 `student`、`age` 真实存在并算出输出列。
**表／列不存在是在这里报错，不是解析器报** —— 这是现场判定错误阶段最常见的考点。

### ⑤ 逻辑计划 —— `yoursql/planner/logical.py`

```
Project → Filter → SeqScan
```

此时还没有"用索引还是全表扫"的概念，只有关系代数的形状。

### ⑥ 优化改写 —— `yoursql/planner/optimizer.py`

同一棵树，属性变了：

```json
{"node": "Filter", "properties": {"predicate": {...}, "pushed": true}}
{"node": "SeqScan", "properties": {"table": "student",
   "pushed_predicate": {"node": "BinaryOp", "operator": "=", ...}}}
```

`pushed: true` 说明谓词下推生效；被折叠前后的 AST 会挂回计划根节点，**执行时用的就是这份 AST**（`database.py` 注释：避免优化结果只停留在 EXPLAIN 展示层）。

### ⑦ 物理计划 —— `yoursql/planner/physical.py`

逻辑与物理目前共享 `PlanNode` 字段，`as_physical()` 做标记转换。设计上刻意保持一致，好处是优化规则可以逐节点复用。

### ⑧ 执行 —— `yoursql/execution/`

实测三种情况的对比（**这是"可优化"验收点最有力的证据**）：

| 场景 | 访问路径 | 实测 stats |
| --- | --- | --- |
| 无索引 | `SeqScan` | `rows_examined: 13` |
| 建 `idx_age` 后 | `IndexScan` | `cache_hits: 13, rows_examined: 13` |
| `age >= 18`（命中 380/400） | **退回 `SeqScan`** | `scan_reason: 索引选择性过低，改用顺序扫描` |

---

## 3. 四大子系统

### 3.1 SQL 前端 `yoursql/sql/`（7 文件）

| 文件 | 关键点 |
| --- | --- |
| `lexer.py` | 手写扫描器；`TokenKind` 枚举 + 关键字表双写 |
| `parser.py` | 942 行递归下降；`parse_one()` 单条 / `parse_script()` 多条 |
| `ast.py` | 纯数据节点，`to_dict()` 直接给工作台画树用 |
| `binder.py` | 只读 Catalog 协议，不修改目录 |
| `compiler.py` | `CompilationResult` 一次性返回 tokens / ast / bound / plan |

`CompilationResult` 就是验收"现场画 AST/Plan"的最佳抓手 —— 一个对象里五份产物都有。

### 3.2 计划与优化 `yoursql/planner/`（5 文件）

**优化规则共 7 条**（即 `DEFAULT_RULES`，满足计划书"≥2 条规则"；可用 `--rules` 枚举、`--disable-rule` 单独关闭）：

| 规则（`DEFAULT_RULES` 名称） | 实现位置 |
| --- | --- |
| `constant_folding` 常量折叠 | `constant_value` / `_fold_binary` / `_rewrite_expr` |
| `boolean_simplification` 布尔化简（三值逻辑） | `_simplify_boolean` / `_and_truth` |
| `predicate_elimination` 恒真/恒假过滤消除 | `_rewrite_plan_node`（谓词阶段） |
| `predicate_pushdown` 谓词下推 | `_push_predicates` / `_push_into_subtree` / `_can_push_to` |
| `index_selection` 访问路径选择 + 覆盖索引 | `_choose_scan` / `should_use_index` / `should_use_index_only` |
| `join_reordering` 等值键贪心重排连接顺序 | `_reorder_joins` / `_equi_join_pair` |
| `limit_pushdown` 限行下推 / `top_n` 标注 | `_rewrite_plan_node`（基数阶段）；运行时由 `_sort_projected` 消费 `top_n` |

**两个必须记住的阈值常量**（`optimizer.py` L51-54）：

```python
_SMALL_TABLE_ROW_THRESHOLD = 128      # 小表直接可用索引
_INDEX_SELECTIVITY_THRESHOLD = 0.20   # 候选超 20% 倾向顺序扫描
```

代价模型常数在 `planner/cost.py`：`SEQ_PAGE_COST`、`DECODE_ROW_COST`、`INDEX_ENTRY_COST`、`INDEX_ONLY_ENTRY_COST`、`RANDOM_PAGE_COST`。

`should_use_index()` 的 docstring 里写了实测依据：**顺序扫描 4.2 µs/行 vs 随机回表 98.6 µs/行**，只看"候选行占比"会把 18% 候选的查询错判给索引（实测慢 3 倍）。准备答辩时这里是最有说服力的细节。

**计划缓存**：`PlanCache` 容量 256，键是规范化 SQL；写操作 / DDL 会 `invalidate()`。

### 3.3 执行 `yoursql/execution/`（4 文件）

- `executor.py`：与 SQL 无关的 Volcano 契约 —— 每个算子只有 `open()` / `next()` / `close()`。
  实现：`Values` / `SeqScan` / `Filter` / `Project` / `Sort` / `Limit` / `NestedLoopJoin` / `Aggregate`。
- `evaluator.py`：`_compile_expr`(L235) 把表达式编译成闭包（比逐行解释快），`_eval_expr`(L399) 求值，
  `_eval_function`(L521) 内置函数。聚合：`count/sum/avg/min/max`；标量：`lower/upper/length/len/abs/coalesce/date`。
- `query.py`：1904 行，SELECT 的扫描、连接、索引访问与投影编排。
  连接策略有**哈希连接 / 索引嵌套循环 / 嵌套循环**三种，测试在 `tests/test_join_strategies.py`。

### 3.4 存储 `yoursql/storage/`（6 文件）

**页格式**（`page.py`）：

- 页头 30 B：`magic "MDBP"`(4) + `version`(4) + `type`(1) + 保留(1) + `page_id`(8) + `payload_len`(4) + `CRC32`(4) + 对齐保留(4)
- 页类型：`FREE` / `SUPERBLOCK`（第 0 页）/ `CATALOG` / `HEAP` / `INDEX`
- HEAP 页负载：`MSP2` 双向槽式头 12 B + 槽目录（6 B/项，**向后增长**）+ 空闲区 + 记录区（**向前生长**）

**其他组件**：

| 文件 | 职责 |
| --- | --- |
| `disk.py` | 单文件页式管理 + superblock 识别页大小 |
| `buffer.py` | 固定容量页缓存，LRU / FIFO 可切换（默认 64 页），Pin 中的页不进入淘汰队列 |
| `heap.py` | 变长记录堆表；优先增量分配保持未受影响记录的物理 offset，删除保留槽号 |
| `index.py` | 1825 行，落盘 B+Tree（`MBIX` JSON 负载），Catalog 存根页号，叶子链连接 |

---

## 4. 目录与权限

- **Catalog**（`engine/catalog.py`）是前端与后端的**唯一共享结构**：表 / 字段 / 索引 / 视图元数据的 JSON，链式 `CATALOG` 页持久化。
- **RBAC**（`engine/security/`）：用户、角色、对象权限、PBKDF2-HMAC-SHA256 加盐哈希、JSON Lines 审计日志。
- **内部权限表**：`_sys_users` / `_sys_roles` / `_sys_role_members` / `_sys_privileges` 四张隐藏 Heap 表。
- **系统视图**：`sys_users` / `sys_roles` / `sys_role_members` / `sys_privileges`，admin-only 只读，回读内部表且不分配独立数据页；`sys_users` 不暴露密码哈希，普通用户即使有全库 `SELECT` 也看不到。

---

## 5. 服务层 `yoursql/engine/services/`

| 服务 | 说明 |
| --- | --- |
| `http.py` | 标准库 HTTP，默认只监听本机；兼容 `/health` `/metrics` `/sql` + 完整 `/api/*` |
| `workbench.py` | 有界任务队列、持久化认证、查询历史；查询超时 / 取消 / 结果保留字节上限可配 |
| `workbench_sql.py` | 语句切分、密码脱敏、错误锚点、阶段采集、结果列类型推断 |
| `inspection.py` | 存储只读检查：页头总览（500 页/批）、Buffer Pool 快照、索引快照、单页详情 |
| `ssh.py` | 零依赖 stdio 协议：每行 SQL → 每行 JSON，可挂 OpenSSH 强制命令 |

SQL 边界（`workbench_sql.py`）值得单独提：**语句数量、SQL 长度、请求体、结果行数、会话数、任务数全都有上限**，这是"稳健"验收点的落点。

---

## 6. 验收演练手册

### 6.1 现场加需求：先判断属于哪一层

```
加内置函数    → evaluator._eval_function → binder 类型校验 → examples/test_example.md
加运算符      → lexer.TokenKind(+关键字表) → parser._comparison/_additive → evaluator 求值 + optimizer 折叠
加优化规则    → optimizer._rewrite_plan_node → cost 常数 → tests/test_optimizer_rules.py 断言计划变化
加 SQL 语句   → ast.py 新节点 → parser._statement → binder → logical.plan_from_statement → commands.py 执行
```

**回答话术**："这是语言前端需求，改 lexer 和 parser；执行语义落在 evaluator；如果新运算符要参与索引选择，还要在 optimizer 的谓词分析里登记。"

### 6.2 现场判定错误阶段（全部实测）

| 输入 | 实际抛出的异常 | 实测消息 |
| --- | --- | --- |
| `... WHERE age = 1 @ 2;` | `LexerError` | `[LEXER_ERROR] at line 1, column 37: 非法字符 '@'` |
| `... WHERE city = 'beijing;` | `LexerError` | `[LEXER_ERROR] at line 1, column 36: 字符串未闭合，期望单引号` |
| `SELECT * FROM "student;` | `LexerError` | `[LEXER_ERROR] at line 1, column 15: 引用标识符未闭合` |
| `... WHERE age = ;` | `ParserError` | `[PARSER_ERROR] at line 1, column 35: 需要表达式` |
| `... WHERE age = 1a;` | `ParserError` | `[PARSER_ERROR] at line 1, column 36: 多语句脚本中的语句必须以分号分隔` |
| `SELECT nosuch FROM student;` | `BinderError` | `[BINDER_ERROR] at line 1, column 8: 列 'nosuch' 不存在` |
| `SELECT * FROM nosuchtable;` | `BinderError` | `[BINDER_ERROR] at line 1, column 15: 表或视图 'nosuchtable' 不存在` |
| `SELECT name WHERE age = 30;` | `BinderError` | `[BINDER_ERROR] at line 1, column 8: 列 'name' 没有可绑定的表` |
| `SELECT 1/0;` | `ExecutionError` | `[EXECUTION_ERROR] at line 1, column 9: 除数不能为零` |

两个容易答错的点，值得提前准备：

1. **`SELECT name WHERE age = 30;`（缺 FROM）报的是语义错不是语法错** —— 因为文法允许无 FROM 的 SELECT（`SELECT 1;` 合法），只有绑定时才发现列没有来源。
2. **`age = 1a` 报"语句必须以分号分隔"** —— 词法把 `1` 和 `a` 切成两个合法 Token，解析器看到 SELECT 语句结束后还有多余 Token。

异常层次（`common/errors.py`）：`YourSQLError` → `LexerError` / `ParserError` / `BinderError` / `CatalogError` / `StorageError` / `ExecutionError` / `AuthorizationError`，都带行列。

### 6.3 现场画 AST / Plan

一条命令拿全部产物：

```bash
python - <<'EOF'
from yoursql.engine.runtime.database import Database
db = Database("demo.db")
c = db.compile("SELECT name FROM student WHERE age = 30;")
print("Token:", [t.lexeme for t in c.tokens])
print("AST  :", c.ast.to_dict())
print("逻辑 :", c.plan.to_dict())
print("物理 :", c.optimized_plan.to_dict())
EOF
```

工作台里对应的可视化：结果区「流水线」页签 → AST 树 / 逻辑计划 / 优化计划 / 物理阶段算子图。

### 6.4 五条贯穿性标准的落点

| 标准 | 证据 |
| --- | --- |
| 正确 | `tests/` 201 个用例；`benchmarks/verify_tpch_values.py` 与 SQLite 逐值对拍（16 条 TPC-H） |
| 稳健 | 结构化异常 + 行列定位；SQL/请求/结果/会话全边界限流；CRC 校验页头 |
| 可扩展 | 分层 + 目录归属约定，`tests/test_planner_layers.py` 固化边界防越权 |
| 可优化 | 5 类规则；索引选择性退化实测；EXPLAIN 可解释 |
| 可验证 | `examples/test_example.md` 可复现用例；TPC-H 基准 + 多引擎对照报告 |

---

## 7. 命令速查

```bash
# 启动
cd web && npm install && npm run build && cd ..        # 仅首次
python -m yoursql.web --database data/showcase_v2.db --port 8080   # admin / admin

# 快速验证
python -m yoursql.cli --database demo.db --sql "SHOW TABLES; SELECT 1;"
python -m pytest -q
cd web && npm test

# 优化规则与查询计划（不执行，只看计划）
python -m yoursql.cli --rules                                  # 规则清单：名称 / 阶段 / 说明
python -m yoursql.cli --database demo.db --sql "SELECT 1 FROM t WHERE id = 1" --plan mermaid
python -m yoursql.cli --database demo.db --sql "SELECT 1 FROM t WHERE id = 1" \
    --plan dot --disable-rule index_selection                  # 现场对比关规则前后的计划

# 脚本语法检查：一次报全所有错误，不执行
python -m yoursql.cli --sql "SELECT 1; SELCT 2; SELECT @ FROM t;" --check

# 把计划导出成可浏览的 HTML（Mermaid 从 CDN 加载）
python -m scripts.plan_visualize --database demo.db \
    --sql "SELECT 1 FROM t WHERE id = 1" --format html --out docs/plan_demo.html

# 演示库
python -m examples.create_showcase_db --force

# 基准
python benchmarks/run_tpch_queries.py
python benchmarks/compare_tpch_queries.py
```

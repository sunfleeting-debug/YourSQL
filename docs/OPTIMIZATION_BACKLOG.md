# 优化候选清单（实测证据版）

> 测量时间：2026-09-14 晚 · 数据集：TPC-H SF0.01 的 `lineitem`（60,175 行 / 3,088 页 / 4096 B 页）
> 口径：**每条查询独立进程**，预热 1 次后取 3 次最小值；内存峰值用 `tracemalloc` 单独一轮测量，
> 不参与计时（`tracemalloc` 会把耗时放大数倍）。
> 本文最初只做分析与取证、不含代码改动；**下面的 P1-1 / P1-2 / P2-1 / P2-2 与 P3-2 的降级提示已于同日实施**，
> 实施后的实测数字见第 7 节，正文保留原始分析以便对照"改之前是什么样"。

## 0. 先说三件影响阅读的事

1. **工作区在我测量期间处于并发编辑状态**：`yoursql/storage/heap.py`、`yoursql/storage/page.py` 在 18:56 被改，
   `yoursql/execution/query.py` 在 18:57 被改，`tests/test_scan_fast_path.py` 19:05 新增。
   也就是说"**零列扫描快路径**（`COUNT(*)` 不再逐行解码）"和"**投影列裁剪**"这两件事已经有人在做了，
   本文**不把它们列为待办**；第 4 节的基线是这些改动生效之后的现状。
2. 时间数字只用来比较数量级。同一份代码在不同时段复测会出现 2 倍级差异（README 已记录 Q6 在
   0.30–0.57 s 间漂移），所以本文所有结论都建立在"同一轮、同一台机、同一口径"的对照上。
   交叉验证过一处易踩的坑：`tracemalloc` 会把耗时放大 3–5 倍（同一条 `COUNT(*)` 计到 1070 ms，
   关掉后是 88.8 ms），所以内存与时间必须分轮测量。
3. 第 3 节列的是**已经用实验证伪、不建议再试**的方向——这部分和待办同样有价值，能省掉重复踩坑。

## 1. 结论速览

| 优先级 | 候选 | 实测依据（同一轮对照） | 预期收益 | 工作量 | 风险 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| **P1** | 连接顺序不做任何搜索/重排 | 同一个三表连接，6 种 `FROM` 写法里 **2 种 >45 s 未跑完**，其余约 0.85 s | 最坏 50×+ | 中 | 中（语义必须保持不变） | ✅ 已实施：`join_reordering` |
| **P1** | `ORDER BY` + `LIMIT` 没有 top-N | `LIMIT 10` 0.2 ms；`ORDER BY x LIMIT 10` **600.8 ms / 54.7 MB** | 时间 ~10×、内存 O(k) | 小 | 低（NULL/多键排序要覆盖） | ✅ 已实施：`limit_pushdown` |
| **P2** | 分组聚合按行持有整组上下文 | 单组聚合峰值 **35.5 MB**、分组聚合 27.8 MB；`groups[key]` 存的是逐行 dict | 内存 O(组数) | 中 | 中（NULL/空组/HAVING 语义） | ✅ 已实施：流式累加器 |
| **P2** | 索引嵌套循环在常规规模不可达 | 左 2 行 × 右 20,000 行且有索引：默认预算选 `hash`，把预算压到 46,400 B 才选 `index` | 小表驱动大表时省建表开销 | 小 | 低 | ✅ 已实施：三候选同层取最小 |
| **P2** | 投影完成后仍长期持有行上下文 | 3 列投影 **64.3 MB** vs 1 列 44.4 MB；ORDER BY 只用输出别名时上下文已无用 | 结果集内存降约 1/3 | 小 | 低 | ⬜ 未做 |
| **P3** | 相关子查询逐行重跑 | TPC-H Q2/Q4/Q15/Q20/Q21 分钟级超时、Q22 约 50 s（既有结论） | 数量级 | 大 | 中 | ⬜ 未做 |
| **P3** | 超预算哈希连接直接掉进嵌套循环 | 阈值 = 256 MB ÷ 464 B ≈ **578,524 行**；越过即退化为 10.4 µs/行·对 的嵌套循环 | 避免内存悬崖 | 大 | 中 | ◐ 已加降级提示（`stats["join_degrade"]`），连接算法本身未做 |

## 2. 值得做的

### P1-1 连接顺序不做搜索：同一个查询最坏慢 50 倍以上

**现象.** 同一个逻辑查询（`lineitem ⋈ orders ⋈ customer`），只改 `FROM` 里表的书写顺序，性能差两个数量级。

**证据.** 6 种排列，每条查询独立进程 + 独立 45 s 超时：

| `FROM` 顺序 | 结果 | 进程墙钟 |
| --- | --- | --- |
| `lineitem l, orders o, customer c` | 979.5 ms · `joins=['HashJoin','HashJoin']` | 1360 ms |
| `lineitem l, customer c, orders o` | **>45 s（超时）** | — |
| `orders o, lineitem l, customer c` | 863.6 ms | 1230 ms |
| `orders o, customer c, lineitem l` | 834.8 ms | 1202 ms |
| `customer c, lineitem l, orders o` | **>45 s（超时）** | — |
| `customer c, orders o, lineitem l` | 834.7 ms | 1196 ms |

两个超时排列的共同点：**第一层被连接的两张表在 `WHERE` 里没有可用的等值键**。
`lineitem` 与 `customer` 之间没有直接连接列（键要经过 `orders`），而"从 `WHERE` 推断连接键"只认
"引用表都已在左侧就绪"的条件，于是第一层退化成 `60,175 × 1,500` 的笛卡尔积，后面怎么连都救不回来。
反观能跑完的四种写法，第一层都是 `lineitem ⋈ orders`（键立刻可用，中间结果仍是 60,175 行）。

**根因.** 执行层只按 `FROM` 的书写顺序逐层连接（`_joined_contexts` 的循环顺序即 `from_table` → `joins`），
没有任何"选择连接顺序/选择驱动表"的步骤；计划层也没有 join 重排规则
（`DEFAULT_RULES` 里只有表达式与谓词类规则，见 `yoursql/planner/optimizer.py`）。

**建议.** 二选一或叠加：

- **轻量版（推荐先做）**：对逗号连接/多表连接的等值键做一次**贪心重排**——每次从剩余表里挑
  "与已连接集合有等值键、且预计中间结果最小"的那张表；无等值键的（必须做笛卡尔积）放到最后。
  代价用现成的 `TableStats.row_count` 即可，不需要新的统计。
- **完整版**：在 `planner` 层加一条 `join_reordering` 重写规则（同时就能满足"≥2 条可枚举规则"的验收点，
  现在已有 5 条，加进去顺理成章），并在 `plan.properties["rules"]` 里留下痕迹。

**验证.** 上表 6 种排列的结果必须完全一致（都是 60,175 行），且任意排列的耗时都应落在同一量级；
`stats["joins"]` 应显示引擎改写了连接顺序。可直接把上表固化成参数化测试。

### P1-2 `ORDER BY` + `LIMIT` 没有 top-N：比无排序 `LIMIT` 慢 2000 倍

**现象.** 排序后的 `LIMIT` 拿不到限行带来的任何好处。

**证据.**

| 查询 | 时间 | 峰值内存 |
| --- | --- | --- |
| `SELECT l_orderkey FROM lineitem LIMIT 10` | **0.2 ms** | 0.0 MB |
| `SELECT l_orderkey FROM lineitem ORDER BY l_extendedprice LIMIT 10` | **600.8 ms** | **54.7 MB** |
| `SELECT l_orderkey FROM lineitem ORDER BY l_extendedprice`（全量） | 589.5 ms | 54.9 MB |

`ORDER BY ... LIMIT 10` 与不带 `LIMIT` 的全量排序几乎同价——`LIMIT` 在这里只是最后切了一刀。

**根因.** 两处叠加（`yoursql/execution/query.py`）：

- 提前终止的条件把排序排除在外：`streamable = not statement.order_by and not statement.distinct and ...`（L345），
  于是排序查询必然全量消费输入；
- 投影结果全量物化后再 `projected.sort(...)`（L411/L413），最后才 `projected[statement.offset:statement.limit]`（L417/L419）。

**建议.**

- 存在 `ORDER BY` + 较小 `LIMIT`（`offset + limit` 远小于行数）时，改用**有界堆**：把
  `(排序键, 行)` 维持一个大小为 `k` 的堆，扫描一遍即得前 k 行。多键排序、`DESC`、
  `NULLS FIRST/LAST` 需要把排序键压成单一复合键（NULL 位 + 值，DESC 用取反或自定义比较包装）。
- 另一条独立的路径：**让索引的有序性服务于 `ORDER BY`**。B+Tree 叶子链本身就是有序的，
  `ORDER BY 索引首列 LIMIT n` 完全可以沿叶子链取前 n 条后提前终止。当前 `_scan_contexts`
  只在 `WHERE` 上取索引候选集，没有任何"按索引顺序返回"的路径。

**验证.** `ORDER BY x LIMIT 10` 的时间应降到与 `LIMIT 10` 同一量级（百毫秒级以下）、内存降到 O(k)；
结果与现在逐值一致（含 `NULLS FIRST/LAST`、多键、`DESC` 的组合用例）。

### P2-1 分组聚合按行持有整组上下文

**现象.** 聚合的内存随**行数**线性增长，而不是随组数。

**证据.** 单组聚合峰值 **35.5 MB**（≈610 B/行 × 60,175），三组 `GROUP BY` 也有 **27.8 MB**：

```
SELECT COUNT(*) FROM lineitem;                                  → 88.8 ms /  0.8 MB   ← 零列快路径，不建上下文
SELECT SUM(l_quantity) FROM lineitem;                           → 415.6 ms / 35.5 MB
SELECT l_returnflag, COUNT(*) FROM lineitem GROUP BY l_returnflag; → 406.4 ms / 27.8 MB
```

**根因.** `_execute_select` 的分组循环是 `groups.setdefault(key, []).append(context)`
（`yoursql/execution/query.py` L314）——**把每个分组的所有行上下文都留在内存里**；
聚合求值时再对每个聚合函数各自遍历一遍这个列表
（`yoursql/execution/evaluator.py` L790 起：`values = [self._eval_expr(arg, item) for item in group]`），
所以 1 个聚合是 1 次全量遍历、5 个聚合就是 5 次。

**建议.** 改成**流式累加器**：`dict[group_key] → {count, sum, min, max}`，边扫边累积，只在收尾时算
`AVG`（sum/count）与 `HAVING`。`COUNT/SUM/MIN/MAX/AVG` 全是可结合的，`COUNT(DISTINCT)` 单独用 set 兜住。
收益：内存从 O(行数) 降到 O(组数)（无 `GROUP BY` 时是常数级），并把"每聚合一遍全量遍历"降到一遍。

**风险点（必须覆盖）.** 无 `GROUP BY` 的空输入要产出单行（现有代码专门处理了这一点）、
`NULL` 不计入 `COUNT(列)`/`SUM`、`HAVING` 引用聚合值时的求值时机。用现有
`tests/test_sql_features.py` + TPC-H 逐值对拍兜住即可。

### P2-2 索引嵌套循环在常规规模不可达

**现象.** 右表有索引、左表只有几行时，引擎仍然选择哈希连接。

**证据.** 直接调内部决策函数（`_choose_join_strategy`，`yoursql/execution/query.py` L1239）：

```
左 2 行 × 右 20,000 行（右表有索引）
  默认预算（256 MB）   → hash
  预算压到 46,400 B    → index
  预算压到 464 B       → index
```

**根因.** 闸门顺序（L1257）：`if hash_fits and hash_cost <= nested_cost: return "hash"`。
而 `hash = 0.22µs·R + 0.05µs·L`、`nested = 10.4µs·L·R`，对任意 `L,R ≥ 1` 都有 `hash ≤ nested`
（L=R=1 时 0.27 µs vs 10.4 µs），所以**只要哈希装得下就永远返回 hash**，第 2 道闸（索引连接）只是
"哈希装不下时的备选"。`hash_fits` 的边界是 256 MB ÷ 464 B ≈ 578,524 行——这就是索引连接要能自然被选中
所需的最小规模，项目自己的回归测试也得靠 monkeypatch 把预算压到 464 B 才能触发它。

**建议.** 把索引连接**提前到同一层参与比较**：`min(hash(若可容纳), index(若有索引), nested)`，
而不是"装不下才考虑"。代价模型里的常数已实测标定（1.4 ms/次索引查找），直接复用。

**验证.** 断言"小表驱动 + 右表有索引"时 `stats["joins"]` 为 `IndexNestedLoop`，且结果与哈希连接逐值一致
（现有测试已证明两者结果一致）。

### P2-3 投影完成后仍长期持有行上下文

**证据.** 同一次全表扫描，输出列越多内存越高，而增量明显不成比例：

| 查询 | 时间 | 峰值内存 |
| --- | --- | --- |
| `SELECT COUNT(l_quantity) FROM lineitem` | 422.9 ms | 35.0 MB |
| `SELECT l_quantity FROM lineitem` | 455.0 ms | 44.4 MB |
| `SELECT l_quantity, l_tax, l_discount FROM lineitem` | 537.3 ms | **64.3 MB** |

**根因.** 投影阶段的元素是三元组 `(输出值, 行上下文, 元数据)`，排序键的求值需要 `item[1]`（行上下文）——
但**只有 ORDER BY 引用非投影表达式时才真的需要它**。ORDER BY 只用输出列/别名时（最常见的情形），
6 万行的行上下文（每行约 200–600 B）就是纯负担。

**建议.** 投影结束后判断"`ORDER BY`/`DISTINCT` 是否还引用行内未输出的列"；不需要就把三元组收敛为
`(输出值, 元数据)`，行上下文提前释放。**注意**：这一条我是从代码结构推的，收益量级尚未单独实测
（要改代码才能量），下面给的是"3 列 vs 1 列的差值"作为上界参考（约 20 MB / 6 万行）。

### P3-1 相关子查询逐行重跑（既有结论，补上可执行方案）

Q2/Q4/Q15/Q20/Q21 分钟级超时、Q22 约 50 s，都是"外层每行重跑一次子查询"。现有代码已经能做到
"不相关子查询只执行一次并物化"，相关子查询没有去相关。可执行的三步：
`min(ps_supplycost)` 这类标量聚合 → 改写成"派生表 + `GROUP BY`"再连接；
`EXISTS` → 半连接（semi-join）；`IN` → 半连接或物化集合。改造后同样要过逐值对拍。

### P3-2 超出内存预算的哈希连接会掉进嵌套循环

`hash_fits` 为假时，策略退到 `index`（需要索引）或 `nested_loop`。两表各 100 万行且无索引时，
嵌套循环是 10¹² 量级的配对——不是"慢一点"，而是不可用。缺的是**排序归并连接**或**分区溢出的哈希连接**
（grace hash）。至少也应在这种形态下给出明确的降级提示（写进 `stats` 或发出警告），
而不是让查询静静跑上几小时。

## 3. 已实测证伪：这些方向别再试

| 候选 | 实验做法 | 结果 |
| --- | --- | --- |
| `Decimal` 构造加 LRU 缓存 | `_decimal_hook` 里把 `Decimal(text)` 换成 `lru_cache` 版本 | 命中率 **82.5%**，`SUM` 全表 403 → 431 ms（**0.94×**，没收益）。短字符串的 `Decimal(str)` 本身是 C 实现，`lru_cache` 的哈希/查找开销与之相当 |
| 哈希连接按行数选建侧（小的那侧建表） | 同一两表连接交换 `FROM` 顺序 | 817 ms vs 888 ms（噪声内，且方向与预期相反）。扫描与上下文合并才是主导，建表开销（0.22 µs/行）不是 |
| 页解析结果跨扫描复用 | 缓存 `SlottedPage.from_page` 结果 | 646.9 → 618.2 ms（**4%**）。收益太小，不值得引入"什么时候失效"的复杂度 |
| 消除编译期的重复表达式重写 | 计时 `db.compile(Q6)` | **0.58 ms/次**，相对 400 ms 的查询可忽略 |
| 计划缓存键的计算 | `PlanCache.key()` 计时 | **0.175 ms/次**，绝对值可忽略 |

README 里还有三条已证伪的旧记录（二进制逐列解码比 `json.loads` 更慢、缓冲池容量无效应、
比较运算符内联无收益），一并避开。

## 4. 现状基线（供回归对照）

TPC-H SF0.01 `lineitem`，60,175 行，独立进程、best-of-3：

| 操作 | 时间 | 峰值内存 |
| --- | --- | --- |
| `COUNT(*)` 全表（零列快路径） | 88.8 ms | 0.8 MB |
| 纯槽位统计（不解码，存储层） | 99.6 ms | — |
| `COUNT(列)` 全表 | 422.9 ms | 35.0 MB |
| `SUM(定点列)` 全表 | 415.6 ms | 35.5 MB |
| 投影 1 列 / 3 列 全表 | 455.0 / 537.3 ms | 44.4 / 64.3 MB |
| `GROUP BY`（3 组） | 406.4 ms | 27.8 MB |
| 两表连接（聚合输出） | 695.1 ms | 80.2 MB |
| `LIMIT 10` | 0.2 ms | 0.0 MB |
| `ORDER BY + LIMIT 10` | 600.8 ms | 54.7 MB |
| 逐行 JSON 解码 60,175 行（单独测） | 225.5 ms | — |
| 读页 + 槽目录解析 + 取原始字节（3,088 页） | 83.6 ms | — |

这次 profile 还确认了两件已经在做的事情是真收益：`heap.count()` **不调用** `codec.loads`
（0 次 vs `heap.scan()` 的 60,175 次），零列查询比逐行扫描快 **8–10 倍**。

## 5. 可验证性建议（工程侧）

1. **性能回归还没有自动化**。现在所有性能数字都来自手动跑 `benchmarks/*`，且同一份代码会漂 2 倍。
   建议把本文这种"独立进程 + 预热 + best-of-N + 固定数据集"的模式固化成
   `benchmarks/run_baseline.py`（输出 JSON、可比对阈值），否则"优化到底有没有效"只能靠当天手测。
   README 里 `--settle-seconds` 的经验（装载后静置再计时，否则 OS 回写会污染测量）应一并固化进去。
2. **每项优化都要有反例测试**：TOP-N 要覆盖 `NULLS FIRST/LAST` 与多键；连接重排要覆盖所有排列结果一致；
   流式聚合要覆盖空表与空组。AGENTS.md 已经要求"优化规则必须同时断言结果正确性和计划变化"，可以照此执行。
3. 前端（`web/`）目前 `npm test` 只有 4 条用例，工作台的页面地图、分页、存储联动主要靠接口测试兜底，
   补关键交互的回归成本不高、收益不小。

### 本次测量脚本

`tmp/` 是忽略目录（不进版本库），本次取证用的脚本在其中，均为"一次性探针"：

| 脚本 | 用途 |
| --- | --- |
| `tmp/probe5.py` / `probe8.py` | 零列快路径是否生效、`heap.count()` 与 `scan()` 的差异、聚合内存 |
| `tmp/probe6.py` | cProfile 聚合路径；行解码占比拆解 |
| `tmp/probe7.py` | `Decimal` 构造缓存的证伪实验 |
| `tmp/probe9.py` / `probe10.py` | 连接顺序敏感性（逐条独立进程 + 独立超时） |
| `tmp/probe11.py` | 连接策略闸门、建侧交换、页解析复用 |
| `tmp/probe12.py` / `probe13.py` | `ORDER BY` + `LIMIT` 与基线表（独立进程 best-of-3） |

> `tmp/` 下另有若干不属本文的脚本（`bench_case.py`、`codec_bench.py`、`profile_agg.py`、`verify_*.py` 等），
> 是同期另一路工作在用的，不要混用。

## 6. 附：功能缺口（不是性能问题，列在这里以免混淆）

- ~~**没有事务语句**~~：已补齐（`BEGIN/COMMIT/ROLLBACK` + 表级 S/X 锁 + WAL 崩溃恢复，
  见 `docs/TODO.md` 的「事务、并发与预写日志」一节）。
- **索引有序性未用于 `ORDER BY`**（与 P1-2 的第二条建议是同一件事）——P1-2 本次只做了 top-N 有界堆，
  "沿叶子链顺序取前 n 条"这条独立路径仍未做。
- 连接顺序、join 重排属于**计划层**能力：现在计划层已有 7 条具名规则（含 `join_reordering`、
  `limit_pushdown`）。

## 7. 实施结果（2026-09-14 晚，同日补测）

同机、同数据集（TPC-H SF0.01 `lineitem`，60,175 行）、同口径（独立进程、预热后 best-of-N、
内存单轮 `tracemalloc`）。A/B 通过 `Database(disabled_rules=(...))` 现场切换，保证两侧只差这一条规则。

| 项目 | 规则/机制 | 改前 | 改后 | 结果一致性 |
| --- | --- | --- | --- | --- |
| **P1-2** `ORDER BY l_extendedprice LIMIT 10` | `limit_pushdown`（计划标 `top_n` + 运行时截断排序） | 627.6 ms / 峰值 **55.5 MB** | **475.0 ms / 1.16 MB**（时间 1.32×、内存 48×） | 结果指纹逐值一致（7 组组合用例：`NULLS FIRST/LAST`、多键、`DESC`、`OFFSET`） |
| **P1-1** 三种表连接（6 种 `FROM` 顺序） | `join_reordering`（按 `row_count` 贪心重排 + 等值边选取） | **2/6 顺序 >60 s 超时**，其余 0.83–0.98 s | **6/6 全部完成，1.18–1.65 s** | 6 种写法结果指纹完全一致（均 60,175 行） |
| **P2-1** 单组聚合 `SUM(l_quantity)` | 流式累加器（`_AggregateAccumulator`，内存 O(组数)） | 峰值 **35.5 MB** | **1.15 MB** | 与改前逐值一致；`tests/test_aggregation_streaming.py` 15 条固定 NULL/空输入/DISTINCT/位置语义 |
| **P2-1** 三组 `GROUP BY l_returnflag` | 同上 | 峰值 **27.8 MB** | **1.24 MB** | 同上 |
| **P2-2** 左 2 行 × 右 20,000 行（右表有索引） | 三候选同层 `min(hash, index, nested)` | 默认预算选 `hash` | 默认预算选 **`IndexNestedLoop`** | 与哈希连接逐值一致（`tests/test_join_strategies.py`） |
| **P3-2** 哈希预算溢出 | 降级提示写入 `stats["join_degrade"]` | 静默掉进嵌套循环 | `stats` 带可读原因，EXPLAIN/CLI 可见 | — |

**口径提醒.** 上表时间数字只用于同轮对照：本轮复测发现同一条 `COUNT(*)` 从 88.8 ms 漂到 118.4 ms（+33%），
所以"改后比改前快 1.32×"只在同轮 A/B 内成立；纯投影地板（不含排序）实测 455.9 ms，
即 top-N 后排序开销已基本归零。

**新增/更新的回归测试**（全套 251 → 277 passed）：
`tests/test_aggregation_streaming.py`（新增 15 条）、`tests/test_optimizer_rules.py`（+3 条 limit_pushdown）、
`tests/test_join_strategies.py`（+6 条三候选/降级/重排/EXPLAIN 一致）、`tests/test_streaming_and_batch_load.py`
（+1 条 top-N 与全量排序等价）。

**仍未做**：P2-3（投影后释放行上下文）、P3-1（相关子查询去相关）、P3-2 的连接算法本体
（排序归并 / grace hash），以及 P1-2 建议里的"索引有序性直接服务 ORDER BY"。

**实施中踩到的坑（已修）**：`join_reordering` 最初只在**顶层**语句是 `Select` 时触发，
而 `EXPLAIN` 是独立语句类型（内层 SELECT 在 `Explain.statement`）——于是 `EXPLAIN SELECT ...`
整条跳过重排：EXPLAIN 打印 `FROM` 的书写顺序，实际执行却按重排后的顺序跑，**计划与执行不一致**，
现场也没法用它演示规则。现由 `_reorder_joins_in_statement` 穿透 `EXPLAIN` 节点，
断言见 `tests/test_join_strategies.py::test_explain_shows_the_same_join_order_as_execution`。

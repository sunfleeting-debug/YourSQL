# 引擎深挖：连接策略与 B+Tree 页维护

> 本文是对 `docs/GUIDE.md` 的两处下钻，所有数字均为 2026-09-14 在本机实测所得。
> 复现脚本见文末第 3 节。

---

## 一、连接策略：三道闸与一个不可达分支

### 1.1 判定逻辑

策略选择全在 `yoursql/execution/query.py` 的 `_choose_join_strategy()`（L1079），只有 20 行：

```python
if not pairs:
    return "nested_loop"
right_count = max(1, right_rows)
left_count = max(1, left_rows or right_count)
hash_cost = right_count * _JOIN_HASH_BUILD_COST + left_count * _JOIN_HASH_PROBE_COST
hash_fits = right_count * _JOIN_CONTEXT_BYTES <= _JOIN_HASH_MEMORY_BUDGET
nested_cost = left_count * right_count * _JOIN_NESTED_LOOP_PAIR_COST
if hash_fits and hash_cost <= nested_cost:
    return "hash"
if index_metadata is not None:
    index_cost = left_count * _JOIN_INDEX_LOOKUP_COST
    if index_cost < min(hash_cost if hash_fits else nested_cost, nested_cost):
        return "index"
return "nested_loop"
```

**左表 = `FROM` 的第一张表，右表 = `JOIN` 的那张表**（`left_rows = _row_count_of(statement.from_table)`，`right_rows = _row_count_of(join.table)`）。

代价模型：

```
hash   = 0.22·R + 0.05·L    (µs)   右表建哈希表，左表流式探测
nested = 10.4·L·R           (µs)   逐对合并 + 谓词
index  = 1400·L             (µs)   左表每行去右表索引等值查找
```

### 1.2 代价常数（全部由本机 TPC-H SF0.01 实测标定）

| 常量 | 值 | 代码里的标定依据 |
| --- | --- | --- |
| `_JOIN_HASH_BUILD_COST` | 0.22 µs/行 | 60,175 行建哈希表 13 ms |
| `_JOIN_HASH_PROBE_COST` | 0.05 µs/行 | 2,000 次探测 0.1 ms |
| `_JOIN_INDEX_LOOKUP_COST` | 1.4 ms/次 | 索引等值查找（2 万条目复合索引） |
| `_JOIN_NESTED_LOOP_PAIR_COST` | 10.4 µs/对 | 234 s / 2,250 万对 |
| `_JOIN_HASH_MEMORY_BUDGET` | 256 MB | 哈希建侧内存上限 |
| `_JOIN_CONTEXT_BYTES` | 464 B | 单行上下文实测值 |

### 1.3 实测（`stats["joins"]` 是引擎自报的实际策略）

| 场景 | 左表 × 右表 | 实际策略 |
| --- | --- | --- |
| 两表各 500 行 | 500 × 500 | `HashJoin` |
| 左极小、右极大、右表有索引 | 2 × 20000 | `HashJoin`（不是索引连接） |
| 逗号交叉连接（无等值键） | 2 × 500 | `NestedLoop` |
| 左右互换 | 20000 × 2 | `HashJoin` |

非等值连接条件（`ON o.customer_id < c.id`）也落到 `NestedLoop`。

### 1.4 关键发现：索引嵌套循环在常规规模不可达

把内部决策参数打出来看，代价算得没错，但结果不是索引连接：

```
决策输入: pairs=[('k','k')] right_rows=20000 left_rows=2 index=('k',)
  hash_cost=4.400ms index_cost=2.800ms → hash
```

原因是**闸的顺序**。做代数推导：

- `hash ≤ nested` ⟺ `0.22R + 0.05L ≤ 10.4·L·R`，对 L≥1、R≥1 **恒成立**
- 所以只要 `hash_fits` 为真，闸1 必返回 `hash`，**闸2（索引连接）永远到不了**

`hash_fits` 的边界是 `256 MB ÷ 464 B = 578,524 行`。把边界两侧喂进函数，翻转点吻合：

```
   左表L      右表R    hash(ms)   nested(ms)  index(ms)  hash_fits   选定
       2     578524     127.275     12033.3      2.800       True   hash
       2     578525     127.276     12033.3      2.800      False   index
       1    1000000     220.000     10400.0      1.400      False   index
     500    600000     132.025   3120000.0    700.000      False   index
```

**结论：`_index_join` 要在右表超过约 57.8 万行时才可能被自然选中。**
项目自身的回归测试也印证了这一点——`tests/test_join_strategies.py::test_index_nested_loop_is_chosen_when_hash_build_does_not_fit` 用 `monkeypatch` 把 `_JOIN_HASH_MEMORY_BUDGET` 压到 464 B 才触发它。

用同样手法实跑索引连接，确认它能工作且语义正确：

| 配置 | 实际策略 | 结果行数 |
| --- | --- | --- |
| 默认预算 | `HashJoin` | 2000 |
| 预算压到 464 B | `IndexNestedLoop` | 2000 |
| 预算压到 464 B + LEFT JOIN | `IndexNestedLoop` | 2000 |
| 两种策略结果集对比 | — | **完全一致** |

### 1.5 语义边界（准备答辩要记住）

- **NULL 键永不匹配**：建哈希表时 `if any(value is None or value is _MISSING for value in key): continue`，左表探测时同样跳过。
- **索引连接只支持 INNER / LEFT**（代码注释明说）：RIGHT / FULL 需要知道哪些右行没被匹配，只能交给哈希或嵌套循环。
- **残余谓词必须生效**：`ON o.customer_id = c.id AND o.oid > 101` 里只有等值部分进连接键，`o.oid > 101` 留作 `residual` 在合并后求值。
- **逗号连接也能推断键**：`FROM c, o WHERE c.id = o.customer_id` 会从 WHERE 里抽出等值键对走哈希连接，不是笛卡尔积；三层逗号连接每层只取"已就绪"的条件。
- **索引连接的前置条件**：右表索引的**前导列必须恰好等于连接键列**（`_join_index_metadata`），派生表 / 视图没有索引，只能走哈希或嵌套循环。

---

## 二、B+Tree：页分裂、借位与合并

实现全部在 `yoursql/storage/index.py`（1825 行），落盘格式 `MBIX` JSON 负载，`PAGE_TYPE=INDEX`。

### 2.1 节点结构

- 叶子页：`keys` / `row_ids` / `payloads`（覆盖索引 INCLUDE 列）三个平行数组 + `next_page` / `prev_page` 叶子链
- 内部页：`children` + `keys`，**`keys[i]` 是 `children[i]` 子树的最大键**，所以 n 个子页只有 n-1 个键
- `_node_fits`：`payload + INDEX_LINK_RESERVE(64B) ≤ page_size - HEADER_SIZE(30B)` 才算放得下
- `_node_underfull`：按**实际编码字节数**判断 `payload < 可用区一半`（变长键不能用固定条数描述占用率）

### 2.2 叶页分裂（实测）

拦截 `_split_leaf_and_propagate` 观察逐条插入 300 行的过程：

```
第 1 次: 溢出前 212 条 → 左 106 条 + 右 106 条
最终: 高度=2 叶子数=2 各叶键数=[106, 194]
```

由此得出：

- **单页容量约 211 条**（键 `'v00000'` 格式，约 19 B/条）
- **分裂点是严格中点**（212 // 2 = 106）——`_split_position()` 把候选位置按 `abs(position - middle)` 排序，从正中间向外试，取第一个"左右都放得下"的位置
- 右页随后从 106 增长到 194，所以 300 条只需 2 页；"106/194"不是分裂形态，而是分裂后的自然增长

分裂后的连锁动作：

1. 右页接管 `leaf.next_page`，左页指向右页，并修正原后继页的 `prev_page`（叶子链不断）
2. 父页 `children.insert(index+1, right.page_id)` → `_refresh_internal_keys` 重算分隔键
3. 父页放得下就收工并 `_refresh_ancestors`；放不下就 `_split_internal_and_propagate` 递归
4. 没有父页（就是根）→ `_create_root`，**新根用新页号，原页保留为左叶**

增长过程实测（键格式同上）：

| 行数 | 高度 | 总页 | 叶子页 | 内部页 | 根页 |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | 1 | 1 | 0 | 6 |
| 100 | 1 | 1 | 1 | 0 | 6 |
| 300 | 2 | 3 | 2 | 1 | 10 |
| 600 | 2 | 6 | 5 | 1 | 10 |
| 1200 | 2 | 12 | 11 | 1 | 10 |
| 2500 | 2 | 25 | 24 | 1 | 10 |

### 2.3 内部页分裂与教科书不同之处

教科书做法是"把中间键提升到父页"。这里**没有键提升**，而是：

```python
left_keys  = [self._load_max_key(child_id) for child_id in left_children[:-1]]
right_keys = [self._load_max_key(child_id) for child_id in right_children[:-1]]
```

切分点是按 `children` 数量在中点附近选，分隔键全部**从子页递归读回最大键重新计算**。好处是变长键也不会因为"中间键"选错而破坏不变量；代价是分裂时要多读几次子页。

### 2.4 删除：借位优先，合并兜底

删除链路的入口是 `_rebalance_empty(node)`（"修复低占用的非根页，先借位，无法借位时合并"）。拦截它的真实调用比例（插入 400 行后逐行删到 0）：

```
_rebalance_empty 被调用 83 次
  借位（页数不变）: 81 次
  合并（页数 -1） : 2 次
```

**借位是主力，合并是最后手段。** 两个关键约束：

- **不能把问题转移给兄弟**：借 1 条后若兄弟自己也变成低占用，就原样退回（`pop_entry` 后再 `insert_entry` 放回去）
- **合并优先并入左兄弟**：`left.extend_from(node)`，左兄弟装不下（`_node_fits` 为假）才尝试 `node.extend_from(right)`；成功合并则从父页删掉子页并调 `_repair_parent_after_removal` 递归向上

内部页的借位借的是**子页**而不是键，条件是兄弟至少还有 3 个子页（`len(left.children) > 2`），保证借完兄弟不会变空。

### 2.5 兜底路径：删除绝不失败

变长键可能出现"两个各自合法的页合并不下"的情况。代码的处理是**保留当前页，只更新父页分隔键**：

```python
# 变长键可能使两个合法页无法合并；保留当前页并更新边界，不能让删除失败。
self._write_node(node)
self._refresh_internal_keys(parent)
self._write_node(parent)
self._refresh_ancestors(parent.parent)
```

这是"稳健"验收点里最能讲的一处：宁可暂时保留一个低占用页，也不让 DELETE 报错。

收缩过程实测（数据结构与增长实验同一份）：

| 状态 | 高度 | 总页 | 叶子页 | 根页 |
| --- | --- | --- | --- | --- |
| 留 1200 行 | 2 | 12 | 11 | 10 |
| 留 300 行 | 2 | 3 | 2 | 10 |
| 留 60 行 | **1** | **1** | 1 | **6** |
| 留 5 行 | 1 | 1 | 1 | 6 |
| 全删 | 1 | 1 | 1 | 6 |

高度回落时**根页号从 10 回到最初的 6**——索引的根页贯穿整个生命周期，便于 Catalog 里的 `root_page_id` 稳定引用。

---

## 三、复现脚本

```bash
cd "F:/MyProject/SQL编译器"
PYTHONIOENCODING=utf-8 python - <<'EOF'
import os
from yoursql.engine.runtime.database import Database
from yoursql.storage.index import BPlusTree

# ---- 分裂追踪 ----
path = "/tmp/split_trace.db"
if os.path.exists(path): os.remove(path)
db = Database(path)
db.execute("CREATE TABLE t(id INT, v VARCHAR);")
db.execute("CREATE INDEX idx_v ON t(v);")

log = []
orig = BPlusTree._split_leaf_and_propagate
def spy(self, leaf):
    before = len(leaf.keys)
    orig(self, leaf)
    log.append((before, len(leaf.keys)))
BPlusTree._split_leaf_and_propagate = spy
for i in range(300):
    db.execute(f"INSERT INTO t VALUES ({i},'v{i:05d}');")
BPlusTree._split_leaf_and_propagate = orig
for k, (before, left) in enumerate(log, 1):
    print(f"第 {k} 次分裂: 溢出前 {before} → 左 {left} + 右 {before-left}")

snap = db.index_manager.get("idx_v").snapshot(limit=1)
print("最终高度:", snap["height"], "页数:", snap["page_count"])
db.close()
EOF
```

把 `_split_leaf_and_propagate` 换成 `_rebalance_empty` 即可统计借位与合并次数；把预算常量改成
`yoursql.execution.query._JOIN_HASH_MEMORY_BUDGET = 464` 即可强制触发索引嵌套循环。

# MiniSQL —— 小型数据库系统

> 中南大学《大型平台软件设计实习》项目：SQL 编译器 + 页式存储系统 + 小型数据库系统
> 语言：Python 3.10+（无第三方依赖）

一条完整链路：**SQL → Token → AST → 语义检查 → 逻辑计划 → 优化 → 执行引擎 → 数据页 / 磁盘**

---

## 1. 快速开始

```bash
cd F:\MyProject\SQL编译器

# 交互式 REPL
python -m database_system.cli.main --db data/mini.db

# 执行脚本文件
python -m database_system.cli.main --file database_system/demo.sql --plan

# 执行一条 SQL
python -m database_system.cli.main --sql "SELECT * FROM student;" --db data/mini.db

# 全链路输出演示（Token / AST / Plan / 优化前后对比 / 执行结果）
python -m database_system.cli.pipeline_demo

# 运行全部测试
python -m database_system.tests.run_all
```

交互模式内置命令：`.help` `.tables` `.schema <t>` `.plan on|off` `.optimize on|off`
`.tokens on|off` `.stats` `.log` `.policy LRU|FIFO` `.exit`

一个最小会话：

```text
MiniSQL> CREATE TABLE student(id INT, name VARCHAR(20), age INT);
table 'student' created (root_page=1)

MiniSQL> INSERT INTO student VALUES (1,'Alice',20),(2,'Bob',17);
2 row(s) inserted into 'student'

MiniSQL> SELECT name, age FROM student WHERE age > 10 + 8;
+-------+-----+
| name  | age |
+-------+-----+
| Alice |  20 |
+-------+-----+
1 row(s) returned

MiniSQL> .plan on
MiniSQL> EXPLAIN SELECT name FROM student WHERE age > 10 + 8;
--- 逻辑计划（优化前）---
Project [name]
Filter (age > (10 + 8))
SeqScan student [id, name, age]
--- 逻辑计划（优化后）---
Project [name]
Filter (age > 18)
SeqScan student [name, age]
生效的优化规则: 常量折叠 ConstantFolding; 投影裁剪 ProjectionPruning
```

---

## 2. 目录结构

```text
database_system/
|-- sql_compiler/           # SQL 编译器（编译原理）
|   |-- lexer.py            # 词法分析：Token 流 + 位置信息 + 词法错误
|   |-- parser.py           # 递归下降语法分析，与 grammar.md 逐条对应
|   |-- ast_nodes.py        # AST 节点定义与树形打印
|   |-- semantic.py         # 语义分析：名字绑定 + 类型规则表
|   |-- catalog.py          # 模式目录（符号表）：createTable/findTable/findColumn/getType
|   |-- planner.py          # AST -> 逻辑执行计划（SeqScan/Filter/Project/...）
|   `-- optimizer.py        # 规则式优化器（6 条规则）
|-- storage/                # 页式存储系统（操作系统）
|   |-- page.py             # 4KB 定长页 + 槽位目录（记录读写 / 删除 / 空间管理）
|   |-- file_manager.py     # 磁盘管理：页分配 / 释放 / 空闲页链表 / 元数据页
|   `-- buffer.py           # 缓冲池：LRU / FIFO 替换、命中统计、替换日志
|-- engine/                 # 数据库引擎
|   |-- record.py           # Row <-> Page 序列化（二进制编码）
|   |-- storage_engine.py   # 表堆 TableHeap：页链表、插入 / 迭代 / 删除 / 回收
|   |-- catalog_manager.py  # 系统目录作为特殊表持久化
|   |-- executor.py         # 火山模型执行器（SeqScan/Filter/Project/OrderBy/Limit...）
|   `-- database.py         # 门面：串起编译 -> 计划 -> 执行 -> 存储
|-- cli/
|   |-- main.py             # 命令行入口
|   `-- pipeline_demo.py    # 全链路输出演示
|-- tests/                  # test_sql / test_storage / test_db / test_fuzz + run_all
|-- utils/                  # 常量、错误类型、输出格式化
|-- grammar.md              # 文法与语义规则（阶段 0 交付物）
`-- demo.sql                # 端到端演示脚本
```

---

## 3. 各模块要点

### 3.1 词法分析（`lexer.py`）

* 输出 `[种别码, 词素值, 行号, 列号]`，种别码 ∈ {KEYWORD, IDENTIFIER, CONST, OPERATOR, DELIMITER, EOF}
* 关键字大小写不敏感；支持 `--` 与 `/* */` 注释；字符串 `''` 转义
* 多字符运算符 `>= <= != <>`；`<>` 归一化为 `!=`
* 非法字符、未闭合字符串 / 注释、非法数字（`1.5`、`12ab`）、整数越界 → `LexicalError`（带行列，不崩溃）

### 3.2 语法分析（`parser.py` + `grammar.md`）

* 递归下降，表达式优先级 **NOT > 比较 > AND > OR**，括号可改变优先级
* 支持 CREATE TABLE / INSERT（多行）/ SELECT / DELETE / UPDATE / DROP TABLE / EXPLAIN
* 语法错误输出：`SyntaxError at line L, column C: unexpected 'X', expected {A, B}`
* 缺分号、括号不匹配、结构错误均可精确定位

### 3.3 语义分析（`semantic.py` + `catalog.py`）

* 表 / 列存在性检查；标识符绑定到 `ColumnRef(表, 列, 序号, 类型)`
* 类型规则：`INT+INT→INT`、比较→`BOOL`、`AND/OR` 要求 BOOL、`NOT BOOL→BOOL`、`INT+VARCHAR→ERROR`
* INSERT 列数 / 列序 / 值类型检查；未指定列填 NULL（NOT NULL 且无默认值则报错）
* Catalog 即符号表：`createTable / findTable / findColumn / getType`

### 3.4 执行计划与优化（`planner.py` + `optimizer.py`）

* 转换规则：`FROM→SeqScan`、`WHERE→Filter`、`ORDER BY→OrderBy`（在 Project 之下，可引用未投影列）、
  `SELECT→Project`、`LIMIT→Limit`、`DELETE→Delete`
* Plan 三种输出形式：树形 `to_tree()`、JSON `to_json()`、S 表达式 `to_s_expr()`
* 6 条优化规则：常量折叠、布尔化简、谓词分解、谓词下推、冗余节点消除、投影裁剪；
  `EXPLAIN` 或 `.plan on` 可直接对比优化前后

### 3.5 页式存储（`page.py` / `file_manager.py` / `buffer.py`）

* 单页 4KB，页头 16B + 槽目录（每项 4B）+ 从页尾向下生长的记录区
* 磁盘文件 = 页数组；第 0 页为元数据页（页数 / 空闲链表头 / 目录根页）
* 页分配优先复用空闲链表；表扩展时申请新页挂到页链表尾部；`DROP TABLE` 回收全部页
* 缓冲池固定帧数，`get_page / unpin_page / flush_page / flush_all`，支持 **LRU / FIFO 双策略**，
  统计命中率、淘汰次数、磁盘读写，并记录每次替换日志（`.log` 查看）

### 3.6 执行引擎与持久化（`executor.py` / `storage_engine.py` / `catalog_manager.py`）

* 火山模型（open / next / close）：SeqScan 迭代页链表，Filter 三值逻辑过滤，Project 支持 DISTINCT
* 系统目录作为一张特殊表（页类型 CATALOG）与用户数据共用同一套槽位页与记录编码
* 所有表数据与元数据均通过页式存储落盘，程序重启后数据不丢失

---

## 4. 支持范围

| 类别 | 支持情况 |
|---|---|
| 语句 | CREATE TABLE / INSERT（多行）/ SELECT / DELETE / UPDATE / DROP TABLE / EXPLAIN |
| 查询 | `*`、列投影、别名、DISTINCT、WHERE、ORDER BY（多列 ASC/DESC）、LIMIT / OFFSET |
| 表达式 | 算术 `+ - * /`、比较 `= != <> > >= < <=`、AND / OR / NOT、`IS [NOT] NULL`、括号、一元正负 |
| 类型 | INT、VARCHAR(n)、BOOL、NULL |
| 约束 | PRIMARY KEY / NOT NULL / UNIQUE / DEFAULT（解析并登记；PK、UNIQUE 暂不强制唯一性） |
| 未支持 | JOIN、GROUP BY、聚合函数、子查询、索引、事务、并发控制 |

---

## 5. 测试

```bash
python -m database_system.tests.run_all
```

| 文件 | 覆盖内容 |
|---|---|
| `test_sql.py` | 词法（注释 / 转义 / 非法字符 / 非法数字 / 位置）、语法（四类语句 / 优先级 / 缺分号 / 括号）、语义（表列不存在 / 类型不匹配 / 列数）、计划与优化 |
| `test_storage.py` | 页的插入 / 删除 / 槽复用 / 页满、记录编解码、磁盘分配与空闲页复用、LRU 与 FIFO 淘汰差异、脏页回写、pin 约束 |
| `test_db.py` | 端到端 CRUD、持久化重启、多页 + 小缓冲池（500 行 / 3 帧）、四类错误的阶段归属、边界输入不崩溃 |
| `test_fuzz.py` | 合法 SQL 随机生成（不误拒）+ 字符级变异（不崩溃、不乱收）+ 随机字节输入 |

最近一次运行结果：**92 个用例全部通过**。

---

## 6. 已知限制

1. 单表查询，不支持 JOIN / 子查询 / 聚合。
2. PRIMARY KEY、UNIQUE 只做登记，不强制唯一性约束。
3. 删除为逻辑删除（清空槽目录项），空间在槽复用时回收，不做页内碎片整理。
4. 单行序列化后必须能放入一页（约 4KB），不支持跨页溢出（超长 VARCHAR 会报 ExecutionError）。
5. 单进程单线程，无事务、无 WAL、无崩溃恢复（仅在每次执行后 flush 保证落盘）。

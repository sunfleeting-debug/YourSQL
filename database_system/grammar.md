# MiniSQL 文法与语义规则（grammar.md）

> 阶段 0 交付物。本文法与 `sql_compiler/parser.py` 的实现**逐条对应**，
> 修改文法必须同步修改代码与本文。

## 1. 词法规则

| 类别 | 规则 |
|---|---|
| 关键字 | SELECT FROM WHERE CREATE TABLE INSERT INTO VALUES DELETE UPDATE SET DROP DISTINCT ORDER BY LIMIT OFFSET AS AND OR NOT NULL IS IN EXPLAIN IF EXISTS PRIMARY KEY UNIQUE DEFAULT ASC DESC TRUE FALSE INT VARCHAR BOOL BOOLEAN（**大小写不敏感**） |
| 标识符 | `[A-Za-z_][A-Za-z0-9_]*`，可为表名、列名、别名 |
| 整型常量 | `[0-9]+`，范围 INT32；不支持浮点（遇 `1.5` / `12ab` 报词法错误） |
| 字符串常量 | `'...'`，内部 `''` 表示一个字面单引号（如 `'Tom''s book'`）；未闭合报错 |
| 运算符 | `=  !=  <>  >  >=  <  <=  +  -  *  /`（`<>` 归一化为 `!=`） |
| 分隔符 | `( ) , ; .` |
| 空白 | 空格、制表、换行 |
| 注释 | `--` 单行；`/* ... */` 多行（未闭合报词法错误） |

Token 输出格式：`[种别码, 词素值, 行号, 列号]`，种别码 ∈
{KEYWORD, IDENTIFIER, CONST, OPERATOR, DELIMITER, EOF}。

## 2. 语法规则（EBNF）

```ebnf
program         = { statement } EOF ;

statement       = ( create_table_stmt
                  | insert_stmt
                  | select_stmt
                  | delete_stmt
                  | update_stmt
                  | drop_table_stmt ) ";" ;

explain_stmt    = "EXPLAIN" statement ;          (* 内层语句同样带 ';' *)

create_table_stmt
                = "CREATE" "TABLE" [ "IF" "NOT" "EXISTS" ] ident
                  "(" column_def { "," ( column_def | table_constraint ) } ")" ;

column_def      = ident data_type { column_constraint } ;
data_type       = "INT" | "VARCHAR" [ "(" int ")" ] | "BOOL" | "BOOLEAN" ;
column_constraint
                = "PRIMARY" "KEY" | "NOT" "NULL" | "NULL" | "UNIQUE"
                | "DEFAULT" literal ;
table_constraint
                = "PRIMARY" "KEY" "(" ident { "," ident } ")"
                | "UNIQUE" "(" ident { "," ident } ")" ;

insert_stmt     = "INSERT" "INTO" ident [ "(" ident { "," ident } ")" ]
                  "VALUES" value_tuple { "," value_tuple } ;
value_tuple     = "(" expr { "," expr } ")" ;

select_stmt     = "SELECT" [ "DISTINCT" ] select_item { "," select_item }
                  "FROM" table_ref
                  [ "WHERE" expr ]
                  [ "ORDER" "BY" order_item { "," order_item } ]
                  [ "LIMIT" int ] [ "OFFSET" int ] ;
select_item     = "*" | expr [ [ "AS" ] ident ] ;
table_ref       = ident [ [ "AS" ] ident ]
order_item      = expr [ "ASC" | "DESC" ] ;

delete_stmt     = "DELETE" "FROM" ident [ "WHERE" expr ] ;
update_stmt     = "UPDATE" ident "SET" ident "=" expr { "," ident "=" expr }
                  [ "WHERE" expr ] ;
drop_table_stmt = "DROP" "TABLE" [ "IF" "EXISTS" ] ident ;
```

### 2.1 表达式文法（优先级：NOT > 比较 > AND > OR）

```ebnf
expr            = or_expr ;
or_expr         = and_expr { "OR" and_expr } ;
and_expr        = not_expr { "AND" not_expr } ;
not_expr        = "NOT" not_expr | comparison_expr ;
comparison_expr = additive { ("=" | "!=" | "<>" | ">" | ">=" | "<" | "<=") not_expr
                            | "IS" [ "NOT" ] "NULL" } ;
additive        = multiplicative { ("+" | "-") multiplicative } ;
multiplicative  = unary { ("*" | "/") unary } ;
unary           = ("-" | "+") unary | primary ;
primary         = int | string | "TRUE" | "FALSE" | "NULL"
                | ident [ "." ident ] | "(" expr ")" ;
```

要点：

* `NOT` 在 `and_expr` 之下、`comparison` 之上，故 `NOT a = 1` 解析为 `NOT (a = 1)`。
* `a = 1 OR b = 2 AND c = 3` 解析为 `(a = 1) OR ((b = 2) AND (c = 3))`。

## 3. AST -> 逻辑执行计划 的转换规则

| AST 成分 | Plan 算子 | 说明 |
|---|---|---|
| `FROM t` | `SeqScan t [cols]` | 数据源；`cols` 为投影裁剪后真正需要读的列 |
| `WHERE e` | `Filter (e)` | 逐行过滤，NULL 视为不满足 |
| `ORDER BY k` | `OrderBy [k]` | 位于 Project 之下，可引用未投影的列 |
| `SELECT 列表` | `Project [items]`（可带 DISTINCT） | 求值投影 |
| `LIMIT n` | `Limit n` | 截断 |
| `INSERT` | `Insert t rows=N` | 记录转二进制写入数据页 |
| `DELETE` | `Delete t` | 按 rowid 定位并删除 |
| `UPDATE` | `Update t SET ...` | 定位后重写整行 |
| `CREATE TABLE` | `CreateTable t(cols)` | 建表并注册 Catalog |
| `DROP TABLE` | `DropTable t` | 删表并回收全部页 |

## 4. 语义规则

1. **表存在性**：`FROM/INSERT INTO/DELETE FROM/UPDATE` 引用的表必须在 Catalog 中。
2. **列存在性 + 名字绑定**：标识符绑定到 `ColumnRef(table, column, ordinal, type)`；
   限定符 `t.c` 必须匹配当前表名或别名。
3. **类型规则**

   | 运算 | 操作数 | 结果 |
   |---|---|---|
   | `+ - * /` | INT, INT | INT |
   | `= != > >= < <=` | 同类型（INT/VARCHAR/BOOL） | BOOL |
   | `AND OR` | BOOL, BOOL | BOOL |
   | `NOT` | BOOL | BOOL |
   | `IS [NOT] NULL` | 任意 | BOOL |
   | 其他组合（如 `INT + VARCHAR`） | — | **SemanticError** |

4. **WHERE 类型**：必须是 BOOL，否则报 `WHERE clause must be of type BOOL, got X`。
5. **INSERT**：列数必须与值个数一致；值必须是常量表达式；值类型必须可赋给目标列；
   未列出的列填 NULL（若该列 NOT NULL 且无默认值则报错）。
6. **NULL 语义**：三值逻辑，任意操作数为 NULL 时算术/比较结果为 NULL；
   `Filter` 只接受结果为 TRUE 的行。

## 5. 优化规则

| 规则 | 示例 |
|---|---|
| 常量折叠 | `age > 10 + 8` → `age > 18` |
| 布尔化简 | `x AND TRUE` → `x`；`NOT NOT x` → `x`；`x OR TRUE` → `TRUE` |
| 谓词分解 | `Filter(a AND b)` → `Filter(a)` 之下再 `Filter(b)` |
| 谓词下推 | `Filter(Project(X))` → `Project(Filter(X))` |
| 冗余节点消除 | `Filter(TRUE)` 删除；恒等 `Project` 删除 |
| 投影裁剪 | `SeqScan` 只读取真正需要的列（DELETE 只取 rowid） |

## 6. 错误类型与输出格式

```
<错误类型> at line <行>, column <列>: <原因说明>
```

* `LexicalError`  —— 非法字符、未闭合字符串/注释、非法数字
* `SyntaxError`   —— `unexpected <符号>, expected {<期望集合>}`
* `SemanticError` —— 表/列不存在、类型不匹配、列数不匹配
* `ExecutionError`—— 存储或执行期异常（如除零、行过大）
* `StorageError`  —— 页已满、缓冲区已满

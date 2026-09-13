# YourSQL Showcase 全功能演示

本文件与 [`showcase_init.sql`](./showcase_init.sql) 配套。脚本可以直接初始化一个紧凑的演示库；仓库交付的 `data/showcase_v2.db` 则由同目录的 Python 生成器创建为同一套结构、更多确定性数据和 4096B 页。

## 0. 创建数据库

建议先用 SQL 脚本验证完整初始化流程：

```powershell
python -m yoursql.cli --database showcase_sql.db --file examples/showcase_init.sql
```

查看大样本工作台数据库：

```powershell
python -m examples.create_showcase_db --force
python -m yoursql.web --database data/showcase_v2.db --host 127.0.0.1 --port 8080
```

生成器会保留这些数据库参数：`page_size=4096`、128 页 Buffer Pool。已存在数据库会从 superblock 自动识别页大小，因此不要用外部工具改写文件头。

## 1. 登录账号和权限

默认管理员是 `admin / admin`。初始化脚本还会创建：

| 用户 | 密码 | 能力 |
| --- | --- | --- |
| `analyst` | `analyst` | 全库只读查询、视图、EXPLAIN 和工作台流水线 |
| `auditor` | `auditor` | 全库只读查询 |
| `support` | `support` | customers/orders/support_tickets 查询，以及工单 INSERT/UPDATE |

在 CLI 中切换身份：

```powershell
python -m yoursql.cli --database showcase_sql.db --user analyst --password analyst --sql "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY n DESC"
```

## 2. 目录、模式、视图和权限

```sql
SHOW TABLES;
SHOW VIEWS;

DESC customers;
SHOW COLUMNS FROM products;
SHOW FIELDS IN orders;

SHOW INDEXES FROM orders;
SHOW INDEX FROM products;
SHOW CREATE TABLE order_items;
SHOW CREATE VIEW customer_order_totals;

SHOW GRANTS;
SHOW GRANTS FOR USER analyst;
SHOW GRANTS FOR ROLE support;
```

`SHOW CREATE`、`DESC`、`SHOW COLUMNS/FIELDS` 适合放到工作台的结果区观察目录信息；`SHOW INDEXES` 可以进一步跳转到存储面板查看索引页。

## 3. 基础查询、别名、NULL、过滤和分页

```sql
SELECT id, name, segment, city, credit_limit
FROM customers
WHERE active = TRUE
  AND segment IN ('enterprise', 'education')
ORDER BY credit_limit DESC NULLS LAST, id
LIMIT 20 OFFSET 1;

SELECT id, status, order_date, shipped_date, total,
       total * 1.06 AS with_tax,
       'order-' || id AS order_label
FROM orders
WHERE status BETWEEN 'paid' AND 'shipped'
  AND shipped_date IS NOT NULL
ORDER BY with_tax DESC
LIMIT 20;

SELECT id, subject, priority, status
FROM support_tickets
WHERE subject LIKE '%order%'
   OR body NOT LIKE '%missing%'
ORDER BY priority, id;

SELECT id, title, LENGTH(body) AS body_length,
       COALESCE(body, '[no body]') AS normalized_body,
       verified
FROM reviews
WHERE body IS NULL OR LENGTH(body) > 30
ORDER BY id;
```

还可以观察三值逻辑、负数和 `NOT`：

```sql
SELECT id, name, credit_limit
FROM customers
WHERE NOT (active = FALSE)
  AND credit_limit NOT BETWEEN 0 AND 3000
ORDER BY id;
```

## 4. 函数、聚合、GROUP BY 和 HAVING

```sql
SELECT status, channel,
       count(*) AS order_count,
       count(DISTINCT customer_id) AS customers,
       sum(total) AS gross_total,
       avg(total) AS average_total,
       min(total) AS minimum_total,
       max(total) AS maximum_total
FROM orders
GROUP BY status, channel
HAVING count(*) >= 1
ORDER BY gross_total DESC;

SELECT category,
       count(*) AS products,
       avg(price) AS average_price,
       min(price) AS cheapest,
       max(price) AS most_expensive
FROM products
GROUP BY category
HAVING avg(price) > 50
ORDER BY average_price DESC;

SELECT DATE('2024-01-31', '+1 month') AS next_month,
       DATE('2024-03-31', '-90 day') AS ninety_days_before,
       DATE('2024-02-29', '+1 year') AS normalized_leap_day,
       UPPER('yoursql') AS product_name,
       LOWER('WORKBENCH') AS surface_name,
       ABS(-42) AS absolute_value;
```

## 5. 索引扫描和 EXPLAIN

以下查询包含高选择率或联合索引条件。结果区的执行统计可观察 `IndexScan`；选择率过低时优化器可能主动选择 SeqScan，这也是值得观察的计划决策。`EXPLAIN` 的计划字符串会送到流水线面板：

```sql
SELECT id, customer_id, status, total
FROM orders
WHERE customer_id = 1
ORDER BY total DESC
LIMIT 50;

SELECT id, customer_id, status
FROM orders
WHERE customer_id = 1 AND status IN ('paid', 'shipped')
ORDER BY id;

SELECT id, sku, name, category, price
FROM products
WHERE category BETWEEN 'compute' AND 'storage'
ORDER BY price DESC;

EXPLAIN SELECT order_id, count(*) AS lines, sum(line_total) AS amount
FROM order_items
WHERE order_id = 1
GROUP BY order_id;
```

## 6. JOIN、LEFT/RIGHT/FULL 和逗号交叉连接

```sql
SELECT o.id AS order_id, c.name AS customer_name,
       w.code AS warehouse_code, o.status, o.total
FROM orders AS o
JOIN customers AS c ON o.customer_id = c.id
JOIN warehouses AS w ON o.warehouse_id = w.id
WHERE o.total > 200
ORDER BY o.total DESC
LIMIT 20;

SELECT c.id, c.name, o.id AS order_id, o.total
FROM customers AS c
LEFT JOIN orders AS o ON c.id = o.customer_id
ORDER BY c.id, o.id;

SELECT c.id, c.name, o.id AS order_id
FROM customers AS c
RIGHT JOIN orders AS o ON c.id = o.customer_id
WHERE o.status = 'pending'
ORDER BY o.id;

SELECT d.region, d.name AS department, w.code AS warehouse
FROM departments AS d
FULL OUTER JOIN warehouses AS w ON d.region = w.region
ORDER BY d.region, w.code;

SELECT d.code, w.code AS warehouse_code
FROM departments AS d, warehouses AS w
WHERE d.id = 1 AND w.id <= 3
ORDER BY w.id;
```

## 7. 子查询、IN、UNION 和 DISTINCT

```sql
SELECT id, code
FROM warehouses
WHERE id IN (SELECT warehouse_id FROM orders WHERE status = 'cancelled')
ORDER BY id;

SELECT id, name
FROM customers
WHERE id NOT IN (SELECT customer_id FROM orders WHERE status = 'cancelled')
ORDER BY id;

SELECT DISTINCT category
FROM products
ORDER BY category;

SELECT id, event_type AS activity
FROM events
WHERE event_type = 'login'
LIMIT 5
UNION ALL
SELECT id, event_type AS activity
FROM events
WHERE event_type = 'checkout'
LIMIT 5;

SELECT id, name
FROM customers
WHERE city = '上海'
UNION
SELECT id, name
FROM customers
WHERE segment = 'enterprise'
ORDER BY id;
```

## 8. 视图查询

```sql
SELECT *
FROM active_customers
WHERE credit_limit > 6000
ORDER BY credit_limit DESC;

SELECT status, order_count, gross_total
FROM order_status_summary
ORDER BY gross_total DESC;

SELECT customer_id, customer_name, order_count,
       COALESCE(gross_total, 0) AS gross_total
FROM customer_order_totals
WHERE order_count >= 2
ORDER BY gross_total DESC;
```

视图是只读目录对象。可以执行 `DROP VIEW IF EXISTS`、`CREATE VIEW IF NOT EXISTS` 观察生命周期，但不要在主 `data/showcase_v2.db` 上删除正式视图。

## 9. INSERT、UPDATE、DELETE 和约束

下面的脚本演示工单的写入、更新、查询和删除。写语句完成后立即持久化，建议在 `showcase_sql.db` 副本中执行：

```sql
INSERT INTO support_tickets
VALUES (900001, 1, NULL, 'high', 'open', '2025-04-01', NULL,
        'Workbench write demo',
        'This row is used to inspect direct page updates.');
UPDATE support_tickets
SET status = 'pending', priority = 'urgent'
WHERE id = 900001;
SELECT id, priority, status FROM support_tickets WHERE id = 900001;
DELETE FROM support_tickets WHERE id = 900001;
```

也可以用部分列插入观察默认值和约束：

```sql
INSERT INTO support_tickets (id, customer_id, opened_at, subject)
VALUES (900002, 2, '2025-04-02', 'Default value ticket');
SELECT id, priority, status FROM support_tickets WHERE id = 900002;
```

## 10. DDL 和索引生命周期

这些语句建议在 `showcase_sql.db` 副本中执行：

```sql
CREATE TABLE demo_scratch(
    id INT PRIMARY KEY,
    label VARCHAR NOT NULL,
    score FLOAT DEFAULT 0.0,
    enabled BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS demo_scratch(
    id INT PRIMARY KEY,
    label VARCHAR NOT NULL,
    score FLOAT DEFAULT 0.0,
    enabled BOOLEAN DEFAULT TRUE
);

INSERT INTO demo_scratch (id, label)
VALUES (1, 'first row'), (2, 'second row');

CREATE INDEX IF NOT EXISTS idx_demo_scratch_score ON demo_scratch (score);
SHOW CREATE TABLE demo_scratch;
DROP INDEX IF EXISTS idx_demo_scratch_score;
DROP TABLE IF EXISTS demo_scratch;
DROP VIEW IF EXISTS missing_view;
```

## 11. RBAC、GRANT、REVOKE 和错误路径

在副本中创建一个临时角色，观察权限目录变化：

```sql
CREATE ROLE demo_reader;
CREATE USER demo_user IDENTIFIED BY 'demo_password' DEFAULT ROLE demo_reader;
GRANT SELECT ON customers TO ROLE demo_reader;
SHOW GRANTS FOR ROLE demo_reader;
SHOW GRANTS FOR USER demo_user;
REVOKE SELECT ON customers FROM ROLE demo_reader;
```

退出管理员后使用 `support / support` 登录并运行：

```sql
SELECT id, subject, status FROM support_tickets LIMIT 5;
INSERT INTO support_tickets (id, customer_id, opened_at, subject)
VALUES (900003, 3, '2025-04-03', 'Support can insert');
SELECT * FROM products LIMIT 5;
```

最后一条应返回权限错误。管理员账号还可以演示绑定错误和唯一约束错误：

```sql
SELECT missing_column FROM customers;
INSERT INTO departments VALUES (999, 'D001', 'Duplicate', '华东', 1.0, TRUE);
```

## 12. 工作台流水线、存储页、Buffer Pool 和索引

1. 执行任一 `SELECT`，在结果区切换“流水线”，查看 Token、AST、逻辑/物理阶段、算子统计和实际计划。
2. 切换“存储”，打开页面地图，选择 `orders`、`events` 或 `support_tickets` 的 HEAP 页。
3. 在 4096B 数据库中观察 30B 页头、双向槽目录、记录偏移、空闲区以及原始 Hex/ASCII。
4. 点开索引节点查看 `idx_orders_status`、联合索引和叶子页；在 Buffer Pool 面板观察当前状态。
5. 运行写入脚本后重新查看页面，比较 INSERT/UPDATE/DELETE 对槽位和页面内容的影响。

CLI 也可以输出结构化结果：

```powershell
python -m yoursql.cli --database data/showcase_v2.db --json --sql "EXPLAIN SELECT * FROM orders WHERE customer_id = 1 LIMIT 5"
```

## 13. 重建和检查页大小

```powershell
python -m examples.create_showcase_db --force
python -m yoursql.cli --database data/showcase_v2.db --sql "SHOW TABLES; SHOW VIEWS; SHOW INDEXES FROM orders;"
```

用 Python 只读检查 superblock 识别出的页大小：

```powershell
python -c "from yoursql.engine.database import Database; print(Database.detect_page_size('data/showcase_v2.db'))"
```

预期输出为 `4096`。生成器使用确定性数据和分批导入，适合反复查看页分配、索引和页面布局；初始化 SQL 则适合快速建立一个无需等待大批量导入的完整功能样本。

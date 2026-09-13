-- YourSQL Showcase 完整初始化脚本
--
-- 面向空数据库执行，包含 9 张业务表、代表性数据、索引、视图和 RBAC。
-- 页面大小属于数据库文件参数，不由 SQL 设置；
-- examples/create_showcase_db.py 使用 4096B 页创建大样本 data/showcase_v2.db。

CREATE TABLE departments(
    id INT PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    name VARCHAR(80) NOT NULL,
    region VARCHAR(20) NOT NULL,
    budget FLOAT DEFAULT 0.0,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE warehouses(
    id INT PRIMARY KEY,
    code VARCHAR(16) UNIQUE NOT NULL,
    city VARCHAR(40) NOT NULL,
    region VARCHAR(20) NOT NULL,
    capacity INT DEFAULT 0,
    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE customers(
    id INT PRIMARY KEY,
    name VARCHAR(80) NOT NULL,
    email VARCHAR(120) UNIQUE NOT NULL,
    segment VARCHAR(30),
    city VARCHAR(40),
    signup_date VARCHAR(10),
    credit_limit FLOAT DEFAULT 0.0,
    active BOOLEAN DEFAULT TRUE,
    notes VARCHAR
);

CREATE TABLE products(
    id INT PRIMARY KEY,
    sku VARCHAR(32) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    category VARCHAR(40),
    price FLOAT NOT NULL,
    cost FLOAT,
    stock INT DEFAULT 0,
    rating FLOAT,
    discontinued BOOLEAN DEFAULT FALSE,
    description VARCHAR
);

CREATE TABLE orders(
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    warehouse_id INT,
    status VARCHAR(20) DEFAULT 'pending',
    order_date VARCHAR(10) NOT NULL,
    shipped_date VARCHAR(10),
    total FLOAT DEFAULT 0.0,
    priority INT DEFAULT 3,
    channel VARCHAR(20),
    notes VARCHAR
);

CREATE TABLE order_items(
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT DEFAULT 1,
    unit_price FLOAT NOT NULL,
    discount FLOAT DEFAULT 0.0,
    line_total FLOAT NOT NULL,
    fulfilled BOOLEAN DEFAULT FALSE,
    serial_note VARCHAR
);

CREATE TABLE support_tickets(
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    order_id INT,
    priority VARCHAR(20) DEFAULT 'normal',
    status VARCHAR(20) DEFAULT 'open',
    opened_at VARCHAR(10) NOT NULL,
    resolved_at VARCHAR(10),
    subject VARCHAR(160) NOT NULL,
    body VARCHAR
);

CREATE TABLE events(
    id INT PRIMARY KEY,
    customer_id INT,
    event_type VARCHAR(30) NOT NULL,
    event_date VARCHAR(10) NOT NULL,
    source VARCHAR(20),
    payload VARCHAR,
    success BOOLEAN DEFAULT TRUE,
    latency_ms INT DEFAULT 0
);

CREATE TABLE reviews(
    id INT PRIMARY KEY,
    product_id INT NOT NULL,
    customer_id INT NOT NULL,
    rating INT NOT NULL,
    title VARCHAR(120) NOT NULL,
    body VARCHAR,
    verified BOOLEAN DEFAULT FALSE,
    created_at VARCHAR(10) NOT NULL
);

-- 代表性种子数据覆盖 NULL、默认值、低基数索引、长文本和多种 JOIN。
INSERT INTO departments VALUES
    (1, 'D001', 'Platform', '华东', 125000.0, TRUE),
    (2, 'D002', 'Analytics', '华南', 98000.5, TRUE),
    (3, 'D003', 'Security', '华北', 156000.0, TRUE),
    (4, 'D004', 'Operations', '西南', 87000.0, TRUE),
    (5, 'D005', 'Training', '西北', 72000.0, FALSE),
    (6, 'D006', 'Research', '华东', 210000.0, TRUE);

INSERT INTO warehouses VALUES
    (1, 'WH-001', '上海', '华东', 12000, TRUE),
    (2, 'WH-002', '深圳', '华南', 14000, TRUE),
    (3, 'WH-003', '北京', '华北', 16000, TRUE),
    (4, 'WH-004', '成都', '西南', 11000, TRUE),
    (5, 'WH-005', '西安', '西北', 9000, FALSE),
    (6, 'WH-006', '杭州', '华东', 17500, TRUE),
    (7, 'WH-007', '武汉', '华中', 10000, TRUE),
    (8, 'WH-008', '南京', '华东', 13000, TRUE);

INSERT INTO customers VALUES
    (1, 'Alice Chen', 'alice@example.test', 'enterprise', '上海', '2024-01-12', 12000.0, TRUE, 'priority account'),
    (2, 'Bob Wang', 'bob@example.test', 'smb', '深圳', '2024-02-18', 5000.0, TRUE, NULL),
    (3, 'Carol Liu', 'carol@example.test', 'consumer', '北京', '2024-03-03', 2500.0, TRUE, 'prefers email'),
    (4, 'David Zhou', 'david@example.test', 'education', '成都', '2024-03-21', 8000.0, FALSE, 'account paused'),
    (5, 'Eva Sun', 'eva@example.test', 'public', '西安', '2024-04-09', 6500.0, TRUE, NULL),
    (6, 'Frank Hu', 'frank@example.test', 'enterprise', '杭州', '2024-05-17', 18000.0, TRUE, 'quarterly billing'),
    (7, 'Grace Xu', 'grace@example.test', 'smb', '武汉', '2024-06-02', 4500.0, TRUE, NULL),
    (8, 'Henry Tang', 'henry@example.test', 'consumer', '南京', '2024-06-28', 3000.0, TRUE, 'new customer'),
    (9, 'Iris Gao', 'iris@example.test', 'education', '上海', '2024-07-15', 7000.0, TRUE, NULL),
    (10, 'Jason He', 'jason@example.test', 'public', '深圳', '2024-08-11', 9500.0, TRUE, 'contract renewal'),
    (11, 'Kiki Fan', 'kiki@example.test', 'enterprise', '北京', '2024-09-05', 16000.0, TRUE, NULL),
    (12, 'Leo Qin', 'leo@example.test', 'smb', '广州', '2024-10-19', 4200.0, FALSE, NULL);

INSERT INTO products VALUES
    (1, 'SKU-000001', 'Object Storage Basic', 'storage', 49.5, 25.0, 1200, 4.8, FALSE, 'Durable object storage for backups.'),
    (2, 'SKU-000002', 'Object Storage Pro', 'storage', 129.0, 65.0, 800, 4.6, FALSE, 'Higher throughput storage tier.'),
    (3, 'SKU-000003', 'Compute Small', 'compute', 88.0, 47.0, 430, 4.1, FALSE, 'Small virtual machine instance.'),
    (4, 'SKU-000004', 'Compute Large', 'compute', 399.0, 210.0, 160, 4.7, FALSE, 'Large compute instance for batch jobs.'),
    (5, 'SKU-000005', 'Edge Router', 'network', 219.0, 120.0, 250, 3.9, FALSE, 'Programmable edge routing appliance.'),
    (6, 'SKU-000006', 'Secure Gateway', 'security', 599.0, 310.0, 75, 4.9, FALSE, 'Gateway with policy and audit support.'),
    (7, 'SKU-000007', 'Metrics Board', 'analytics', 159.0, 82.0, 310, 4.3, FALSE, 'Operational metrics dashboard license.'),
    (8, 'SKU-000008', 'Legacy Display', 'display', 39.0, 20.0, 0, 3.1, TRUE, 'Legacy item kept for NULL and filter demos.'),
    (9, 'SKU-000009', 'Mobile Client', 'mobile', 29.0, 12.0, 900, 4.0, FALSE, 'Mobile client subscription.'),
    (10, 'SKU-000010', 'Audio Monitor', 'audio', 79.0, 42.0, 180, 3.8, FALSE, 'Audio monitoring extension.'),
    (11, 'SKU-000011', 'Keyboard', 'accessory', 59.0, 31.0, 600, 4.2, FALSE, 'Mechanical keyboard for operators.'),
    (12, 'SKU-000012', 'Incident Training', 'training', 299.0, 140.0, 90, 4.5, FALSE, 'Incident response workshop.'),
    (13, 'SKU-000013', 'Archive Service', 'service', 19.5, 8.0, 2000, 4.4, FALSE, 'Cold archive service.'),
    (14, 'SKU-000014', 'Query Lab', 'analytics', 249.0, 130.0, 130, 4.6, FALSE, 'SQL performance lab license.'),
    (15, 'SKU-000015', 'Policy Pack', 'security', 179.0, 91.0, 220, 4.7, FALSE, 'Reusable security policy pack.'),
    (16, 'SKU-000016', 'Retired Adapter', 'accessory', 15.0, 7.0, 0, 2.7, TRUE, 'Discontinued adapter.');

INSERT INTO orders VALUES
    (1, 1, 1, 'paid', '2025-01-03', '2025-01-04', 178.5, 1, 'web', 'priority shipment'),
    (2, 2, 2, 'shipped', '2025-01-05', '2025-01-07', 399.0, 2, 'mobile', NULL),
    (3, 3, 3, 'pending', '2025-01-08', NULL, 88.0, 3, 'web', 'awaiting approval'),
    (4, 4, 4, 'cancelled', '2025-01-11', NULL, 599.0, 4, 'partner', 'budget rejected'),
    (5, 5, 5, 'paid', '2025-01-13', '2025-01-14', 129.0, 2, 'sales', NULL),
    (6, 6, 6, 'shipped', '2025-01-16', '2025-01-18', 818.0, 1, 'web', NULL),
    (7, 7, 7, 'pending', '2025-01-19', NULL, 159.0, 5, 'mobile', NULL),
    (8, 8, 8, 'paid', '2025-01-22', '2025-01-23', 78.0, 3, 'web', 'small order'),
    (9, 9, 1, 'shipped', '2025-01-25', '2025-01-27', 599.0, 1, 'partner', NULL),
    (10, 10, 2, 'cancelled', '2025-01-28', NULL, 249.0, 4, 'sales', 'customer changed plan'),
    (11, 11, 3, 'paid', '2025-02-02', '2025-02-03', 478.0, 2, 'web', NULL),
    (12, 12, 4, 'pending', '2025-02-04', NULL, 29.0, 5, 'mobile', NULL),
    (13, 1, 6, 'shipped', '2025-02-07', '2025-02-09', 648.0, 1, 'web', 'renewal'),
    (14, 2, 1, 'paid', '2025-02-11', '2025-02-12', 159.0, 3, 'partner', NULL),
    (15, 3, 2, 'pending', '2025-02-14', NULL, 179.0, 2, 'sales', NULL),
    (16, 5, 3, 'shipped', '2025-02-17', '2025-02-19', 249.0, 1, 'web', NULL),
    (17, 6, 4, 'paid', '2025-02-21', '2025-02-22', 299.0, 2, 'mobile', NULL),
    (18, 7, 5, 'cancelled', '2025-02-24', NULL, 39.0, 4, 'partner', 'out of stock'),
    (19, 8, 6, 'pending', '2025-02-27', NULL, 219.0, 5, 'web', NULL),
    (20, 9, 7, 'shipped', '2025-03-01', '2025-03-02', 399.0, 1, 'sales', NULL),
    (21, 10, 8, 'paid', '2025-03-04', '2025-03-05', 129.0, 3, 'web', NULL),
    (22, 11, 1, 'pending', '2025-03-07', NULL, 49.5, 2, 'mobile', NULL),
    (23, 1, 2, 'shipped', '2025-03-10', '2025-03-12', 777.0, 1, 'partner', 'multi-product'),
    (24, 6, 3, 'paid', '2025-03-13', '2025-03-14', 358.0, 2, 'web', NULL);

INSERT INTO order_items VALUES
    (1, 1, 1, 1, 49.5, 0.0, 49.5, TRUE, NULL),
    (2, 1, 2, 1, 129.0, 0.0, 129.0, TRUE, NULL),
    (3, 2, 4, 1, 399.0, 0.0, 399.0, TRUE, 'serial-batch-02'),
    (4, 3, 3, 1, 88.0, 0.0, 88.0, FALSE, NULL),
    (5, 4, 6, 1, 599.0, 0.0, 599.0, FALSE, NULL),
    (6, 5, 2, 1, 129.0, 0.0, 129.0, TRUE, NULL),
    (7, 6, 4, 1, 399.0, 0.0, 399.0, TRUE, NULL),
    (8, 6, 5, 2, 219.0, 0.0, 438.0, TRUE, NULL),
    (9, 7, 7, 1, 159.0, 0.0, 159.0, FALSE, NULL),
    (10, 8, 8, 2, 39.0, 0.0, 78.0, TRUE, NULL),
    (11, 9, 6, 1, 599.0, 0.0, 599.0, TRUE, NULL),
    (12, 10, 14, 1, 249.0, 0.0, 249.0, FALSE, NULL),
    (13, 11, 5, 1, 219.0, 0.0, 219.0, TRUE, NULL),
    (14, 11, 15, 1, 179.0, 0.0, 179.0, TRUE, NULL),
    (15, 11, 9, 1, 29.0, 0.0, 29.0, TRUE, NULL),
    (16, 12, 9, 1, 29.0, 0.0, 29.0, FALSE, NULL),
    (17, 13, 2, 1, 129.0, 0.0, 129.0, TRUE, NULL),
    (18, 13, 6, 1, 599.0, 0.0, 599.0, TRUE, NULL),
    (19, 14, 7, 1, 159.0, 0.0, 159.0, TRUE, NULL),
    (20, 15, 15, 1, 179.0, 0.0, 179.0, FALSE, NULL),
    (21, 16, 14, 1, 249.0, 0.0, 249.0, TRUE, NULL),
    (22, 17, 12, 1, 299.0, 0.0, 299.0, TRUE, 'serial-batch-17'),
    (23, 18, 8, 1, 39.0, 0.0, 39.0, FALSE, NULL),
    (24, 19, 5, 1, 219.0, 0.0, 219.0, FALSE, NULL),
    (25, 20, 4, 1, 399.0, 0.0, 399.0, TRUE, NULL),
    (26, 21, 2, 1, 129.0, 0.0, 129.0, TRUE, NULL),
    (27, 22, 1, 1, 49.5, 0.0, 49.5, FALSE, NULL),
    (28, 23, 6, 1, 599.0, 0.0, 599.0, TRUE, NULL),
    (29, 23, 4, 1, 399.0, 0.05, 379.05, TRUE, NULL),
    (30, 23, 5, 1, 219.0, 0.0, 219.0, TRUE, NULL),
    (31, 24, 15, 1, 179.0, 0.0, 179.0, TRUE, NULL),
    (32, 24, 12, 1, 299.0, 0.0, 299.0, TRUE, NULL);

INSERT INTO support_tickets VALUES
    (1, 1, 3, 'high', 'open', '2025-01-09', NULL, 'Approval is still pending', 'Customer cannot complete the approval workflow.'),
    (2, 2, 2, 'normal', 'resolved', '2025-01-10', '2025-01-11', 'Mobile order status', 'The status was refreshed after a delayed callback.'),
    (3, 3, NULL, 'low', 'closed', '2025-01-12', '2025-01-13', 'How to export data', NULL),
    (4, 4, 4, 'urgent', 'open', '2025-01-14', NULL, 'Cancellation confirmation', 'Please confirm the cancellation and release reserved capacity.'),
    (5, 5, 5, 'normal', 'pending', '2025-01-15', NULL, 'Invoice address', 'Invoice address needs to be updated.'),
    (6, 6, 6, 'high', 'resolved', '2025-01-18', '2025-01-20', 'Gateway timeout', 'Timeout reproduced once during a high latency window.'),
    (7, 7, NULL, 'low', 'closed', '2025-01-20', '2025-01-21', 'Training material', NULL),
    (8, 8, 8, 'normal', 'open', '2025-01-23', NULL, 'Display replacement', 'Customer reported a reproducible issue with the retired display.'),
    (9, 9, 9, 'urgent', 'pending', '2025-01-26', NULL, 'Security review', 'Security review is waiting for an assigned operator.'),
    (10, 10, 10, 'normal', 'resolved', '2025-01-29', '2025-01-30', 'Plan change', 'Customer changed the plan after the order was cancelled.'),
    (11, 11, 13, 'high', 'closed', '2025-02-04', '2025-02-06', 'Renewal receipt', NULL),
    (12, 12, NULL, 'low', 'open', '2025-02-05', NULL, 'New account question', 'Customer asked for help with the first login.');

INSERT INTO events VALUES
    (1, 1, 'login', '2025-01-03', 'web', '{"event":"login","attempt":1}', TRUE, 32),
    (2, 1, 'search', '2025-01-03', 'web', '{"event":"search","attempt":1}', TRUE, 48),
    (3, 2, 'view_product', '2025-01-05', 'mobile', '{"event":"view_product","attempt":1}', TRUE, 77),
    (4, 2, 'checkout', '2025-01-05', 'mobile', '{"event":"checkout","attempt":1}', TRUE, 120),
    (5, 3, 'payment', '2025-01-08', 'web', '{"event":"payment","attempt":2}', FALSE, 1800),
    (6, 4, 'login', '2025-01-11', 'api', '{"event":"login","attempt":1}', TRUE, 25),
    (7, 5, 'add_cart', '2025-01-13', 'web', '{"event":"add_cart","attempt":1}', TRUE, 65),
    (8, 6, 'checkout', '2025-01-16', 'web', '{"event":"checkout","attempt":1}', TRUE, 95),
    (9, 7, 'logout', '2025-01-19', 'ios', '{"event":"logout","attempt":1}', TRUE, 21),
    (10, 8, 'view_product', '2025-01-22', 'android', '{"event":"view_product","attempt":1}', TRUE, 84),
    (11, 9, 'payment', '2025-01-25', 'api', '{"event":"payment","attempt":1}', TRUE, 210),
    (12, 10, 'search', '2025-01-28', 'web', '{"event":"search","attempt":2}', TRUE, 41),
    (13, NULL, 'login', '2025-01-30', 'batch', '{"event":"login","attempt":1}', TRUE, 17),
    (14, 11, 'checkout', '2025-02-02', 'web', '{"event":"checkout","attempt":1}', TRUE, 133),
    (15, 12, 'login', '2025-02-04', 'mobile', '{"event":"login","attempt":1}', FALSE, 900),
    (16, 1, 'logout', '2025-02-05', 'web', '{"event":"logout","attempt":1}', TRUE, 20),
    (17, 2, 'search', '2025-02-06', 'ios', '{"event":"search","attempt":1}', TRUE, 51),
    (18, 3, 'view_product', '2025-02-07', 'android', '{"event":"view_product","attempt":1}', TRUE, 88),
    (19, 5, 'add_cart', '2025-02-08', 'web', '{"event":"add_cart","attempt":1}', TRUE, 73),
    (20, 6, 'payment', '2025-02-09', 'api', '{"event":"payment","attempt":1}', TRUE, 156),
    (21, 7, 'login', '2025-02-10', 'web', '{"event":"login","attempt":1}', TRUE, 29),
    (22, 8, 'checkout', '2025-02-11', 'mobile', '{"event":"checkout","attempt":2}', FALSE, 1300),
    (23, 9, 'logout', '2025-02-12', 'web', '{"event":"logout","attempt":1}', TRUE, 19),
    (24, 10, 'search', '2025-02-13', 'batch', '{"event":"search","attempt":1}', TRUE, 62);

INSERT INTO reviews VALUES
    (1, 1, 1, 5, 'Reliable storage', 'The product matched the documented workload.', TRUE, '2025-01-06'),
    (2, 2, 2, 4, 'Good throughput', 'Useful for larger backup jobs.', TRUE, '2025-01-08'),
    (3, 3, 3, 4, 'Stable compute', NULL, FALSE, '2025-01-10'),
    (4, 4, 4, 5, 'Fast batch jobs', 'The larger instance remained stable under load.', TRUE, '2025-01-12'),
    (5, 5, 5, 3, 'Needs tuning', 'Routing defaults need a little tuning.', FALSE, '2025-01-15'),
    (6, 6, 6, 5, 'Strong gateway', 'Policy and audit support are both useful.', TRUE, '2025-01-19'),
    (7, 7, 7, 4, 'Clear metrics', NULL, FALSE, '2025-01-22'),
    (8, 8, 8, 2, 'Legacy device', 'The old display is usable but clearly dated.', FALSE, '2025-01-24'),
    (9, 9, 9, 4, 'Good client', 'The mobile client is easy to deploy.', TRUE, '2025-01-27'),
    (10, 12, 10, 5, 'Useful training', 'The workshop was practical and well structured.', TRUE, '2025-02-03'),
    (11, 14, 11, 4, 'Helpful lab', 'Useful text for LIKE, LENGTH and NULL examples.', TRUE, '2025-02-06'),
    (12, 16, 12, 1, 'Retired', NULL, FALSE, '2025-02-08');

-- 索引生命周期、低基数扫描和联合索引演示。
CREATE UNIQUE INDEX uq_departments_code ON departments (code);
CREATE UNIQUE INDEX uq_warehouses_code ON warehouses (code);
CREATE UNIQUE INDEX uq_customers_email ON customers (email);
CREATE UNIQUE INDEX uq_products_sku ON products (sku);
CREATE INDEX idx_orders_status ON orders (status);
CREATE INDEX idx_orders_customer_status ON orders (customer_id, status);
CREATE INDEX idx_products_category ON products (category);
CREATE INDEX idx_tickets_priority ON support_tickets (priority);
CREATE INDEX idx_events_type ON events (event_type);
CREATE INDEX idx_reviews_rating ON reviews (rating);
CREATE INDEX idx_order_items_order_product ON order_items (order_id, product_id);

-- 视图只保存定义，查询时重新执行底层 SELECT，因此天然只读。
CREATE VIEW active_customers AS
    SELECT id, name, segment, city, credit_limit
    FROM customers
    WHERE active = TRUE;

CREATE VIEW order_status_summary AS
    SELECT status, count(*) AS order_count, sum(total) AS gross_total, avg(total) AS average_total
    FROM orders
    GROUP BY status;

CREATE VIEW customer_order_totals AS
    SELECT c.id AS customer_id, c.name AS customer_name,
           count(o.id) AS order_count, sum(o.total) AS gross_total
    FROM customers AS c
    LEFT JOIN orders AS o ON c.id = o.customer_id
    GROUP BY c.id, c.name;

-- RBAC：管理员仍由 YourSQL 自动创建为 admin/admin。
CREATE ROLE analyst;
CREATE ROLE support;
CREATE ROLE auditor;
CREATE USER analyst IDENTIFIED BY 'analyst' DEFAULT ROLE analyst;
CREATE USER support IDENTIFIED BY 'support' DEFAULT ROLE support;
CREATE USER auditor IDENTIFIED BY 'auditor' DEFAULT ROLE auditor;
GRANT SELECT ON * TO ROLE analyst;
GRANT SELECT ON * TO ROLE auditor;
GRANT SELECT ON customers TO ROLE support;
GRANT SELECT ON orders TO ROLE support;
GRANT SELECT ON support_tickets TO ROLE support;
GRANT SELECT ON active_customers TO ROLE support;
GRANT INSERT, UPDATE ON support_tickets TO ROLE support;

-- 初始化结束后可用 SHOW GRANTS / SHOW TABLES / SHOW VIEWS 检查目录。

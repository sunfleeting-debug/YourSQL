CREATE TABLE departments(id INT, name VARCHAR);
CREATE TABLE employees(id INT, department_id INT, active BOOLEAN);
INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Sales');
INSERT INTO employees VALUES (1, 1, TRUE), (2, 1, FALSE), (3, 2, TRUE);
CREATE INDEX idx_departments_id ON departments (id);
CREATE INDEX idx_employees_id ON employees (id);

-- 常量折叠：1 + 2 在优化计划中变为 3。
SELECT 1 + 2 AS folded_value;

-- AND/OR 化简：TRUE AND 条件 OR FALSE 化简为条件。
SELECT id FROM employees
WHERE TRUE AND active = TRUE OR FALSE
ORDER BY id;

-- 恒假条件消除：优化计划使用 EmptyScan，结果为空。
SELECT id FROM employees WHERE FALSE;

-- 常量表达式参与索引匹配：id = 1 + 2 使用 idx_employees_id。
SELECT id FROM employees WHERE id = 1 + 2;

-- 显式谓词下推：e.active 和 d.id 分别下推到两侧扫描。
EXPLAIN
SELECT e.id, d.name
FROM employees AS e
JOIN departments AS d ON e.department_id = d.id
WHERE e.active = TRUE AND d.id = 1
ORDER BY e.id;

-- 逗号连接：连接条件在 WHERE 里，优化器从中推断等值键（否则退化为笛卡尔积）。
-- 结果与上面的显式 JOIN 写法一致，访问路径断言见 tests/test_join_strategies.py。
SELECT e.id, d.name
FROM employees AS e, departments AS d
WHERE e.department_id = d.id
ORDER BY e.id;

-- OR 分支里的连接键：取所有分支共同要求的等式建哈希表，OR 整体仍作为残余谓词生效。
SELECT e.id, d.name
FROM employees AS e, departments AS d
WHERE (e.department_id = d.id AND d.id = 1) OR (e.department_id = d.id AND d.id = 2)
ORDER BY e.id;

-- 不相关 IN 子查询：只执行一次并复用结果（相关子查询仍逐行执行）。
SELECT id FROM employees WHERE department_id IN (SELECT id FROM departments) ORDER BY id;

-- ── 事务：显式事务的提交与回滚 ────────────────────────────────────────────────
-- 用独立表，避免影响上面 employees / departments 的行数断言。
CREATE TABLE IF NOT EXISTS txn_demo (id INT PRIMARY KEY, note VARCHAR(20));

BEGIN;                          -- BEGIN / BEGIN WORK / BEGIN TRANSACTION 等价
INSERT INTO txn_demo VALUES (1, 'rolled-back');
ROLLBACK;                       -- 上面这一行必须消失

BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;
INSERT INTO txn_demo VALUES (2, 'committed');
COMMIT;

SELECT id, note FROM txn_demo ORDER BY id;   -- 只应看到 (2, 'committed')

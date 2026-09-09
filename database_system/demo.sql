-- MiniSQL 端到端演示脚本
-- 运行：python -m database_system.cli.main --file database_system/demo.sql --plan

-- 1) 建表：注册到系统目录（目录本身也落盘在数据页中）
CREATE TABLE student(id INT, name VARCHAR(20), age INT, score INT);

-- 2) 插入：字符串支持 '' 转义
INSERT INTO student(id, name, age, score) VALUES
  (1, 'Alice', 20, 95),
  (2, 'Bob',   17, 88),
  (3, 'Tom''s',21, 95),
  (4, 'Dan',   20, 71);

-- 3) 查询：WHERE 含算术 + 比较 + AND（可观察常量折叠 age > 10+8 -> age > 18）
SELECT name, score FROM student WHERE age > 10 + 8 AND score >= 90;

-- 4) 恒真谓词：可观察布尔化简与冗余节点消除
SELECT id, name FROM student WHERE 1 = 1 AND age > 18;

-- 5) DISTINCT + ORDER BY + LIMIT
SELECT DISTINCT score FROM student ORDER BY score DESC LIMIT 3;

-- 6) 更新
UPDATE student SET score = score + 5 WHERE age < 18;

-- 7) 删除
DELETE FROM student WHERE id = 4;

-- 8) 最终结果
SELECT * FROM student;

-- 9) 只看执行计划，不实际执行
EXPLAIN SELECT id FROM student WHERE score > 80;

-- 10) 错误诊断（取消注释可观察对应阶段的报错）
-- SELECT @ FROM student;                    -- 词法错误
-- SELECT id FROM student WHERE;             -- 语法错误
-- SELECT nope FROM student;                 -- 语义错误：列不存在
-- SELECT * FROM student WHERE id + name > 1;-- 语义错误：类型不匹配

-- Buffer Pool SQL 压力用例。
--
-- 使用方式：在 data/buffer_pool_lab_large.db 上执行，不要在空库上执行。
-- 工作集分成 16 个核心点查和 24 个次热点点查：核心点查重复三次，
-- 次热点点查只执行一次；随后做完整 HEAP 顺序扫描，再逆序探测热点。
-- 运行时通过 YOURSQL_REPLACEMENT_POLICY 和
-- YOURSQL_BUFFER_POOL_PROTECT_PAGE_TYPES 切换缓存策略。

-- 先确认点查走 INDEX，顺序扫描走 HEAP。
EXPLAIN SELECT COUNT(*) AS core_probe
FROM buffer_lab
WHERE id IN (
    1, 1001, 2001, 3001, 4001, 5001, 6001, 7001,
    8001, 9001, 10001, 11001, 12001, 13001, 14001, 15001
);

EXPLAIN SELECT COUNT(*) AS full_scan
FROM buffer_lab;

-- 核心热点：重复访问三次，供 2Q 识别为热页。
SELECT COUNT(*) AS core_warm_1
FROM buffer_lab
WHERE id IN (
    1, 1001, 2001, 3001, 4001, 5001, 6001, 7001,
    8001, 9001, 10001, 11001, 12001, 13001, 14001, 15001
);

SELECT COUNT(*) AS core_warm_2
FROM buffer_lab
WHERE id IN (
    1, 1001, 2001, 3001, 4001, 5001, 6001, 7001,
    8001, 9001, 10001, 11001, 12001, 13001, 14001, 15001
);

SELECT COUNT(*) AS core_warm_3
FROM buffer_lab
WHERE id IN (
    1, 1001, 2001, 3001, 4001, 5001, 6001, 7001,
    8001, 9001, 10001, 11001, 12001, 13001, 14001, 15001
);

-- 次热点：只访问一次，故意不让 2Q 把它们当成长期热点。
SELECT COUNT(*) AS secondary_once
FROM buffer_lab
WHERE id IN (
    501, 1151, 1801, 2451, 3101, 3751, 4401, 5051,
    5701, 6351, 7001, 7651, 8301, 8951, 9601, 10251,
    10901, 11551, 12201, 12851, 13501, 14151, 14801, 15451
);

-- 扫描污染：COUNT(*) 强制顺序访问整张 HEAP 表。
SELECT COUNT(*) AS scan_1
FROM buffer_lab;

-- 逆序探测：核心页和次热点页都参与，观察扫描后的实际命中情况。
SELECT COUNT(*) AS probe_1
FROM buffer_lab
WHERE id IN (
    15001, 14001, 13001, 12001, 11001, 10001, 9001, 8001,
    7001, 6001, 5001, 4001, 3001, 2001, 1001, 1,
    15451, 14801, 14151, 13501, 12851, 12201, 11551, 10901,
    10251, 9601, 8951, 8301, 7651, 7001, 6351, 5701,
    5051, 4401, 3751, 3101, 2451, 1801, 1151, 501
);

-- 第二轮扫描-探测，避免单次偶然结果。
SELECT COUNT(*) AS scan_2
FROM buffer_lab;

SELECT COUNT(*) AS probe_2
FROM buffer_lab
WHERE id IN (
    15001, 14001, 13001, 12001, 11001, 10001, 9001, 8001,
    7001, 6001, 5001, 4001, 3001, 2001, 1001, 1,
    15451, 14801, 14151, 13501, 12851, 12201, 11551, 10901,
    10251, 9601, 8951, 8301, 7651, 7001, 6351, 5701,
    5051, 4401, 3751, 3101, 2451, 1801, 1151, 501
);

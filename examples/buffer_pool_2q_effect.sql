-- 2Q / 页面类型保护的可复现实验。
--
-- 前置条件：使用 data/buffer_pool_lab_demo.db，并在性能监控中点击“重置当前数据”。
-- 每个区段都应单独执行；切换 LRU、2Q 或页面类型保护后重新重置，再执行同一组 SQL。
-- 推荐实验配置：缓存 16 帧；2Q 冷队列 25% = 4 帧，热队列预算 12 帧；
-- 页面保护预算 50% = 8 帧。该配置与“四策略对比”保持一致，且能突出 2Q。
-- 可在“存储检查 → 缓存”中把缓存页数调整为 16；全局参数见 buffer.py。
--
-- 观测重点：
--   1. core_heap_probe：热点查询是否在顺序扫描后仍有命中；这是 2Q 的主实验。
--   2. index_protected_probe：小型 INDEX 热点是否被页面类型保护保留；这是保护实验。
--   3. 性能监控中同时查看“总命中、冷队列 A1in、热队列 Am、未命中”。

-- ================================================================
-- A. 2Q 主实验：热点 HEAP 页 → 顺序扫描污染 → 热点回访
-- ================================================================
-- 这 8 个 ID 分散在不同记录页；读取 payload，避免覆盖索引把 HEAP 访问优化掉。
-- 连续执行三次，让 2Q 将重复访问的页晋升到 Am。
SELECT id AS core_heap_id, payload AS core_heap_payload
FROM buffer_lab
WHERE id IN (
    1, 65, 129, 193, 257, 321, 385, 449
);

SELECT id AS core_heap_id, payload AS core_heap_payload
FROM buffer_lab
WHERE id IN (
    1, 65, 129, 193, 257, 321, 385, 449
);

SELECT id AS core_heap_id, payload AS core_heap_payload
FROM buffer_lab
WHERE id IN (
    1, 65, 129, 193, 257, 321, 385, 449
);

-- 用完整 HEAP 扫描制造一次性页面污染。
SELECT COUNT(*) AS pollution_scan
FROM buffer_lab;

-- 重点看这一条：2Q 的缺页应明显少于 LRU，Am 命中应明显增加。
SELECT id AS core_heap_probe_id, payload AS core_heap_probe_payload
FROM buffer_lab
WHERE id IN (
    1, 65, 129, 193, 257, 321, 385, 449
);

-- ================================================================
-- B. 页面保护实验：小型 INDEX 热点 → HEAP 扫描 → INDEX 回访
-- ================================================================
-- 执行本段前请再次点击“重置当前数据”。这些相邻 ID 只需少量 INDEX 页。
-- LRU + 页面保护应比纯 LRU 少缺页；2Q 本身也应将重复页晋升到 Am。
SELECT id AS index_hot_id
FROM buffer_lab
WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16);

SELECT id AS index_hot_id
FROM buffer_lab
WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16);

SELECT id AS index_hot_id
FROM buffer_lab
WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16);

SELECT COUNT(*) AS protection_scan
FROM buffer_lab;

SELECT id AS index_protected_probe_id
FROM buffer_lab
WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16);

-- YourSQL 存储 OS 演示脚本
--
-- 推荐方式：在工作台中按“区段”执行，每执行一个区段就刷新一次“存储检查”。
-- data/show_storage.db 使用 1024 B 页；请在“存储检查”中观察页地图、页详情、
-- 槽目录、原始布局、缓存统计和索引页。重复破坏性区段前，请先用
-- data/show_storage.db.bak 覆盖恢复主库。

-- ================================================================
-- 0. 目录与初始物理状态
-- ================================================================
SHOW TABLES;

SHOW CREATE TABLE slot_lab;
SHOW CREATE TABLE index_lab;
SHOW CREATE TABLE cache_lab;
SHOW CREATE TABLE txn_lab;

SELECT id, kind, payload
FROM slot_lab
ORDER BY id;

SELECT id, group_id, payload
FROM index_lab
WHERE id IN (1, 60, 120, 180)
ORDER BY id;

-- ================================================================
-- 1. FREE 链表：从空闲页分配，再归还链表
-- ================================================================
-- 先在“存储检查 → 页面”记录 free_page_count 和 free_list_head。
-- 下面 CREATE 只写目录；INSERT 才会真正从 FREE 链表取一个 HEAP 页。
DROP TABLE IF EXISTS free_reuse_demo;
CREATE TABLE free_reuse_demo(id INT PRIMARY KEY, note VARCHAR(80));
INSERT INTO free_reuse_demo VALUES (1, '这个页来自 FREE 链表，可观察页号复用');

SELECT *
FROM free_reuse_demo;

-- 此时刷新页面地图：空闲页数应减少，新的 HEAP 页应出现在原 FREE 页位置。
DROP TABLE free_reuse_demo;

-- 再刷新页面地图：刚才的 HEAP 页回到 FREE 链表头部。

-- ================================================================
-- 2. 页内槽：删除造成洞，扩容更新触发压缩
-- ================================================================
-- slot_lab 初始已删除 id=2/4/6/8。选择它所在的 HEAP 页，先看：
--   * 槽目录仍保留这些 slot_id，但 deleted=true；
--   * free_regions 有多个不连续片段；
--   * 活记录的物理 offset 仍分散在页尾记录区。
SELECT id, kind, payload
FROM slot_lab
ORDER BY id;

-- 这次扩容超过任一单独空洞，页层会压缩并重新安排记录区；
-- 前后都刷新同一页详情，对比 slot 的 offset、free_regions 和 record_region。
UPDATE slot_lab
SET payload = 'QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ'
WHERE id = 1;

SELECT id, kind, payload
FROM slot_lab
ORDER BY id;

-- ================================================================
-- 3. 槽复用：插入新行，优先复用 deleted slot
-- ================================================================
-- 从 .bak 恢复后执行这一段最容易观察：新行会拿到已经删除的槽号，
-- 而不是无条件追加一个新槽。
INSERT INTO slot_lab VALUES (20, 'reused', '新记录优先复用 deleted 槽');

SELECT id, kind, payload
FROM slot_lab
ORDER BY id;

-- ================================================================
-- 4. B+Tree 索引页：点查与顺序扫描对照
-- ================================================================
EXPLAIN
SELECT id, group_id
FROM index_lab
WHERE group_id = 3
ORDER BY id;

SELECT id, group_id
FROM index_lab
WHERE group_id = 3
ORDER BY id;

EXPLAIN
SELECT id, group_id
FROM index_lab
ORDER BY id;

SELECT COUNT(*) AS index_lab_rows
FROM index_lab;

-- 在“存储检查 → 索引”中选择 idx_index_lab_group，
-- 观察根页、内部页、叶子页、key/RowId 和叶子链。

-- ================================================================
-- 5. Buffer Pool：热点页、顺序扫描污染、热点回访
-- ================================================================
-- 推荐先在“存储检查 → 缓存”把容量设为 16，策略设为 2Q，
-- 然后点击“重置当前数据”。每一组执行后观察 hits/misses/evictions、
-- cold_hits/hot_hits 和 eviction_order。

-- 分散访问多个 HEAP 页，连续三次让 2Q 识别热点。
SELECT id, payload
FROM cache_lab
WHERE id IN (1, 33, 65, 97, 129, 161, 193, 225)
ORDER BY id;

SELECT id, payload
FROM cache_lab
WHERE id IN (1, 33, 65, 97, 129, 161, 193, 225)
ORDER BY id;

SELECT id, payload
FROM cache_lab
WHERE id IN (1, 33, 65, 97, 129, 161, 193, 225)
ORDER BY id;

-- 顺序扫描制造一次性页面污染。
SELECT id
FROM cache_lab
ORDER BY id;

-- 热点回访：对比顺序扫描前后的命中与淘汰。
SELECT id, payload
FROM cache_lab
WHERE id IN (1, 33, 65, 97, 129, 161, 193, 225)
ORDER BY id;

-- ================================================================
-- 6. 事务/WAL：页修改、回滚和前像
-- ================================================================
BEGIN;

UPDATE txn_lab
SET status = 'changed'
WHERE id = 2;

DELETE FROM txn_lab
WHERE id = 3;

SELECT id, status, note
FROM txn_lab
ORDER BY id;

-- 观察脏页/缓存变化后回滚；回滚完成后数据和槽目录应恢复。
ROLLBACK;

SELECT id, status, note
FROM txn_lab
ORDER BY id;

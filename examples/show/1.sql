-- 制造热点
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

-- 污染缓存
SELECT COUNT(*) AS pollution_scan
FROM buffer_lab;

-- 重查热点
SELECT id AS core_heap_probe_id, payload AS core_heap_probe_payload
FROM buffer_lab
WHERE id IN (
    1, 65, 129, 193, 257, 321, 385, 449
);
# 页式存储调试 CLI 演示手册

## 1. 用途

本手册使用 `database_system.tests.test_cli`，只通过页式存储模块观察和修改数据库文件，不经过 SQL 编译器、Catalog 或执行器。

可用于验证和调试：

- 超级块信息
- 页分配和页回收
- 页头、槽位目录和记录内容
- 页的序列化与反序列化
- 页链表的建立
- 缓冲池命中、淘汰和刷盘
- LRU/FIFO 策略
- dirty 页写回
- 空闲页复用
- 非法页和故障注入

命令应在项目根目录执行。

## 2. 创建干净的演示数据库

下面命令适用于 PowerShell。删除的只是专用演示文件：

```powershell
$demoDb = Join-Path (Get-Location) "storage-demo.db"

if (Test-Path -LiteralPath $demoDb) {
    Remove-Item -LiteralPath $demoDb -Force
}
```

如果不希望删除文件，可以改用一个新的数据库文件名。

## 3. 查看超级块

```powershell
python -m database_system.tests.test_cli $demoDb info
```

新数据库预期包含：

```text
page_size=4096
page_count=1
free_list_head=-1
catalog_root=-1
```

说明：第 0 页是超级块，普通数据页从第 1 页开始。

## 4. 分配页面

```powershell
python -m database_system.tests.test_cli $demoDb alloc --page-type DATA
python -m database_system.tests.test_cli $demoDb alloc --page-type DATA
python -m database_system.tests.test_cli $demoDb alloc --page-type CATALOG
```

在干净数据库中，分配结果应为：

```text
page 1 -> DATA
page 2 -> DATA
page 3 -> CATALOG
```

查看所有页面：

```powershell
python -m database_system.tests.test_cli $demoDb pages
```

## 5. 查看页结构和原始字节

查看页头、槽位和记录：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
```

查看页头原始字节：

```powershell
python -m database_system.tests.test_cli $demoDb raw 1 --offset 0 --length 64
```

重点观察：

- `page_type`
- `next_page_id`
- `num_slots`
- `free_pointer`
- `free_space`
- 页头二进制内容

## 6. 手动插入、更新和删除记录

CLI 的记录参数是十六进制字节，不经过 `engine/record.py` 的 Row 编码。

插入 `hello` 和 `world`：

```powershell
python -m database_system.tests.test_cli $demoDb insert 1 68656c6c6f
python -m database_system.tests.test_cli $demoDb insert 1 776f726c64
```

其中：

```text
68656c6c6f = hello
776f726c64 = world
```

查看结果：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
```

更新槽位 0：

```powershell
python -m database_system.tests.test_cli $demoDb update 1 0 6869
```

删除槽位 1：

```powershell
python -m database_system.tests.test_cli $demoDb delete 1 1
```

再次查看：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
```

此时可以看到槽位 1 被标记为 `deleted`。

## 7. 建立页链

将页 1 指向页 2：

```powershell
python -m database_system.tests.test_cli $demoDb next 1 2
python -m database_system.tests.test_cli $demoDb next 2 -1
```

查看页链：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
python -m database_system.tests.test_cli $demoDb page 2
```

预期结果：

```text
page 1 next=2
page 2 next=-1
```

## 8. 观察缓冲池命中、淘汰和日志

使用 2 个缓冲帧访问 3 个页面，制造淘汰：

```powershell
python -m database_system.tests.test_cli `
    $demoDb buffer `
    --fetch 1 2 3 1 `
    --pool-size 2 `
    --policy LRU `
    --verbose `
    --flush
```

重点观察：

```text
MISS page ...
HIT page ...
EVICT page ...
UNPIN page ...
stats=...
log:
```

切换 FIFO 再执行一次：

```powershell
python -m database_system.tests.test_cli `
    $demoDb buffer `
    --fetch 1 2 3 1 `
    --pool-size 2 `
    --policy FIFO `
    --verbose `
    --flush
```

## 9. 观察 dirty 页写回

通过缓冲池修改页内第 100 字节，并标记为 dirty：

```powershell
python -m database_system.tests.test_cli `
    $demoDb buffer `
    --fetch 1 `
    --write 1 100 aa `
    --flush `
    --verbose
```

查看写回后的原始字节：

```powershell
python -m database_system.tests.test_cli `
    $demoDb raw 1 `
    --offset 96 `
    --length 16
```

应该可以看到 `aa`。

## 10. 回收和复用页面

回收页 3：

```powershell
python -m database_system.tests.test_cli $demoDb free 3
python -m database_system.tests.test_cli $demoDb info
python -m database_system.tests.test_cli $demoDb pages
```

重新申请一个 DATA 页：

```powershell
python -m database_system.tests.test_cli $demoDb alloc --page-type DATA
```

由于空闲页链表采用 LIFO，预期重新得到页 3：

```text
allocated page=3 type=DATA
```

## 11. 验证持久化

每条 CLI 命令都会重新打开和关闭数据库文件，因此下面命令可以验证正常关闭后的持久化结果：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
python -m database_system.tests.test_cli $demoDb info
```

## 12. 可选故障注入

验证缓冲池在所有页面 pinned 时无法淘汰：

```powershell
python -m database_system.tests.test_cli `
    $demoDb buffer `
    --fetch 1 2 `
    --pool-size 1 `
    --keep-pinned
```

直接破坏页头中的 `free_pointer`：

```powershell
python -m database_system.tests.test_cli `
    $demoDb write 1 8 0000
```

再次读取页面：

```powershell
python -m database_system.tests.test_cli $demoDb page 1
```

应该得到页格式错误。故障注入只建议对演示副本执行，因为它可能使页面无法正常反序列化。

## 13. 注意事项

1. `test_cli` 不使用 SQL Row 编码，插入参数就是页面中的原始记录字节。
2. `buffer` 命令中的日志只存在于当前进程，不是 WAL，也不会持久化。
3. `write` 命令可以制造非法页格式，使用前应确认数据库文件是调试副本。
4. 当前存储系统不提供崩溃恢复、事务回滚或并发控制。

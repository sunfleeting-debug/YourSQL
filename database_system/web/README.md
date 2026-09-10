# 前端启动与团队协作指南

YourSQL 工作台是本项目 MiniSQL 数据库的浏览器操作界面。Python 服务同时提供前端页面和数据库接口，启动一个进程即可使用。

## 首次启动

1. 安装 Python 3.10 或更高版本，以及现代浏览器（Edge、Chrome、Firefox 等）。不需要安装 MySQL、Node.js、npm 或第三方 Python 包。
2. 克隆团队仓库或下载并解压代码，然后在**仓库根目录**打开终端。该目录应包含 `README.md` 和 `database_system/`。
3. 检查 Python 版本并启动：

   ```bash
   python --version
   python -m database_system.web.server --db data/workbench.db --port 8765
   ```

   Windows 若找不到 `python`，可将命令中的 `python` 换成 `py -3`；macOS / Linux 通常使用 `python3`。请确认实际解释器版本至少为 3.10。

4. 终端显示 `YourSQL: http://127.0.0.1:8765` 后，在浏览器访问该地址。保持终端运行；服务不会自动打开浏览器。
5. 使用结束时，在服务终端按 `Ctrl+C`。

不要双击 `static/index.html`，也不要使用 VS Code Live Server 打开它：页面需要由上述 Python 服务提供 `/api/state` 和 `/api/execute` 接口。静态页面托管（例如 GitHub Pages）不能独立运行数据库工作台。

## 第一次看到空数据库怎么办

新建数据库没有任何表，这是正常情况。使用右上角“演示用例”下拉框选择功能脚本，再点击“执行 SQL”即可载入并执行对应测试。脚本使用 `DROP TABLE IF EXISTS`，同一个用例可以反复演示。

也可以在编辑器输入自己的 SQL：

```sql
CREATE TABLE team_members (
  id INT PRIMARY KEY,
  name VARCHAR(50)
);

INSERT INTO team_members VALUES (1, 'Alice'), (2, 'Bob');

SELECT * FROM team_members;
```

执行后应在左侧看到 `team_members`，最后一条语句的结果包含两行记录。后续只执行 `SELECT` 即可重新查看。

## 常用操作

| 操作 | 使用方法 |
| --- | --- |
| 查看表结构 | 单击左侧表名 |
| 生成查询 | 双击表名，或在结构视图点击“查询前 100 行” |
| 执行 SQL | 点击执行按钮，或在编辑器内按 Ctrl / Command + Enter；优先执行选中的文字 |
| 新建查询 | 点击查询标签右侧的 `＋` |
| 调整编辑区 | 拖动深色编辑区域右下角 |
| 查看多条语句结果 | 使用结果区的“语句”下拉框 |
| 筛选结果 | 在“筛选当前结果”中输入关键词，只筛选已加载的行，不重新查询数据库 |
| 查看优化效果 | 点击“执行计划”，比较优化前后计划及生效规则 |
| 导出数据 | 点击“导出 CSV”；导出当前语句已加载的全部行，不受结果筛选影响 |
| 保存查询 | 点击“保存 SQL”下载脚本；使用“打开 SQL”重新载入 |

执行后，结果抽屉还提供编译流水线调试页签：`词法分析` 查看 Token 流，`语法树` 查看 AST，`语义检查` 查看语义阶段状态，`执行计划` 查看优化前计划，`优化结果` 查看优化后计划和规则，`执行阶段` 查看执行器输出。多条 SQL 可以通过结果区上方的“查看语句结果”选择器逐条查看；某一阶段出错时，后续阶段会标记为未执行。

查询标签和历史仅保留在当前页面，刷新或关闭前请保存需要的 SQL。查询结果位于工作区底部抽屉，可折叠以扩大 SQL 编辑区；执行语句后会自动展开。表格展示及 CSV 导出最多包含前 1,000 行，完整查询仍由引擎执行。

## 数据文件与团队共享

- `--db data/workbench.db` 指定的是相对于**启动命令所在目录**的数据文件；文件和父目录不存在时自动创建。也可传入绝对路径。
- 不带参数执行 `python -m database_system.web.server`，默认使用 `data/workbench.db` 和端口 `8765`。
- 下次使用相同的数据文件启动，会保留之前创建的表和记录。文件是 MiniSQL 自定义格式，不能用 MySQL 或 SQLite 工具打开。
- `.gitignore` 已忽略 `data/` 和 `*.db`。队友克隆仓库后不会自动拥有你的本地数据库、演示记录或截图，每人可以独立创建数据库。
- 团队共享初始化数据时，将可重复使用的 `.sql` 脚本放在仓库中受版本管理的目录（不要放在 `data/`），由队友通过“打开 SQL”载入并执行。现有的完整引擎演示见 [`../demo.sql`](../demo.sql)。执行前先阅读脚本中的建表、删除等操作。
- 同一个数据库文件不要同时交给多个服务或 CLI 进程写入。需要多个工作台时，为每个进程指定不同文件和端口。
- 服务只监听本机 `127.0.0.1`。你电脑上的预览链接不是团队共享服务地址；队友需要在自己的电脑上启动。

## 前后端分工与修改方式

```text
database_system/
  web/
    server.py          Python 服务、HTTP 路由、调用 Database 引擎
    static/
      index.html       页面结构
      style.css        布局、配色、响应式样式
      app.js           编辑器、请求接口、结果展示和交互
  engine/database.py   数据库执行入口
  tests/test_web.py    Web 接口集成测试
```

前端修改 `static/` 下的文件后刷新浏览器即可生效，无需构建。修改 `server.py` 后停止并重新启动服务。

接口由同一个服务提供：

- `GET /api/state`：数据库名称、表与字段、缓存统计及当前服务的请求令牌。
- `POST /api/execute`：发送 JSON `{"sql": "SELECT * FROM team_members;"}`，并带上从状态接口获取的 `X-YourSQL-Token` 请求头；返回逐条语句结果与最新表结构。现有 `app.js` 已实现这一流程。

SQL 语句执行失败时接口仍可能返回 HTTP 200，需要检查每条结果的 `ok`、`stage` 和 `message`。多条语句可能部分成功，当前引擎不提供事务回滚。

提交代码时应包含完整 `web/` 目录、相关测试和文档，不能只上传 `index.html`。在仓库根目录查看 `git status`，确认新增文件也已加入提交；本地数据库不需要提交。

## 验证修改

在仓库根目录执行：

```bash
# 全部测试（包含 Web 接口）
python -m database_system.tests.run_all

# 仅 Web 接口测试
python -m unittest database_system.tests.test_web -v
```

Web 测试使用临时数据库和自动分配的端口，不需要提前启动服务，也不会操作你的工作台数据。

前端修改后再在浏览器确认：页面能连接、示例 SQL 可执行、表结构与结果可见、语法着色与结果筛选正常。自动化接口测试不覆盖浏览器视觉效果。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `No module named database_system` | 将终端切换到包含 `database_system/` 的仓库根目录，再执行启动命令 |
| 端口占用 / `Address already in use` / `WinError 10048` | 改用 `--port 8766`，然后访问 `http://127.0.0.1:8766` |
| 页面打不开 | 检查服务终端是否仍在运行，并使用终端实际显示的地址 |
| 页面提示连接失败或要求刷新 | 确认通过 Python 服务地址打开页面；服务重启后刷新页面以重新获取令牌 |
| 看不到别人创建的表 | 确认 `--db` 路径；本地 `.db` 文件不随 Git 共享 |
| 找不到自己的旧数据 | 对照启动终端输出的 `Database:` 绝对路径，确认没有从另一个目录启动并创建新文件 |
| SQL 报错 | 查看结果中的错误阶段和提示；支持范围以根目录 README 为准，暂无 JOIN、聚合或事务回滚 |

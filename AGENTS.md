
## 开发规范
- 建议使用 `uv` 管理后端依赖，建议使用 `npm` 管理前端依赖。
- 保留并及时更新 `requirements.txt` 以保证兼容
- 注意保证必要的参数声明，加强可读性，而不是`Any | None`走到死。
- 前端（`web/`）代码提交前跑 `npm run format`，保持 `npm run format:check` 通过；Prettier 配置见 `web/.prettierrc.json`。
- 不把长逻辑链压进一行：单行超过 150 字符（模板字符串、className 拼接除外）应考虑拆分；`className` 这类条件拼接请用 `web/src/view-classes.ts` 的纯函数。

## Python 导入规范
- `yoursql/` 内的项目代码统一使用绝对导入，例如 `from yoursql.common.types import RowId`；禁止使用 `from .`、`from ..` 等相对导入，避免移动文件时重新计算层级并降低 IDE 静态分析质量。
- 包内导入应指向具体模块，不要为了方便从上层 `__init__.py` 聚合入口导入内部依赖；`__init__.py` 只负责稳定的公开 API 重导出，避免初始化循环。
- `TYPE_CHECKING` 分支中的类型导入同样使用绝对导入，不通过 `sys.path`、运行时路径修改或隐式工作目录解决导入问题。
- mixin、协议适配器等通过组合获得成员的类，必须用 `Protocol` 或明确的类型基类声明宿主属性和协作方法，不能依赖运行时多重继承让 Pylance“猜到”成员存在。
- 新增或迁移模块后，至少执行 `ruff check yoursql`，并用 `rg -n "^(from|import) \\." yoursql --glob '*.py'` 确认没有新增相对导入；涉及导入拓扑变化时执行完整测试集。

## 后端代码归属
- `yoursql/sql/` 只放 SQL 语言前端：词法、语法、AST、绑定和编译入口；不要放代价模型、存储访问或执行算子。
- `yoursql/sql/` 是语言前端目录，不要整体改名为 `compiler/`；`compiler.py` 只是其中一个“AST/绑定到逻辑计划”的入口文件。
- `yoursql/planner/` 放逻辑计划、物理计划、优化规则、统计信息、计划缓存和代价估算。
- 优化器接收 AST/逻辑计划，不负责解析原始 SQL 文本；原始 SQL 的规范化只允许出现在计划缓存的键生成处。
- `yoursql/execution/` 放 Volcano 执行器、表达式求值以及 SELECT 查询执行适配；`executor.py` 保持通用算子，不直接依赖 SQL AST，SQL 查询编排放在 `evaluator.py` / `query.py`。需要读写表、索引或页时通过运行时实例提供的接口访问。
- `yoursql/engine/runtime/` 只负责 Database 生命周期、事务边界以及编译器、计划器、执行器、存储和安全模块的协调；SQL 命令处理放在 `runtime/commands.py`，不要把新的表达式、连接、扫描或投影逻辑堆回 `database.py`。
- `yoursql/engine/catalog.py` 放目录元数据；`yoursql/storage/` 放页、堆表、缓存、磁盘和 B+Tree 实现；`yoursql/engine/services/` 放 HTTP、SSH、Workbench 等协议适配。
- 新增后端文件必须放入上述职责目录，不要把业务实现重新堆回 `yoursql/engine/` 根目录；跨层依赖只能从上游语言/计划层指向下游执行/存储层，禁止 `sql` 反向依赖 `engine` 或 `storage`。

## 注释规范
- 使用中文本土化注释
- 使用必要的简短的头docsting
- 对于内部注释，使用简要的HOW注释
- 对于fix/bug/难点，使用稍详细的WHY注释
- 对于TODO/临时方案，注意注释标记。

## 前端UI/UX规范 & 信息密度
- 面向专业用户和长时间使用场景，界面默认数据优先、文案简洁，避免新手教程式的解释。
- 不重复展示用户从布局、选中态、颜色或控件状态已经能直接判断的信息；删除“点击选中/再次点击查看”“已突出显示”等操作复述和冗余状态串。
- 仅保留错误、权限要求、加载进度、数据缺失和会影响诊断结论的限制说明；其余说明优先收进语义化的 `aria-label`、`title` 或折叠详情，不占用主工作区。
- 修改 UI 文案时同步检查同一状态在横幅、图例、提示和详情面板中的重复出现，避免为了“解释清楚”堆叠废话。

## 提交规范
- 中文语义化提交。
- 提交前如有必要，注意更新`./README.md`、`./docs/TODO.md`
- 每次迭代审核完后，都建议提交，提交注意范围，不要交叉！

## SQL 功能测试约束
- 新增或修改 SQL 语法、语义、执行算子或查询优化规则时，必须同步补充自动化测试。
- 每个可见的新 SQL 功能至少提供一份可复现用例，包含建表、插入数据、查询或 `EXPLAIN` 以及预期结果；统一维护在 `examples/test_example.md`，必要时配套更新 `examples/test_example.sql`。
- 优化规则必须同时断言结果正确性和计划/访问路径变化，避免只验证 `EXPLAIN` 文案而没有验证实际执行。
- 涉及 `NULL`、外连接、错误处理或计划缓存的规则，必须补充对应边界测试，确认优化前后语义一致。

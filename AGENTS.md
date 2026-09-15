# AGENTS.md

本仓库是一个 Legado（阅读）书源管理工具链。给 agent 的核心约定如下。

## 先读这些

| 文档 | 内容 |
| :--- | :--- |
| `skills/agent-write-safety/SKILL.md` | **动手改文件前必读**：受限环境下的可靠写入通道与纪律 |
| `skills/legado-source-toolchain/SKILL.md` | 14 个 CLI 命令、SQLite 管理库、AI 修复循环的用法 |
| `skills/legado-source-lessons/SKILL.md` | 架构决策与踩过的坑 |
| `skills/legado-book-source/SKILL.md` | 书源规则语法、站点分析流程、调试方法 |
| `WORKFLOW.md` | 书源「新增 / 导入 → 校验 → 整理 → 报告」完整链路 |

## 硬性约定

1. **运行时数据都在 `data/`，不进版本库**（已 gitignore）。
   `candidates.json` 是唯一候选主库，**请单独备份**。
2. **SQLite 是管理库，JSON 是交付格式**。给 Legado 的 JSON 由
   `Store.export_json()` 生成，不要把 JSON 当作唯一事实来源。
3. **验证必须由规则回放器完成**，AI 只负责提议。
   不接受「模型说通过」作为验收依据。
4. **不支持的规则语法要显式返回原因**，不能静默返回空——
   否则会把「无法验证」误判成「规则失效」。
5. **URL 一律先规范化再用作 key**（`core/loader._normalize_url`）。
   这是迁移时踩过的真 bug：571 个源因为存了原始 URL 而关联不上校验记录。
6. **改代码前确保 `git status` 干净**，改完立刻跑
   `python -c "import 模块"` 与 `python -m unittest discover -s tests -t .`。
7. **系统标签枚举只在后端定义**（`core/tags.py`、`core/models.py`），
   前端经 `GET /api/sources/tags/meta` 获取，不得再硬编码一份。
   两边各存一份必然漂移——历史上书源类型就曾把 3 当成视频、还编出过
   Legado 不存在的 4。调 `splitSystemUser()` **之前必须先 `await ensureTagMeta()`**：
   拆分结果是存下来的快照，枚举没到位就拆会把系统标签存成用户标签且不再纠正。

## 上游 App 源码（查证用）

代码注释里大量 `Xxx.kt:行号` 指向「阅读」App 的源码。本地在
`E:\Documents\GitHub\legado-with-MD3`——**不是本仓库的依赖**，只在查证时读它。

查的时候**认符号不认行号**：行号会随上游改动漂移，注释里通常同时给了函数名 /
异常名 / 注解 / 代码原文，用那个搜更可靠。引用前先确认该仓库当前版本。

| 要查什么 | 去哪（前缀 `app/src/main/java/io/legado/app/`）|
| :--- | :--- |
| 调试链路分派、`key` 形态 | `model/Debug.kt` |
| 规则解析（CSS / JSON / JS） | `model/analyzeRule/AnalyzeRule.kt` |
| URL 与 webView 处理 | `model/analyzeRule/AnalyzeUrl.kt` |
| 书源类型定义 | `constant/BookSourceType.kt` |
| 正文抓取与分页 | `help/book/BookContent.kt` |
| 校验口径 | `data/repository/BookSourceCheckRepository.kt` |
| 书源表结构 | `data/dao/BookSourceDao.kt` |
| Web 服务 HTTP 接口 | `web/KtorServer.kt`、`api/controller/BookSourceController.kt` |
| 调试 WebSocket | `web/socket/BookSourceDebugWebSocket.kt` |

## 改文件的正确姿势

不要在多行 shell 字符串里拼接代码。用 stdin 通道执行完整 Python 脚本：

    $code = @'
    import pathlib
    p = pathlib.Path("core/xxx.py")
    t = p.read_text(encoding="utf-8")
    # 唯一性断言 + 定位 + 修改
    # 全部断言通过后才 write_text
    ' @
    $code | & .venv/Scripts/python.exe -

详见 `skills/agent-write-safety/SKILL.md`。

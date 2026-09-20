# AGENTS.md

本仓库是一个 Legado（阅读）书源管理工具链。给 agent 的核心约定如下。

## 先读这些

| 文档 | 内容 |
| :--- | :--- |
| `skills/agent-write-safety/SKILL.md` | **动手改文件前必读**：受限环境下的可靠写入通道与纪律 |
| `skills/legado-source-toolchain/SKILL.md` | 13 个 CLI 命令、SQLite 管理库、规则回放器、失效归因、AI 修复循环的用法 |
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
   跨表/跨库关联（如 `checks_map()` 与 `BookSourceRecord.url`）**两侧都要归一**。
   **前端也算一侧**：`GET /api/sources` 的 `source_url` 是库里归一化过的，而
   `result_json.items[].url` 来自 `build_record` 的**原文**——下游（就地回填、
   跳转、匹配）拿它当 key 时必须先归一，否则一条都对不上，且**不报错**。
   详见 lessons §五。
5b. **缓存有效性的轴**：`probe_depth`（探得多深，`core/settings_store.PROBE_DEPTHS`
   一根四档）与 `search_probed`（那一轮**实际**跑没跑搜索，见
   `core/checker.is_cache_item_valid`）。新增任何影响结论的探测维度，都要在这里加一根
   轴并把 `CACHE_VERSION` 加一——只比版本/指纹/时间的话，换个探测参数重跑会**静默复用**
   上一次的结论，界面上显示「校验完成」，看起来一切正常。
   改 `probe_depth` 的**编号含义**（而不只是取值范围）同样要加版本号：缓存里的这一列
   记的是「实际执行到的深度」，编号一平移，整列的含义就跟着变。
6. **改代码前确保 `git status` 干净**，改完立刻跑
   `python -c "import 模块"` 与 `python -m unittest discover -s tests -t .`。
7. **系统标签枚举只在后端定义**（`core/tags.py`、`core/models.py`），
   前端经 `GET /api/sources/tags/meta` 获取，不得再硬编码一份。
   两边各存一份必然漂移——历史上书源类型就曾把 3 当成视频、还编出过
   Legado 不存在的 4。调 `splitSystemUser()` **之前必须先 `await ensureTagMeta()`**：
   拆分结果是存下来的快照，枚举没到位就拆会把系统标签存成用户标签且不再纠正。
   （**改词表**比改这份下发更麻烦：存量 `group_name` 里存的是当时的词，见 #17。）
8. **校验参数的默认值与取值范围只在 `core/settings_store.py` 定义**
   （`DEFAULTS` / `LIMITS` / `PROBE_DEPTHS`）。前端经 `GET /api/settings` 拿值、
   拿 `limits` 渲染上下界，不得再硬编码一份。历史上并发数曾在 `ops.py`(20)、
   CLI(50)、`AsyncChecker`(50) 三处各写一遍，结果是界面上改不动、也没人知道该信哪个——
   这种漂移靠「对齐数字」修不掉，只能靠**把数字从调用点删掉**。
   `cli/main.py` 的 argparse 默认值是**独立的另一条链路**，不要试图统一。
9. **`sources.source_url` 的唯一性是「仅在用」，不是全表**
   （`idx_sources_live_url ... WHERE deleted_at = ''`，见
   `Store.migrate_sources_url_scope_once`）。**不要在它上面加回 `UNIQUE`**：
   那会让删除变成"原地打标记而没腾出 URL"，于是「先删掉旧的、再导入新版」必然
   撞上回收站里那一行、被判冲突——三条路（更新 / 清空回收站 / 删了重来）会同时堵死。
   在用（`deleted_at=''`）每个 URL 至多一行这条**必须保住**：App 存书源是
   `@Insert(onConflict = REPLACE)`、键是 `bookSourceUrl`，两条同 URL 的源导出过去
   只会留最后一条，另一条静默消失。回收站可以留同 URL 的多个历史版本。
10. **文档也只有一个事实来源：一处写，别处只留指针。**
   这是 #7 / #8 同一条原则的第三次应用——**前两次是代码里的枚举和默认值，
   这次是文档**。判据是**性质**，不是重要程度（重要程度只决定「要不要收」）：

   | 性质 | 去哪 | 一句话判据 |
   | :--- | :--- | :--- |
   | 一句话能说成规矩、违反有代价 | **本文件** | 「必须遵守」 |
   | 需要讲清为什么（症状 → 根因） | `skills/legado-source-lessons/SKILL.md` | 「要解释」 |
   | 使用者需要知道 | `README.md` / `WORKFLOW.md` | 「他会碰到」 |
   | 还没做 | `TODO.md` | 「待办」 |

   - **论证与约束要能互查**：lessons 每条末尾标一行 `→ 已提炼为 AGENTS #N`；
     本文件对应条目里指回 lessons 的小节号。规矩和「为什么」脱节，两边都会失效。
   - **已完成的事项从 `TODO.md` 删掉**——那里只放「要做什么」，不放「做过什么」。
     混着放会立刻过期：做完的条目还留着，下次翻到会以为没做。
   - **不要往 README 里塞坑**。它是给使用者的，坑是给改代码的人的。
11. **判定逻辑里不能放「我们自己写进去的结论」当输入。**
    `organizer` 按 `bookSourceType` 生成分组标签，而 `reclassify` 又把分组当特征
    文本读——于是「类型错 → 分组写成 📖小说 → 判据读到小说字样 → 再判成小说」，
    **错的标签自我固化，永远纠正不过来**（实测 119 条）。
    凡是「A 的输出被写回 A 的输入」，先问这个字段是**原始观测**还是**我们的结论**。
    判据要经得起一句反问：**把派生字段全清掉，结论还成立吗？**
    派生字段的候选：分组、缓存、指纹、报表、质量标签。见 lessons §十八。
12. **读一个字段之前，先确认它真的存在。**
    `ruleContent.image` 是个**不存在的字段**（Legado 的 `ContentRule` 里没有），
    读它恒为空 → 那段「有图片规则就 +3 分」的逻辑**从未执行过**。
    危险不在算错（它算不出错），在于**读起来像一个存在的信号**——有人会照它去调权重。
    判一段代码死没死，**别靠读，去数**；删死分支时**必须同时处理那条钉着旧行为的
    断言**，否则死代码看起来是活的。见 lessons §十九。
13. **能由配置推导的状态，不要让用户手工维护。**
    `enabledExplore` 是个开关，而「有没有配发现规则」是**算得出来的**——实测它与
    配置漂了 **885 条**（739 条开着却完全没配置，App 的发现页里是死项；146 条有配置
    却关着，功能被静默关掉）。同类还有：调试要求手填本来能从 `exploreUrl` 取的下游
    URL、就地回填导致的筛选项/排序过期。
    **手工维护的状态一定会漂。算得出的一律推导，算不出的才让用户填。**

14. **变异窗口内绝不 `git add`。** 本仓库的测试惯例是变异验证（临时把源码改坏 →
    跑全量 → 确认变红 → 还原）。变异态只存活几秒，而 `git add` 抓的是**磁盘当时的内容**
    ——本仓库真的发生过一次：HEAD 上出现一个自相矛盾的提交（源码写着 `"detail"`，
    同一个提交里的测试断言它必须是 `"explore"`）。变异前先把当前状态提交成 WIP，
    或用 `git worktree` 隔离出干净副本；**变异窗口内不做任何 git 操作**。见 lessons §十二。
15. **弹窗/抽屉内部结构的样式，只能写在 scoped 之外。**
    el-dialog / el-drawer 是 **teleport 到 body** 的，组件内的 scoped 样式
    **够不到**它内部——写了不生效，而且不报错；**`:deep()` 也救不回来**：那个带前缀
    class 的元素身上根本没有 `data-v-*`。写法是组件里单开一个**不带 scoped** 的
    `<style>` 块、用 class 前缀限定；**只有跨组件共用的才上全局**
    `frontend/src/styles.css`（`.dot` 系列就是共用才上去的），单组件自己的规则别丢进去。
    见 lessons §十三。

16. **界面由后端托管在 `/`**（`backend/app.py` 末尾把 `frontend/dist` 挂成静态目录，
    目录不存在时不挂并打提示）。所以**新增路由/中间件必须写在那个挂载之前**，
    否则会被静态托管吃掉。改了前端要重新 `cd frontend && pnpm build`
    （后端启动会提示 dist 落后于源码）；`pnpm dev` 的 5173 + 代理仍是开发态。

17. **状态词表改了名，必须配一次存量换词迁移——存量字符串里存的是「当时的那个词」。**
    `sources.group_name` 是历史时刻写下的文本。词表一改（2026-09：「需验证」→
    「需登录」、「需代理复检」→「需翻墙」），前端 `splitSystemUser` 就认不出旧词，
    而它认不出时**不是显示原样，是降级成用户标签**——可编辑、可导出（实测 1051 + 98 条）。
    映射表放 `core/tags.py` 的 `RETIRED_STATUS_TAG_RENAMES`，与旧分组回读共用一份。
    - **换名 ≠ 重算**：迁移里**不要**顺手调 `rebuild_system_tags()`。它按 `checks` 表
      推导，而「从未校验」的源没有 checks 行，会被统一压成「待验证」，**抹掉导入时从
      旧分组推断出来的状态**。换名只换词；锁定行也照换（钉的是状态，不是词怎么写），
      而重建必须跳过锁定行。
    - **两档该不该合并，判据是「下一步动作是否相同」**（删/修/重测/翻墙/连 App 试），
      不是语义相近、更不是名字像不像。合并/改词都要 bump `CACHE_VERSION`。
    - **兜底档的名字必须是否定式的**：「异常」是肯定断言，会把一次请求都没发过的新源
      标成坏的。见 lessons §五十二。

18. **界面文案按术语表写；术语表与检查规则只有一份**（`tools/check_copy.py`），
    机检挂 `tests/test_copy.py`（跟全量测试一起跑），基线 `tools/copy_baseline.json`
    **只减不增**。**一个词只指一件事**：界面总称是「调试」——依据是**上游 App 自己
    就叫调试**（`strings.xml`：`debug_source`=调试源、`debug`=调试），本地那层叫
    「本地调试」、App 那层叫「连 App 调试 / App 实测」；不要另造词
    （「试跑」「本地回放」是自造或内部说法，已在批 1 清掉）。
    - **注释不是文案**：中文注释里的「回放/口径」是写给改代码的人看的（那里精确
      优先），机检会跳过注释与 `console.*`——**别为了过检把注释改口语**。
    - **永久例外写行尾 `# copy-ok: 理由`，不进基线**：基线里出现永久条目等于把
      闸门关掉。换词表里必须逐字保留的键就是这种（见 #17）。
    - 改文案**必须同时改钉住它的断言**（同 #12）。
    - 机检有**盲区**：它只抓表里有的词，兄弟文案用词不一致（一处「本地调试不了」、
      另一处「本地复盘」）抓不到。依据与前后对照见 lessons §四十六。

19. **内容被「摘」进主干的分支要立刻删掉。** 用 cherry-pick 或手工重做把分支内容
    落进 master 后，那个分支的提交**不在历史里**（两套对象，`git merge-base
    --is-ancestor` 返回假）；而落主干时往往带着适配（改过的串、重扫的基线），
    再 merge 那个分支，冲突解决会把适配顶回去——**生成物（如机检基线）被顶回去
    等于静默改了闸门口径**，看不出来。落完就 `git branch -D`；要备查的是提交信息
    与 lessons，不是那个分支。见 lessons §五十七。

20. **规则产出的 URL，发请求前必须剥掉尾部的 `,{json}` 选项**（`core.urls.rule_url`，
    先 `split_url_options` 再 `abs_url`）。选项装的是 **App 才懂的东西**
    （`webView` / `method` / `body`），带上它去请求**必然拿不到页面**：实测
    `.../1.html,{"webView":true}` 返回 **404**、同一个地址剥掉选项返回 **200**。
    危险不在请求失败，在于**归因会说反**——`_probe_content` 会写下
    「正文请求失败(status=404)」，读起来像源坏了，其实是我们拼错了地址，
    与 #4 是同一类错。凡是「规则 → 地址 → 发请求」这条链上的新调用点都要走它；
    `bookUrl`（46 条源带选项）/ `tocUrl`（60 条）/ `chapterUrl`（64 条）都在其中。
    见 lessons §五十八。

21. **取值规则末段是「属性名或取值动作」，不是选择器**（`_is_attr_or_action_name`）。
    Legado 的取值路径是 `getResultList` → 末段交给 `getResultLast`，那里除
    `text`/`textNodes`/`ownText`/`html`/`all` 外**一律**取属性。
    `title` / `style` / `label` **同时**是 HTML 标签名与常见属性名，一旦按标签判，
    `class.a@title` 就成了「在 a 里再选一个 title 元素」→ **恒取空**，
    而调用方看到的是「源没取到书名」，不是「工具不会算」（同 #4）。
    实测末段为 `title` 的取值规则约 170 条。**列表规则不走这条**：
    `getElements` 把每个 `@` 段都当选择器（`class.list@tag.a` 必须仍是选择器）。
    见 lessons §五十八。

## 上游 App 源码（查证用）

代码注释里大量 `Xxx.kt:行号` 指向「阅读」App 的源码。本地在
`D:\Documents\GitHub\legado-with-MD3`——**不是本仓库的依赖**，只在查证时读它。

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
    t = p.read_bytes().decode("utf-8")   # 字节进出：行尾原样保留
    # 唯一性断言 + 定位 + 修改
    # 全部断言通过后才写回
    p.write_bytes(t.encode("utf-8"))
    ' @
    $code | & .venv/Scripts/python.exe -

**写回一律走 `write_bytes`，不要用 `write_text`**：它在 Windows 上把 `\n` 写成
`\r\n`，改一个字就把整个文件的行尾翻掉，而 `git diff` 看不出来（两边都归一化），
只在 `git add` 时冒一句 warning。也可以直接用 `tools/apply_edits.py`（行级补丁、
保留原行尾、锚点不唯一就报错）。

详见 `skills/agent-write-safety/SKILL.md`。

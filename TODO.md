# 待办与二期候选

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）。可复用的机制与踩坑
> 写进 `skills/legado-source-lessons`，这里只留一行指针；**已完成的事项从这里删掉**，
> 别让它在这儿过期（这份文件反复订正过，根因就是分工没定）。
>
> ⚠️ 行号是 **2026-09-16 的快照**，代码一改就会漂，引用前先核对；
> 本文件**不写行号**的地方（如「见 `core/store.py` 的某函数」）按函数名找。
>
> **最近已修**（2026-09-16 ~ 09-18；机制见 `skills/legado-source-lessons`
> §十八/§十九/§二十七～§四十七，**细节看 `git log`，这里不再逐项列**）：
> 「导入从 O(n²) 降到线性」（3000 条 60s → 0.14s：逐条一次 SELECT + 一次事务，
> 且 `upsert_sources` 每次调用内部还全表扫一遍 `known_user_tags`）、
> `source_url` 的唯一性从「全表」放宽到「**仅在用**」（删除不再原地打标记挡住
> 导入；回收站可留同 URL 的多个历史版本）、回收站可**清空**（唯一硬删除路径，
> 先落快照）、「选中全部 N 条筛选结果」+ 批量删除改 POST body、
> 校验按行显示进度并可取消、校验结果报出相对上次的变化（`transitions`）、
> 缓存复用加「**验过搜索**」这一维、校验参数并入弹框并去掉工具栏独立按钮、
> 「试」出来的相对链接可直接打开（含 `/manhua/xxx/1.html` 这类根相对路径）、
> 调试 AI 区块文案按中文应用惯例重写（§四十六）。
> 并发下的两处随机 500（SQLite 连接跨线程 / 每个请求重建 schema）、后端直接托管
> 前端产物（含 Windows 的 MIME 与缓存头）、探测深度合并成一根四档轴、删除备份改单文件
> JSONL、`证书问题` 进系统标签表、reclassify 的证书分档、名称清洗全链路、
> "合并重复源"的 P0/P1、「登录墙」误判修正（裸 `login` 误伤 797 条）、`auth` 缓存
> 降到 1 天、列表「验证」列（深度 + 实测结果）与其排序口径对齐、跨页勾选、
> 任务详情带源地址、调试抽屉的「诊断」与「在页面上找目标」（候选规则）、
> 校验链路的三个「崩溃/失明」问题（fd 耗尽、进度卡住、超时与异常让源永远显示
> 「未校验」）、DNS 失败归因（域名注销 vs 解析被污染）、证书错误单独分档、
> 任务僵尸行与 `checks` 保留策略、CLI 归因与校验链路对齐、新增 `dups` 找重复源、
> 调试的页面缓存、修复循环的字段护栏（本地回放不了的不覆盖也不采纳）、
> 调试抽屉的 AI 提议（每条候选都过回放器）。
>
> 更早那批（类型判定自固化、`@html:` 前缀、`webView` / `enabledExplore` 的推导与存量
> 清理、`CACHE_VERSION` 8→9 等）同样以 lessons 与 git 历史为准——**这份清单只用来
> 回答"这条是不是刚做过"，不承载细节**（它原来每轮都在长，正是本节开头警告的那种
> 过期）。
>
> **已定决策：缓存不做向后兼容。** 它是纯派生数据，口径一变就整体作废
> （`CACHE_VERSION`）。做兼容反而危险——旧缓存里的 health 是旧逻辑的产物，混着用
> 就是「修了等于没修」（v7、v9 两次都是这个理由）。代价是每次口径变化要一次全量
> 重跑（约 3850 条），已知且接受。

---

## 明确该做，需排期

> 排序：**已知的误判**优先（它现在就在错判源）→ 需要实测的大改动 →
> 有触发条件的预防性工作 → 按需。

### 1. 目录验证的三个 bug（纯本地，不依赖 App；**先修 C**）

2026-09-18 查《SF轻小说》《中文书城》"调试能抓到正文、校验说不行"时挖出来的。
**三条全在 `_probe_toc` / 目录那一步**，且**互相独立**：

| # | bug | 证据 |
|---|---|---|
| **C** | **`text.` / `children.` 简写没实现，却判成「可回放」** | App 的 `AnalyzeByJSoup.kt:313-320`：`"text" -> temp.getElementsContainingOwnText(rules[1])`；我们落到 `else -> temp.select(...)` 那一支，必然为空。**而 `parse_rule` 报 `supported=True`**，于是报「解析结果为空」＝**判源失效**，不是"无法判定" |
| **B** | **`_probe_toc` 完全不用 `ruleBookInfo.tocUrl`** | 全库 **1617/3774（42.8%）**配了它。而 `tocUrl` 非空意味着目录在**独立页**上，详情页里根本没有章节列表 |
| **A** | **`bookUrl` 少了 bookList 作用域** | 实测 `book.sfacg.com`：现状把 `tag.a@href` 作用于**整页** → 解出 33 条（全是导航栏），取第一条 = `https://www.sfacg.com`（漫画首页）；按 Legado 语义先在 `tag.form@tag.table.-2@tag.ul` 节点内取 → `https://book.sfacg.com/Novel/249775`（对的书） |

**C 是最该先修的**：它是**确定性语法**（App 的语义就在眼前），而且 **B 修了也白修**——
用到 `tocUrl` 的源里，`text.查看完整目录@href` / `text.章节目录@href` 这种写法占绝大多数
（12.3% 的源用到 `text.`/`children.`，样例几乎全是 tocUrl）。

**修 C 时必须同时改判定**：`parse_rule` 对不认识的简写要报 `unknown`，
**不能再报 fail**——这正是 lessons §四十四 那条「把工具的欠缺说成源的问题」。

**附带**：`core/verify.py:238-240` 把 `tocUrl` 当 **URL 字符串**用
（`_abs_url(book_url, "text.点击阅读@href")` → 拼出垃圾地址），而它在 Legado 里是**规则**。

**每一步都会改变目录判定结果 → 每步都要 `CACHE_VERSION` 加一。**

### 2. 让 App 承担搜索档校验（`WS /searchBook`）

**动机**：本地回放覆盖不了全部语法（JS 448 条规则、模板 457 条、加上 §1-C 那类**我们没实现的**
——合计影响 1334 条源的深度验证）。而 App 里 **Rhino + JSoup + 完整规则引擎都在**，
我们正在**重新发明它已经有的东西**，还发明不全。

**依据（2026-09-18 读 `legado-with-MD3` 源码）**：`web/socket/BookSearchWebSocket.kt`
开着一个我们从未用过的 WS：

    ws://<App IP>:1123/searchBook
    输入   {"key": "<关键词>"}
    行为   SearchBooksUseCase.execute(keyword, scope=<App 的 SEARCH_SCOPE 偏好>,
                                      concurrency=<App 的线程数设置>)
    输出   流式推回每次**新命中**的 SearchBook：
           { bookUrl, origin ← 书源 URL, originName, name, author, ... }
    结束   "Search finish"

**一次连接、App 并发搜全部源、每条结果带 `origin`** → 直接得出"每条源的搜索通不通"，
且**一条语法都不用我们实现**。

**已从源码定下的两条**：

- **范围 = App 全部 `enabled` 源**（`SearchRepository.kt:96-115`：`scope.isAll -> allEnabledPart`，
  且 `selectedSources` 为空时兜底也是 `allEnabledPart`）。`SEARCH_SCOPE` 空串即 `isAll`。
  **但 scope 读自 App 偏好，`/searchBook` 不接受请求参数**——用户设过「搜索范围」就只能跟着那个范围。
- **App 里没有的源不会被搜**。要覆盖全库得先批量推——顺带纠正：**推送有批量接口**
  （`POST /saveBookSources` 吃 JSON 数组，`KtorServer.kt:58`），
  **一个请求就够**，不是 N 个。（同样有 `/getBookSources`、`/deleteBookSources` 批量；
  我们 `core/app_debug.py` 只用过单数的 `/saveBookSource`。）

**待实测（需要一台连着 App 的环境）**：

1. 3000+ 条一次搜索的实际耗时，以及会不会被 App 主动断连
2. `SearchBook` 推回来的真实 JSON：字段名、以及 **`origin` 是导入原文还是规范化过**——
   这决定能不能对上我们库里的键（lessons §五 记过 20.7% 的不一致）
3. 一条都搜不到的源，在流里是什么表现（不出现 / 别的 event）
4. App 里**未启用**的源是否被排除

**待设计**：**「没结果」的二义性**——源码里只推**新命中**，所以"某源没出现"
既可能是源坏了、也可能是它没这本书。本地 `_probe_search` 至少有"请求失败 vs 响应里没这个词"
的区分。可能的缓解：多用几个关键词，某源全不命中才判坏。

**分工建议（待实测后定稿）**：

| 档位 | 谁跑 | 理由 |
|---|---|---|
| 搜索 | **App**（`/searchBook`，一次连接） | App 有完整规则引擎；本地那 618 条判不了的直接归零 |
| 目录 / 正文 | 本地回放（并修 §1 的三个 bug） | App 的 `getChapterList` / `getBookContent` 是**书架维度**（`BookController.kt:132` 明写「未在数据库找到对应书籍，请先添加」），要用它得往用户书架加书——比推书源敏感 |

**明确不做**：不把 `getChapterList`/`getBookContent` 接进校验（动用户书架），
除非将来单独立项。
### 3. 修复循环：撞上登录墙要停下（不管它返回的是 200）

**2026-09-17 量过规模**（存活 3775 条）：`loginUrl` 非空 **491 条（13.0%）**、
`enabledCookieJar` 为真 **2004 条（53.1%）**；校验出的 `health=auth` 共 **1051 条**，
其中 **857 条状态码是 200**（域名探测口径）。

**问题的性质**：不在「验得对不对」，在「模型看到的是什么」。`build_evidence` 里
`ev["ok"] = bool(ev["pages"])` —— **抓到任意一页就算有证据**：

- 站点返回 403：`core/fetch.fetch` 直接抛（它不吞 HTTPError）→ 无页面 →
  修复循环提前退出 `no_evidence`，**不烧轮次** ✓
- 站点返回 **200 + 登录页 / 反爬挑战页**：抓得到 HTML，但那是登录页 →
  **模型拿登录页的 DOM 大纲去改正文规则**，盲改 3 轮 ✗

而 `core/verify.py` 里 `auth` / `login` **零命中**——登录墙的唯一判定表在
`core/checker.py:classify_http_status`，修复循环不用它。

**要做的动作**：`build_evidence` 每页复用 `checker.classify_http_status`
（**全仓唯一的判定表**，别新写一份；注意要传 `enabled_cookie_jar`——
`checker.py:152` 那档「200 + 登录词」以它为前提）；判成 AUTH → `ev["ok"]=False`
并打标记，`repair_one` 给独立 status、报告单独一栏。**不得静默变成「已经修好」**：
`quality.py:407` 记着这个坑——`bookList` 为空的源被判 unknown → `all_ok=True`
→ 修复循环永远不碰它。顺带把话术改对：JS 规则解析为空时说「本地无法回放」，
不说「搜索规则已失效」。

**2026-09-17 补的范围**：调试抽屉的 **AI 提议**同病——抓到登录页时模型看到的不是
App 那份（App 带登录态），提的建议既验不了也修不对，而且这次是**花了钱**才发现。
所以抽屉现在也用它：`core/checker.is_login_wall`（`classify_http_status` 的薄包装，
判定表仍然只有一份）判成登录墙就先把话说在前面、禁用「让 AI 提规则」。修复循环那边
（`build_evidence`）仍未接。

**已查过、不要重复查的**

- **App 侧解决不了这条**。调试只推文本、**不给 HTML**（`build_steps` 的
  `matched_html` 恒为空串），把验证换成 App 后模型看到的还是登录页 → 盲改照旧。
  且调试 WS 按 `bookSourceUrl` 精确匹配 App 库、`saveBookSource` 是 REPLACE，
  验证"提议"就得每轮覆盖用户 App 里的源，与 lessons §十四 的边界冲突。
- **Java 规则不是问题**。裸 `java.xxx` 会被回放器静默当成 CSS 选择器，但落在
  `verify_chain` 真正评估的那 5 个字段里的**只有 2 条**（大美书网 ×2）；其余
  1073 条在 `<js>` 里、149 条在 `{{}}` 里，都已被既有检测覆盖。

**何时做**：修复循环只有 CLI 入口，`repairs` 表只建表、无任何读写代码，`data/`
下也没有修复产物 → **无法证实它跑过，也没有证据表明已经造成过伤害**。所以这条是
**预防性的**：真跑批量修复之前必须先做，不跑就不占排期。

**规模参考**（按 `verify_chain` 真正评估的 5 个字段分类）：全可回放 2171 /
混合 1561 / 全不可回放 43（共 3775）。

### 4. 「合并重复源」剩 P2

**已实现**（2026-09-17）：`整理源` 抽屉（`TidyDrawer.vue`）第 1 步名称清洗、第 2 步
重复梳理与合并；后端 `GET /api/sources/dups`、`POST /api/sources/merge`（含 `dry_run`）、
`/merge/undo`；编排在 `services/merge_sources.py`（判据在后端**重算**，不信前端分组）。

**实测规模**（管理库，2026-09-17）：可合并 **369 组 / 782 条 / 可精简 413 条**
（同站点 + 行为指纹相同）。只读的三档：镜像 62 组、同域名 885 组、同名 407 组 ——
它们**只展示不动作**，`m.suixkan.com` 那种一个域名下 5 种源就是要人判断的例子。

**还没做**（P2 智能）：① B/C 档并排显示可用性辅助判断留哪条；② 「一键处理所有 A 档」
按建议批量应用；③ 「都留着并记住」（需要持久化，见下面的实现约束）。

**P2 的实现约束**（别重新纠结）：

- 「忽略这组」和「这组合并过」形状一样（都是对某一组做了个决定）→ 用**一张表**
  `dup_decisions(kind, key, decision, note, created_at)` 装，别塞 settings
  （`POST /api/settings/reset` 会把它们**静默清掉**，「用户对数据的决定」在本项目里
  一律存库）

口径与写路径（同站点 + 行为指纹、`tags_added` 只回真增、软删除放最后）见
lessons §三十四；「明确不做」的五条也在那一节（不自动合并、不按域名批量去重、
不改地址、不做逐组向导、不把忽略放设置）。


---

## P2 · 体验类，按需

- **前端暴露 `pick`**：搜索结果多条时选第 N 条重试（后端 `pick` 参数已支持；
  Legado 固定取第 0 条，见 `Debug.kt:285-288`）。
- 点击摘要里的变化数 → 按对应筛选跳转（需与统计条 chip 的筛选状态协同）
- 校验历史时间线（每次校验的前后对比，而不只是聚合数）——
  **依赖放宽 `checks` 的保留策略**：现在每源只留最近一条，时间线至少要留 N>1 条
  （改一个数字的事，但得先定 N 定多大、以及它带来的库增长），见 lessons §二十八
- `TrashDrawer` 的批量恢复对齐同一套 URL 语义（它仍是页级勾选 + 行对象，
  与列表页 `ec93ad4` 之后的 URL 语义不一致）

### 设置抽屉的「App 连接」页签：要么实现，要么撤掉那句承诺

`SettingsDrawer.vue` 那一页现在是空的，文案写着「尚未实现：App 的 IP / 调试端口 /
连接超时会在这一页配置」。**UI 已经向用户承诺了这件事**，所以它是一条真实的待办，
不是"以后有空再说"。

现状是散的：

| 项 | 现在在哪 |
|---|---|
| App 的 IP | `SourceEditDialog` 的输入框 + localStorage（`legado.appHost`） |
| 调试端口 1123 / HTTP 端口 1122 | `core/app_debug.py` 硬编码（`DEFAULT_DEBUG_PORT` / `DEFAULT_HTTP_PORT`） |
| 连接 10s / 收帧 20s / HTTP 8s | `core/app_debug.py` 硬编码（`CONNECT_TIMEOUT` / `RECV_TIMEOUT` / `HTTP_TIMEOUT`），**调试接口根本不接受这几个参数** |

两个方向，选一个：

- **实现**：IP / 端口提到设置里（IP 从 localStorage 迁过来，与「个人设置存库」的既有
  约定对齐——见「合并重复源」那节的约束），三个超时按需要暴露
- **撤承诺**：把那句文案改成说明现状（IP 在编辑弹窗里填、端口固定 1123），
  页签去掉或改成只读展示

**别让它一直是"点开只有一句话"的状态**——那比没有这一页更让人以为功能坏了。

### 可考虑：调试时写入验证缓存

方向可行，但有个前提：**试跑的是表单里当前的规则**，未保存时 `fingerprint` 与库里
不一致，缓存写了也用不上（`is_cache_item_valid` 要比 fingerprint）。所以只对
**已保存的源**有意义。口径上没问题——项目已做「判定收拢到 `core.quality`」。
时效已有：`cache_ttl_ok`(14天) / `cache_ttl_other`(7天) / `cache_ttl_auth`(1天)，
**都已在设置里可改**
（键在 `core/settings_store.DEFAULTS["check"]`；这里不写行号——它每轮都漂）。

---

## 需先调研（**不要直接动手**）

### A. 书源类型判定的判据改造

现状的六个问题：

| # | 问题 | 位置 |
|---|---|---|
| ① | **数学上判不出 1（音频）和 3（下载源）**——`infer_type_static` 只有 `2`/`0`/`-1` 三个出口 | `core/reclassify.py:121-127` |
| ④ | 已声明的 1 / 3 被完全无视——只处理 `declared == 2` 和 `declared == 0` | `core/reclassify.py:116-120` |
| ⑤ | `-1`（证据不足）的语义是「保持原样」，而多数源本来就是默认 `0` → **"保持原样"就是保持错误**；且没有任何路径能产出 `4`（❓未知） | `core/reclassify.py:127` |
| ⑥ | 根本问题：`bookSourceType` 决定的是**阅读器走哪条渲染路径**（`BookSourceType.kt` 的 `@IntDef` + `BookSourceExtensions.getBookType()`），而现有判据全是「域名/名称像什么」——**用内容线索猜渲染路径** | `core/reclassify.py:34-50` |

要做的两件事（**都需先调研**）：

| # | 推迟项 | 依据 | 卡在哪 |
|---|---|---|---|
| 1 | 补齐音频 / 下载源的静态出口 | ①④⑤ | 要产出 `1`/`3` 得先有信号 |
| 2 | 判据从「域名/名称」改为「**规则形状**」 | ⑥ | 同上，且见下面的实测 |

**第 2 项才是真正的解法**，思路是对齐 Legado 的 `Debug.kt:329-332`——用
`book.isWebFile`（由 `downloadUrls` 存在决定，`BookExtensions.kt:94`）判下载源，
**不是猜的**。

> ⚠️ **但这条思路现在搬不过来**：实测**管理库**（3861 条，2026-09-17 复测；
> 原来引用的 `data/candidates.json` 已经不存在了）里，**带 `downloadUrls` 的是 0 条**。
> 所以两项都必须**先做一次调研**：摸清 3861 条里到底存在哪些**能定案**的结构信号，
> 再谈判据换不换。
> **在此之前不要动 `infer_type_static` 的出口**——没有信号就加出口，只会把
> 「猜域名」换成「猜别的东西」。

`reclassify --write` 在那之前仍只能在 `0` / `2` 之间翻转。
推迟的代价：`organizer.group_title` 与 `store._system_group_for` 都用
`BOOK_SOURCE_TYPE_NAMES` 生成类型标签，所以类型判定不补齐，App 里的类型分类就会
一直带着 ①④⑤ 的偏差。当时提供的是「人主动纠正单条源」的通道，不是批量修正。

### B. **取值类**规则的末段语义差异（**290 条**，已定案）

**一句话**：Legado 对**列表类**规则和**取值类**规则用**两个不同的函数**，而我们对两者
用同一套解析器：

| 规则种类 | Legado 走 | 末段怎么解释 | 我们 |
|---|---|---|---|
| 列表（`chapterList` / `bookList`） | `getElements`（`BookChapterList.kt:203`） | **每个 @ 段都是选择器** | 一致 ✓ |
| 取值（`content` / `intro` / `author`…） | `getStringList` → `getResultList` → `getResultLast` | `when` 只认 text/textNodes/ownText/html/all，**其余当属性名**（`attr()`） | 当选择器 ✗ |

所以 `content: "id.content"` 这类「末段写裸选择器」的**取值**规则，Legado 取的是
`attr("id.content")` → **空**，而我们给出正文 → **误放**。

**实测范围**（全库 3861 条）：

    落在列表类字段 → 3151 条  **我们是对的，销案**
    落在取值类字段 →  290 条  ← 真正的对象
      content.content 41 / search.coverUrl 40 / bookinfo.author 38 /
      bookinfo.intro 22 / bookinfo.kind 20 / bookinfo.name 18 / …

> **2026-09-16 的两次订正，值得记下来**：
> ① 最初写成「3441 条」，是把列表类字段也算进去了——**没分清两个函数**。
> ② 中间一度被 App 实测（`chapterList = class.chapter-list@tag.a` **能出章节**）
>    推翻，降级成「待实测」。后来才发现那条走的是 `getElements`——
>    **反例与我读的函数不是同一个**，两件事都对。
>
> 教训：**「实测推翻了源码结论」时，先确认两边说的是不是同一个代码路径。**

**动作**：只在**取值类**字段上改（列表类一个字都不能动）。
**必须灰度**：会让这 290 条的对应字段由「有值」变「空」，可能连带影响星级。

### C. `ruleContent` 里我们**完全没建模**的字段

App 用它们改写正文（去广告、拼副文、二次解密图片），**我们一条都不用**——
这比 `webView` 更直接地构成「同源不同判」。

**2026-09-16 逐个核过源码，全部在用**（引用补全）：

| 字段 | App 在哪用 | 干什么 |
|---|---|---|
| `replaceRegex` | `BookContent.kt:190` | 正文替换 |
| `subContent` | `BookContent.kt:144` | 副文规则，拼在正文后（或取歌词） |
| `nextContentUrl` | `BookContent.kt:254` | 下一页（**这个我们已建模**） |
| `sourceRegex` | `WebBook.kt:430` → `AnalyzeUrl.kt` | 页面源码正则（WebView 分支） |
| `callBackJs` | `SourceCallBack.kt:53,92` | 事件回调 JS |
| `payAction` | `MangaReaderActionRepository.kt:237-244` | 漫画购买操作（evalJS） |
| `imageDecode` | `BookHelp.kt:367-389` | 图片 bytes 二次解密 |

**实测使用率**（2026-09-16，全库 3861 条）：

| 字段 | 非空 | 说明 |
|---|---|---|
| **`replaceRegex`** | **1024 条（26.5%）** | App 在正文提取**之后**做全文替换：`analyzeRule.getString(replaceRegex, contentStr)`（`BookContent.kt:191`），走的是**规则引擎**不是裸正则 |
| `title` | 41（1.1%） | 有些站只能在正文里取标题 |
| `sourceRegex` | 19（0.5%） | 键出现 667 次，绝大多数是空串 |
| `payAction` | 13（0.3%） | |
| `imageDecode` | 11（0.3%） | |
| `callBackJs` | 1 | |
| `subContent` | **0** | |

**优先看 `replaceRegex`**：26.5% 的覆盖面，但**风险方向单一**——我们判「非空即通过」，
只有「替换后变空」才会构成误放（替换多是去广告，正文仍在）。所以大概率是
**低风险、高覆盖**，值得评估但不必恐慌。其余几个都是个位数，按需。

**未评估前不要动手。**

---

> **已明确不做的项**（附当时砍掉的理由）在 `skills/legado-source-lessons` §二十六——
> 那是**决策记录**，不是待办。放这里会和「还没做」混成一片。

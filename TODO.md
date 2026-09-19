# 待办与二期候选

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）。可复用的机制与踩坑
> 写进 `skills/legado-source-lessons`，这里只留一行指针；**已完成的事项从这里删掉**。
>
> **已完成**（09-19，细节看 `git log` 与路线图 ✅ 行，机制见 lessons §四十八～§五十四）：
> S0 提交收尾、S1 JVM 搜索档全量（3774 条，环境缺口 0）、S2 接进产品（设置/自检/
> 列表 JVM 列）、S4 健康档位六档重设计（存量迁移一次做完）、回放边界判定、
> 死代码与文档清理。更早的以 lessons 与 git 历史为准。
>
> **已定决策：缓存不做向后兼容**（`CACHE_VERSION`）。它是纯派生数据，口径一变就整体
> 作废；做兼容反而危险——旧缓存里的结论是旧逻辑的产物，混着用就是「修了等于没修」。

---

## 路线图（排期）

> 判据：**先让主线跑起来，再往产品里接，最后清存量**。每阶段都能独立交付。

| 阶段 | 做什么 | 依赖 | 完成判据 |
|---|---|---|---|
| **S1 服务化·搜索档闭环** ✅（2026-09-19） | ValidateService（收源 → searchBookAwait → 结论 NDJSON）+ Launcher（参数走 `appservice/args.properties`）；(a) 剥 webView 选项 ✅；(a2) 壳归因 ✅（empty_js_shell） | S0 | **✅ 全量跑完**：3774 条 / 17 分钟 / 环境缺口 0；结论分布与归因细分见 lessons §五十三；剥 webView 救回 16 条 ok |
| **S2 接进产品** ✅（2026-09-19） | 设置项（App 源码目录 + JDK/SDK/gradle-home 自动推导）+ **自检接口/按钮** + 前端页签 + **列表 JVM 阶梯列**（不写 checks——JVM 与本地回放是两条证据，meta `jvm_check:<batch>:<url>` 每源每 URL 取最新批次） | S1 | ✅ 界面上能配、能自检、列表上能看见 JVM 结论（tooltip 给命中率与批次） |
| **S3 补深度** ✅（2026-09-19） | 目录段 + 正文段 + **(b) 浏览器桥**——四批全完成 | S2 ✅ | ✅ 全链路可跑（搜索/目录/正文三段 + 浏览器渲染复验）；webView 源不再是盲区 |
| **S4 清存量** ✅（2026-09-19） | Health 档位重设计 + 分类侧可行动性（同一件事的两面，必须一起做） | 可与 S3 并行 | **✅ 9 档 + 未校验 → 6 档**（ok/auth/gfw/cert/pending/dead，判据：下一步动作相同）；「未校验」降为派生筛选（`health=none`）；CACHE_VERSION 13；存量映射 + 组名换词（lessons §五十二）。**残留**：unknown 的产品出口、标签快照过期 → 见 §四 |
| **S5 按需** | 真机复检通道（只读那条先做）、本地回放的 `bookUrl` 作用域、体验类 P2 | —— | —— |

> ⚠️ **CACHE_VERSION 合并 bump**：S3/S4/S5 里凡是翻转判定结论的改动，**做完一批
> 再 bump 一次**、付一次全量重跑，别每步付一次。
> （S4 已 bump 到 **13**——档位词表变了，旧缓存的 health 是旧词表的产物；
> 12 是回放边界判定那批。）

---

## 一、JVM 校验服务（S1/S2 已落地；机制与环境坑全在 lessons §四十八～§五十三）

**现状**：搜索档全量已跑完（3774 条，结论分布与归因口径见 lessons §五十三）；
设置/自检/跑批/列表阶梯列都在界面上（S2）。探针故事、零入侵挂载、环境一次性成本、
四个工具链坑——**全部已在 lessons §四十八/§四十八点五，此处不再重复**。

**换机器 / 新用户上手清单**（操作性内容，唯一留档处）：

    前置：App 仓库（github 本地克隆）+ JDK 21 + Android SDK + Gradle 缓存（同盘）
    路径只填一个：设置 → 「JVM 校验」页签 → App 源码目录；JDK/SDK/gradle-home 自动推导
    先点「自检」：四项全绿才能跑批（缺 SDK/Gradle 时首次编译十几分钟，是正常现象）
    跑批形态：一次性调用（后端 subprocess 调 appservice/legado-gradle.bat，跑完退出）

**来源阶梯与产品定位（已定，不再展开论证）**：

- 每源结论带来源阶梯：**本地回放 < JVM(App 引擎) < 真机(App + 真实环境)**；
  JVM 结论存 meta（`jvm_check:<batch>:<url>`），**不写 checks**——与本地回放是
  两条证据，混在一个字段里就分不出谁说的。
- 三条通道各管一段：**校验/调试归 JVM**（调试那条它能交回 HTML，设备 WS 给不了）、
  **真机只做复检**（登录墙 / WebView 依赖 / 用户网络出口，见 §二）。
  设置抽屉的「App 连接」页签已改为复检通道占位。

**S3 之后悬着的两个设计**（都不阻塞 S3，做真机通道或结论互通时再回来）：

- **「没结果」二义性**：JVM 搜索对单关键词跑，某源没结果可能是「没这本书」——
  缓解靠多词复核（全不命中才判坏），落地点在做 `no_result` 复核批次时。
- **JVM 结论要不要反向喂给本地口径**（health/stars 的映射与来源标注）：
  那是「结论互通」的事，S4 已把 health 侧收干净（六档），等 S3 三段结论稳定后再议，
  **动本地口径才涉及 CACHE_VERSION**。

## 一点五、S3 动手计划（2026-09-19 定；下一个动手的就是它）

**目标**：JVM 校验从「搜索档」扩到「目录 + 正文」全链路，并用本机浏览器把 JS 壳源
从盲区里捞出来。**复用 S1/S2 的全部骨架**（ValidateService、launcher、args.properties、
meta 落库、列表阶梯列）——S3 只往里加深度，不另起炉灶。

**四个上游接口已核过签名**（`WebBook.kt`，App 仓库当前版本）：
`searchBookAwait(source, keyword)` → `getBookInfoAwait(source, book, canReName=true): Book`
→ `getChapterListAwait(source, book, runPerJs=false): Result<List<BookChapter>>` →
`getContentAwait(source, book, chapter, nextChapterUrl, needSave=false): String`。
链路形态照抄 `Debug.kt` 的 `searchDebug → infoDebug → tocDebug → contentDebug`：
**book 由搜索第一条结果来**（`toBook()`），`book.tocUrl` 由详情段填（空则回退 bookUrl），
正文段的 `nextChapterUrl` 取目录第二条（Debug.kt:353 的口径）。**needSave 必须 false**
——不落 App 数据库，与零入侵边界一致（`getContentAwait` 内部 `needSave=true` 会走
`BookHelp` 落盘缓存）。

**分四批，每批独立可验、独立提交**：

| 批 | 做什么 | 验收判据 |
|---|---|---|
| **S3-1 目录段** ✅（2026-09-19） | `depth` 参数（search/toc）+ `stage` 字段 + `runTocStage`（getBookInfoAwait → getChapterListAwait，链路照抄 Debug.kt）+ 结论字段 `toc_count`/`toc_raw_count`/`toc_sample`/`toc_complete`/`book_url`/`toc_url`；顺手修两处：源无搜索规则不再报 error（是能力事实不是网络失败）、**Koin 网关一次补齐 7 个**（目录段触发 AppLog → OtherSettingsGateway 缺定义，12 条里 7 条栽在这） | ✅ 100 条抽样：95 条跑到目录段（SF轻小说 1201 章 / 听书 1251 / 全本 433，**章数经独立抓页验证属实**）。⚠️ **原判据（与本地矛盾率 <15%）作废**——两边不是同一把尺，74.7% 里混着口径差异与本地实现缺陷，见 lessons §二十三「第二次实证」 |
| **S3-2 正文段** ✅（2026-09-19） | `depth=content`：目录第 1 章（`nextChapterUrl` 取第 2 章，Debug.kt:353 口径）→ `getContentAwait(needSave=false)`；结论带 `content_len`/`content_ok`/`content_sample`/`chapter_title`/`chapter_url`。判定**语义对齐** `core.quality.judge_content`（非空即通过 + 类型分流；跨语言不共享代码）。**TocStage 带着 book/chapters 走**——正文段若重新搜一次，可能命中的不是同一本书 | ✅ 100 条：97 条跑到正文段，78 条有正文（中位数 **2569 字**，p10 890 / p90 5502）；独立抓页自证（溜达小说：页面 `#content` 4203 字 vs JVM 报 4136，开头逐字一致）。**跑批暴露三处结论质量问题，均已修**：① 引擎把异常消息当正文返回（塔读系 4 条，`java.ajax` 抛 `IllegalArgumentException` → 「非空即通过」判它 ok = 假通过）；② `ContentEmptyException`/`TocEmptyException` 被归成 `error` 混进网络桶（其实是源级空）；③ 超时文案谎报「每源总预算用尽」——实测是 App 内部的 **60s 读超时**（`HttpHelper`），故 `timeout` 要 >60s 才有区分度 |
| **S3-3 结论落库 + 界面** ✅（2026-09-19） | 设置加 `jvm.depth`（枚举 `JVM_DEPTHS` 只在 `settings_store` 定义，前端经 `limits.jvm_depth` 拿选项）；`args.properties` 透传 depth；**「取最深一条」收进 `Store.latest_jvm_conclusions`**（深者胜、同深取新）——`/api/jvm/results` 与列表回填**共用这一份实现**（原来各写一遍，正是 lessons §二十三 那个坑）；列表新增 `jvm_stage`/`jvm_toc_count`/`jvm_toc_ok`/`jvm_content_len`/`jvm_content_ok` | ✅ 端到端验证：tooltip 实测渲染「搜索：命中 10 本 ✓ / 目录：432 章 ✓ / 正文：2583 字 ✓」；设置面板「校验深度」三档（标签改名以免与本地回放的「探测深度」同名不同物）；5 个新测试钉住取舍规则（含「后跑的浅批次不许盖掉先跑的深结论」） |
| **S3-4 浏览器桥 (b)** ✅（2026-09-19） | `BrowserBridge`（CDP over WebSocket，复用启动时那个页签、串行渲染）：Edge/Chrome → headless + 专用 user-data-dir → `Page.navigate` → `Runtime.evaluate(outerHTML)`。**不需要 shadow WebView**——`BackstageWebView` 只替换取数那一步，而 App 的解析层都收 `body: String?`，所以渲染结果直接喂 `BookList.analyzeBookList`（链路照抄 `searchBookAwait`，只换 body）。只对 `empty_js_shell` 的源复验，结论带 `rendered`/`render_reason` | ✅ 10 条空壳源：**3 条救回 ok**（hit 15/20/100），**10/10 拿到 `rendered=True`**——剩下 7 条不再是「我们没看」，而是「用真浏览器看过了、页面确实没结果」（归 (c) 真机兜底）。⚠️ 计划里「≤3 条剩下」的判据**没达到**：改判据的是渲染证据，不是救回数 |

**关键约束（动手时重读）**：

- **零入侵边界不放松**：shadow 只在测试编译里挂（`@Config(shadows=[])` 加一行），
  App 源码一行不动；跑完每批都验 App 仓库 `git status` 为空。
- **结论带 stage、六态不新造、`needSave=false`、每源总预算**——机制与理由见
  lessons §五十四，此处只留一句：目录/正文段失败落 `no_result`/`error`/`timeout`
  带 `stage: "toc"/"content"`，不往 App 数据库写任何东西。
- **CACHE_VERSION 不动**：JVM 结论在 meta，不在探测缓存；本地回放的缓存口径没变。
  若之后本地回放也复用 JVM 的目录/正文结论，再议 bump。
- **并发与超时沿用现有参数**：目录+正文比搜索慢 2-4 倍，全量跑之前先用 `--limit 200`
  试跑估时。
- **浏览器桥是最后一批**：它依赖 (a2) 的空壳名单当输入，且 CDP 会话管理（启动/复用/
  崩溃回收）是新的失败面——单独一批，出问题不拖累前三批的结论。

**风险与预案**：

| 风险 | 预案 |
|---|---|
| 目录段触发 `startBrowserAwait` 类规则（正文分页常带 JS） | S3-1/2 先不接浏览器，这类源落 `empty_js_shell`（stage: toc/content），等 S3-4 兜住——**不冤枉优先于验完** |
| 正文段某些源要下载图片/分页 N 次，单源拖全场 | `content` 档只验第 1 章 + `nextChapterUrl` 探测；`withTimeout` 是硬上限，超时落 `timeout`（stage: content） |
| CDP 端口冲突 / Edge 被占用 | 每批起独立端口（9223 起递增）+ 专用 profile；进程退出时 `taskkill` 兜底回收 |
| 渲染后仍空（真死源 vs 需要交互） | 渲染后规则仍跑不出 → 维持 `empty_js_shell` 但加 `rendered: true` 标注，与「没渲染过」区分——这批归 (c) 真机兜底 |


## 二、真机复检通道（原「让 App 承担校验」的归宿）

**定位（2026-09-19）**：不再是校验主力——**主线是 JVM 服务**。真机只在三类上
不可替代，保留为**用户主动触发的「复检」**：① **登录墙**（`loginUrl` 491 /
`enabledCookieJar` 2004 / 判出 auth 1051——JVM 无 cookie，这批在手机上往往好用）；
② **WebView 依赖**（JVM 结构性缺浏览器，S3-4 浏览器桥能兜一部分）；③ **用户自己的
网络出口**（IP / 代理 / DNS 与电脑不同）。

**回推 vs 收割**：**优先做只读的那条**——`WS /searchBook` 搜 App 自己的库 + 读回
App 自带的校验结果，**一行 App 数据都不写**。回推（备份 → `/saveBookSources` 推 →
推回备份）唯一的真损失是「同 URL 两边版本不同」，设备兼职日常使用才保留。
接口能力清单、实测数据形状、备份/还原判据——**全在 lessons §五十一**。

**怎么做一次复检**（脚本已删，步骤留档；协议细节见 lessons §五十一）：

1. 设备与电脑同网段，App 开 Web 服务（HTTP 1122 / WS 1123）；**先 `GET /getBookSources`
   备份落盘**——回推路线靠它还原。
2. 只读复检：每个关键词开**一条新连接**（`Finished` 即 close）打 `/searchBook`，
   对账按 `origin` **去重数源**、两侧都过宽松归一（`strip` + 去尾斜杠）。
3. 要读回 App 自带的校验结果：让用户在 App 里点一次「校验」，再 `GET /getBookSources`
   读分组标签 / `respondTime` / `// Error:`——**读回后立刻映射入库，再让 organizer
   重建分组**（分组是它的地盘）。
4. 回推路线额外两步：`POST /saveBookSources` 推我们的源 → 跑完推回备份 →
   `GET /getBookSources` 与第 1 步对拍。

**判定**：真机结论的**证据等级最高**（真引擎 + 真环境 + 真 cookie），落 `checks` 时要
按来源阶梯标注（本地回放 < JVM < 真机），别和另外两条混成一个数。

## 三、本地回放：剩下的一件 ✅（2026-09-19 已完成，见下）

**`bookUrl` 的 bookList 作用域已补**（旧称 §1-A），并且发现它是**一类**问题：
列表字段（`bookUrl` / `chapterUrl`）在 Legado 里是**在列表节点内**求值的
（`BookChapterList.kt` 的 `elements.forEachIndexed { setContent(item) }`），
而对整页求值时 `tag.a.0@href` 先命中导航栏 → 详情页变成站点首页 → 目录/正文全错。
修法：`replayer.extract_field_in_nodes`（一个实现），checker 与 verify 都调它；
配套修了目录页 `tocUrl` 那条（checker 原来完全不看 tocUrl）。
实测：溜达小说 0→1914 章、穿越小说 0→1664 章、必去网站 0→1491 章，
SF轻小说从「False＝源坏了」翻成 `None`（诚实的 `{{}}` 能力边界）。
CACHE_VERSION 13→14，**全量已重跑**（depth 4，3774 条 / 8 分钟 / 写库失败 0）：
113 条源从「0 章」翻成有真实章节（中位 398 章、合计 11.9 万章）；
28 条反向变化全部归因清楚（19 条是这一轮限流没走到目录段、8 条是旧实现在整页
瞎选到一个 URL 的"假成功"、8 条是 tocUrl 回放不了改成如实判 unknown）。
跑前快照与库备份留在 `data/app_probe/check_snapshot_*.json` 与 `data/backups/`。

**已修的三条**（别重复查）：`bookUrl`/`chapterUrl` 的**列表作用域**、
`text.`/`children.` 简写的**判定半边**（一律 unknown）、`_probe_toc` 接入 `tocUrl`
（含 `verify.py` 把 tocUrl 当 URL 字符串的附带 bug）——`git log`、
lessons §二十三「第二次实证」与 §四十四。

**还值得做的**（按收益排，**全部是「能被验到」而非「不被冤枉」**——冤枉那半已修完）：

| 写法 | 源数 | 说明 |
|---|---|---|
| **排除索引 `li!0` / `dd!0:1:2`** | **589** | 最大一块；`findIndexSet` 里 `.` 与 `!` 是**相反语义**的分隔符，我们只实现了点式 |
| 执行期兜底的「选择器解析不了」 | 257 | 样例是 URL 模板、JSONPath 片段、碎片——**还没逐个归类** |
| JSONPath 超出子集（`[1:3]` 切片等） | 163 | `[*]` 与 `['键']` 已支持 |
| `//` 开头的 XPath | 122 | 要引入 XPath 引擎，或者一直 unknown |
| 方括号索引式 `[-1]` / `[0]` / `[1,3]` | 117 | |
| 区间索引 `[0:10]` | 37 | |
| `text.` / `children.` 简写 | 16 | 语义已查清，实现即可 |

> ⚠️ **这一节整体优先级下调**：本地回放现在的定位是「App 不在场的快速筛选 + 调试/修复
> 需要 HTML 的那条线」。等 JVM 服务上线后，上面这些「本地验不了」的语法**由服务兜住**，
> 只有还留在交互路径（调试抽屉 / AI 修复）的语义才急。**做之前先确认它还在不在那条路径上。**

## 四、分类侧可行动性（档位重设计已完成，剩呈现与出口）

> **档位重设计 2026-09-19 已完成**（9 档 + 未校验 → 6 档：ok / auth / gfw / cert /
> pending / dead，判据是「下一步动作是否相同」；timeout、error、no_search、skipped
> 并入 pending）。这一节只剩下**呈现与出口那半边**。

- **DONE（当时的方向：至少把「从未校验 / 校验了没结论 / 网络性失败」分开）**：
  「从未校验」已分开——它不是一档状态而是数据缺失，统计条上单独一个灰 chip、
  筛选取值 `health=none`。**「验了没结论」与「网络性失败」则刻意不再分档**：
  两者下一步动作相同（重跑一次校验），失败原因留在 `checks.error` 与详情里，
  按动作分档的原则不该为它们再开两档（当时的判据见 lessons §五十二）。

<details>
<summary>重设计前的实测分布与判据（2026-09-19 定稿时留档）</summary>

**实测分布**（存活 3774 条，最近一条 `checks`）：ok 1680 / auth 1051 / timeout 711 /
dead 173 / gfw 98 / error 61 —— 而 **cert 0、no_search 0、skipped ≈0**。

三档的实情（都核过代码）：

| 档 | 实情 |
|---|---|
| `no_search` | **全仓没有任何赋值点**（只有 `checker.evaluate_stars` 的「可达集合」在期待它）——一条都不可能产出 |
| `cert` | 有产出点（证书错误），但当时 **0 条**；它是 2026-09-15 才加的，属「刚埋下」 |
| `skipped` | 不是判定结果：它是 `models.BookSourceRecord.health` 的**默认值**和 `.get(..., SKIPPED)` 的兜底，实际只有「源被禁用」这一种来源 |

**判据**：两档该不该合并，看**下一步动作是否相同**——删 / 修 / 重测 / 翻墙 / 连 App 试。
动作相同的两档，用户分不出来也不该让他分。

</details>
- **unknown 缺产品出口**：界面上只有解释文案，没有转化动作（如「连 App 验一次」）——
  归宿是真机复检通道（§二，S5）。
- **标签是快照，导入即开始过期**：写进 App 的分组标签是校验瞬间的结论，App 侧
  无刷新通道；叠加 ok 档 14 天 TTL，最坏差两周以上。要动它属于大改（反向同步
  通道），先记录不排期。

## 五、「合并重复源」剩 P2


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


## 六、体验类（按需）

- **前端暴露 `pick`**：搜索结果多条时选第 N 条重试（后端 `pick` 参数已支持；
  Legado 固定取第 0 条，见 `Debug.kt:285-288`）。
- 点击摘要里的变化数 → 按对应筛选跳转（需与统计条 chip 的筛选状态协同）
- 校验历史时间线（每次校验的前后对比，而不只是聚合数）——
  **依赖放宽 `checks` 的保留策略**：现在每源只留最近一条，时间线至少要留 N>1 条
  （改一个数字的事，但得先定 N 定多大、以及它带来的库增长），见 lessons §二十八
- `TrashDrawer` 的批量恢复对齐同一套 URL 语义（它仍是页级勾选 + 行对象，
  与列表页 `ec93ad4` 之后的 URL 语义不一致）
- **调试抽屉的「试跑规则」只对已保存的源有意义**：试跑用的是表单里当前的规则，
  未保存时 `fingerprint` 与库里不一致，缓存写了也用不上（`is_cache_item_valid`
  要比 fingerprint）。时效已可改（`cache_ttl_*` 在设置里）——这条只是边界说明，
  不是缺陷。


## 七、需先调研（**不要直接动手**）

两块调研证据与实测数据都在 lessons §五十五，这里只留动作与卡点：

**调研一：类型判定补齐**——`infer_type_static` 判不出音频/下载源（出口只有 2/0/-1），
真正的解法是 `book.isWebFile`（`downloadUrls` 决定），但实测库里带它的 **0 条**。
**先摸清 3861 条里有哪些能定案的结构信号，再谈判据换不换**；在那之前不要动出口
（没有信号就加出口 = 把「猜域名」换成「猜别的东西」）。换判据时顺手拿掉
「被审对象给自己投票」（declared 参与计分，AGENTS #11）。
`reclassify --write` 在那之前仍只能在 0/2 之间翻转。

**调研二：正文后处理字段**（`replaceRegex` 26.5% 覆盖、其余个位数）——
App 用、我们不用，构成「同源不同判」。`replaceRegex` 风险方向单一（替换后变空
才误放），低风险高覆盖。**未评估前不要动手**。

---

## 已明确不做（决策记录）

**在 `skills/legado-source-lessons` §二十六**——那是决策记录，不是待办。放这里会和
「还没做」混成一片。本节只留一句：**一/五/六/二/(A) 那批 App 路线方案、逐源调试 WS
批量化、本地补 JS、XPath 引擎、星级收紧等，都已在那里记了理由。**

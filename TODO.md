# TODO

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）。可复用的机制与踩坑写进
> `skills/legado-source-lessons`，这里只留一行指针；**已完成的事项从这里删掉**
> ——细节看 `git log`，机制看 lessons（AGENTS #10）。
>
> **结构由机检守着**：五档状态、四段分区、必填字段、P0 上限四条在
> `tests/test_todo_structure.py`（跟全量测试一起跑）。手改本文件前先看它。
>
> **历史批次（S1–S4、文案批 1、S5-A 的 A1–A4）不在这里复述**——机制看 lessons，
> 细节看 `git log`。本文件只留：还没做的 + 动手前的判据。

条目格式：`### 条目：<ID> · <标题>`，随后是行首锚定的字段行（值可续行，续行缩进 2 空格）。
必填 `状态：` `依赖：` `优先级：` `背景：` `约束：` `验收：` `指针：`；`open` 另须
`子项：`，`blocked` 另须 `阻塞于：`。指针只接受**路径 / `lessons §节` / git commit**。

---

## 0 · 现在做

> 当前排期：引擎与调试执行链的基础设施已完成；后续调试体验以“入口降噪 → 工作台闭环 → 证据前置 → 编辑能力合入 → DOM 意图化生成”为主线。worker 线按租约、专用 worker、灰度切换推进；批量吞吐和请求合并只在有实测收益时启动。
> P0 的 unknown 出口与可执行提示已交付，当前前端待办集中在新调试页的视觉层级、编辑闭环和证据可信度。

---

## 1 · 排队

### 条目：jvm-request-coalesce · 合并重复的进行中请求
状态：blocked
依赖：无
优先级：P2
背景：同一源、同一规则快照和同一 JVM 参数可能由调试抽屉、生成后验证和批量入口重复提交；单纯排队只能延后重复工作，不能减少请求和站点压力。但它不是当前“重启后单条校验慢”的根因，必须等 worker 和分块边界稳定、且有重复提交数据后再决定是否实现。
约束：合并键必须包含归一化 URL、规则/源快照、阶段、验证深度、搜索词及本次运行参数；不能只按 URL 合并。只合并仍在运行或可复用的同口径任务，每个调用方仍有自己的 job 观察关系；取消一个观察者不能取消共享执行，除非没有观察者且明确执行取消。
验收：只有在日志证明重复提交达到值得优化的数量后才实施；实施时完全相同的重复提交只产生一次 JVM 执行和一份底层结果，任一调用方都能收到同一结论及来源，任一合并键字段变化都会产生独立执行，旧结果不会静默复用。
阻塞于：先统计重复提交率；没有数据证明收益前不实现。
指针：backend/jobs/runner.py，backend/api/jvm.py，core/jvm_debug.py，AGENTS.md #5b，lessons §五十三 / §七十八

### 条目：jvm-worker-lease · 给跨进程 worker 增加 SQLite 租约
状态：todo
依赖：无
优先级：P1
背景：当前 lane、`RUN_LOCK` 和执行中的 asyncio task 都是进程内状态；直接增加 API/uvicorn worker 会让不同进程各自认为自己拿到了 JVM，现有 jobs 表也没有 worker owner/generation，无法安全认领和恢复任务。它解决的是跨进程一致性，不与调度策略合并。
约束：以 SQLite 原子认领为跨进程事实来源，至少记录 owner、generation、heartbeat、attempt 和运行目录；同一 job/块只能有一个有效 owner。认领、续租、完成和失败必须校验 owner/generation，旧 owner 不能覆盖新结果。进程启动、优雅停止和异常退出都要有明确回收/重试规则，不能用一次固定超时把源判坏；运行 manifest 与结果文件必须按 job/块隔离。
验收：启动两个 worker 并发抢同一 job/块时只有一个成功；杀掉 owner 后任务能按规则恢复且不覆盖已完成结果；旧 owner 延迟回写会被拒绝；重启后 jobs、租约、运行目录和结果状态能逐项对账。
指针：core/store.py，backend/jobs/runner.py，core/jvm_debug.py，lessons §二十八 / §六十五 / §七十四

### 条目：proj-3-drop · 前端摘掉本地投影
状态：todo
依赖：proj-3-bookurl, proj-3-attr
优先级：P1
背景：`replayResult` / `canReplay` / `doReplay` / `/replay-step` 那条链是
  「引擎没覆盖的段」的退路；摘之前先定详情段（proj-3-bookurl）与末段属性名
  （proj-3-attr）两处，否则那两段的「命中源码」会空掉。
约束：等那两条拍板落地才动手；「每种空值有可执行的一句话」前置已落地。
  摘的位置与依据在 `frontend/src/components/RuleDebugDrawer.vue` 的
  `matchedFrom` / `matchedHint` 旁。
验收：摘掉后抽屉里每个段的「命中源码」仍能取到值，或明确显示「本段取不到」。
指针：lessons §七十三 / §七十五，frontend/src/components/RuleDebugDrawer.vue

## 2 · 按需

### 条目：jvm-worker-roadmap · JVM 调试与校验的推荐拆分路线
状态：open
依赖：无
优先级：P1
背景：当前单后端 + 单 JVM lane 已解决同一进程内的参数、profile 和输出互相覆盖问题；直接增加多个 API/uvicorn 服务会复制进程内锁与调度状态，不能自然获得安全并发。更合适的边界是保留一个 API 入口，先把 runtime snapshot 和任务 manifest 固定下来，再修复单条校验冷启动，随后优化批量粒度，最后拆两个职责单一的 JVM worker。
约束：runtime snapshot、task manifest、single fast path、环境收尾、调度公平和批量分块已完成；后续只按 ① SQLite job/块租约取代跨进程内存锁；②建立交互调试 worker 和批量校验 worker，各自独占 JVM daemon、浏览器 profile、参数文件、运行根目录和 worker 身份；③灰度切换并逐字段对账；④只有实测证明重复执行或多 worker 有收益时，才做请求合并或多开 JVM。任何阶段都不直接横向复制 API 服务，也不共享 `args.properties`、daemon info、cookie/profile 或固定输出文件。
验收：路线的每个阶段都有独立可回退结果；单条校验的 daemon 与 Gradle fallback 逐字段一致；双 worker 能并行消费不同职责的任务，任务可恢复、可对账且不重复执行；调试延迟、批量吞吐、重复率和资源成本均有实测依据；与当前单进程基线逐字段对账通过后，才允许切换默认执行路径。
子项：
- jvm-scheduler-policy
- jvm-batch-chunk
- jvm-worker-lease
- jvm-request-coalesce（重复率达标后再做）
- jvm-debug-worker
- jvm-validate-worker
- perf-jvm
- jvm-worker-cutover
指针：backend/jobs/runner.py，core/store.py，core/jvm_debug.py，lessons §二十八 / §四十七 / §六十五 / §六十六 / §六十八

### 条目：site-req-opt · 站点请求那一段的优化（都未评估）
状态：todo
依赖：无
优先级：P2
背景：四个候选动作——① 重放优先于重跑（用 App 真看到的那份 HTML 本地重放）；
  ② 给 OkHttp 装 Cache（重复抓同一页第二条起秒回，必须配「忽略缓存重抓」）；
  ③ 分段重跑再把上一轮的 book/chapter 传回去；④ 别把 `fetch_debug_pages` 的
  补抓算进调试耗时（记账口径）。
约束：**先做哪个要看「编辑循环里重复抓同一页的比例」——没量过，别先动手**。
验收：先给出那个比例的一次实测，再按结果选一条动手，并给出改动前后的对比。
指针：lessons §七十五，core/jvm_debug.py

### 条目：s5a-a32 · S5-A3-2 表单登录入口
状态：todo
依赖：无
优先级：P2
背景：`loginUi` 非空那类（弹表单、`loginUrl` 的 JS 在 Rhino 里登录）能力上可行；
  覆盖范围见 lessons §六十三，别在 TODO 里抄一份会过期的条数。
约束：不代用户登录（lessons §六十三 的纪律）。
验收：一条带 `loginUi` 的源能登录并跑通一次调试。
指针：lessons §六十三，core/jvm_debug.py

### 条目：jvm-validate-worker · 批量校验专用常驻 worker
状态：todo
依赖：jvm-worker-lease
优先级：P1
背景：批量校验需要独立于交互调试的常驻 worker，省掉频繁小批次的 Gradle + JVM 拉起；它与交互调试 worker 分开，避免长批量占住交互请求，也避免两个用途共享 profile、cookie、args 或输出。本条承接原 `s5a-d3`，不再单独维护一条重复的“跑批常驻”路线。
约束：**「频繁跑全量会被封 IP」是硬约束**，比任何提速优化都优先（skills/legado-source-toolchain §四）。批量 worker 必须拥有独立 JVM、浏览器 profile、参数文件、运行根目录和优雅停止/重启流程；内部并发只能在分块与资源测量后设置，不能把一个常驻 JVM 当成无限并发池。结果必须保留与一次性运行同样的 stage、来源、原因和逐条可恢复性；未证明恢复、隔离和逐字段一致前，不切默认路径。
验收：在同一批次、同一参数和同一快照下，对比一次性运行与常驻 worker 的逐字段结果；中途杀掉 worker 后按租约恢复且不重复已完成块；并验证调试 worker 能在批量 worker 工作时独立接收请求，两个 worker 不读写对方的 profile、args、运行目录或结果文件。
指针：lessons §六十六 / §六十八 / §七十四，core/jvm_debug.py

### 条目：s5a-a1 · A1 唯一没做完的验收：与设备 WS 逐事件对拍
状态：todo
依赖：无
优先级：P2
背景：结构同构已由契约测试保证；逐事件对拍需要手机开着 Web 服务，等设备在场时补。
约束：对拍要逐事件，不接受「结构同构」代替。
验收：设备在场时跑一次逐事件对拍，差异逐条有归因。
指针：lessons §五十一，tests/test_jvm_debug_contract.py

### 条目：S5B-real · S5-B 真机复检通道（设备在场才做）
状态：todo
依赖：无
优先级：P2
背景：真机只在三类上不可替代（登录墙 / WebView 依赖 / 用户自己的网络出口），是
  **用户主动触发的「复检」**，不是校验主力。接口能力与协议细节见 lessons §五十一。
约束：**优先做只读的那条**（`WS /searchBook` + 读回 App 自带结论，一行 App 数据
  都不写）；回推路线只在设备兼职日常使用时才保留。判定时真机结论证据等级最高，
  落 `checks` 要按来源阶梯标注，别和另外两条混成一个数。别抄会过期的计数。
验收：只读路线跑通——每个关键词开一条新连接打 `/searchBook`，按 `origin` 去重数源，
  两侧都过宽松归一后对账一致；要读回 App 自带结论时，读回后立刻映射入库再让
  organizer 重建分组。
指针：lessons §五十一，core/app_debug.py

### 条目：perf-conc · 并发上调实测
状态：todo
依赖：无
优先级：P2
背景：`ValidateService.validateBatch` 已是 `Semaphore(concurrency)`；上调一档实测
  再决定值不值。
约束：受「频繁跑全量会被封 IP」这条硬约束，等下次真要跑量时顺带量（skills/legado-source-toolchain §四）。
验收：给出上调前后的一档实测对比（时长与失败率）。
指针：lessons §七十四，appservice/ValidateService.kt

### 条目：perf-jvm · 多开 JVM（未评估）
状态：blocked
依赖：jvm-worker-lease
优先级：P2
背景：worker 拆分后是否增加批量 worker 数量，取决于 JVM 启动成本、RSS、站点限流、失败率和调试延迟；不能从 CPU 核数直接推导。这里评估的是专用 worker 的容量，不是直接增加 API/uvicorn 进程；在没有真实容量数据前不做。
约束：先完成 `jvm-worker-lease`、分块和独立运行目录；按同一快照分片，分别测单 worker 与多 worker 的吞吐、P50/P95 延迟、RSS、错误率和站点请求量。未完成测量前不增加实例；若收益不覆盖内存/限流代价，保持一个批量 worker，允许本条最终关闭而不实施多开。
验收：给出可复核的单 worker/多 worker 对比和容量结论；只有在结果支持时才调整 worker 数量，并确认每个实例的参数、profile、JVM 和运行目录完全独立。
指针：core/jvm_debug.py，core/store.py，lessons §四十七 / §六十五 / §六十六
阻塞于：等待 `jvm-worker-lease` 完成并取得单 worker 基线；若容量收益不足，直接关闭本条，不实施多开。

### 条目：jvm-debug-worker · 交互调试专用常驻 worker
状态：todo
依赖：jvm-worker-lease
优先级：P1
背景：交互调试对首个结果延迟敏感，和批量校验的吞吐目标不同；两者共用一个常驻 JVM 会让 profile、cookie、旧类和运行参数互相污染。
约束：交互 worker 独占 JVM daemon、浏览器 profile、参数文件、运行根目录和 worker 身份；同一 profile 内仍按安全边界串行，不能以常驻为理由放开请求间状态清理。调试任务只由该 worker 消费，取消必须等待实际 Gradle/JVM 线程收尾后再释放租约；worker 停止要确认子进程退出。
验收：交互调试冷启动与常驻两条路径的结果逐字段一致；批量 worker 运行时调试请求仍能按调度策略及时开始；重启/取消/异常退出后没有孤儿 JVM、残留租约或跨任务 cookie/输出污染。
指针：core/jvm_debug.py，core/jvm_daemon.py，backend/jobs/runner.py，lessons §六十 / §六十五 / §六十六 / §七十四

### 条目：jvm-worker-cutover · 从单进程 lane 切换到双 worker
状态：todo
依赖：jvm-worker-lease, jvm-debug-worker, jvm-validate-worker
优先级：P1
背景：多起服务的推荐边界是两个专用 JVM worker，而不是复制多个 API 服务；API 负责提交、查询和推送任务，worker 负责实际执行。切换前必须证明跨进程认领、隔离和恢复已经成立。
约束：保留单后端作为唯一 API 入口；先灰度启用 worker 消费，再移除旧的进程内直接执行路径。禁止让两个 worker 共享 `RUN_LOCK` 作为跨进程锁，也禁止共享 `args.properties`、daemon info、cookie/profile 或固定结果文件。切换期间失败必须能定位到 job、owner、generation、chunk 和运行目录；`perf-jvm` 没有容量收益时不阻塞单批量 worker 的切换。
验收：双 worker 并行运行一批调试与一批校验，任务不会重复执行、互相覆盖或丢失；API 重启不影响 worker 已租约任务的可恢复性；停止任一 worker 后另一 worker 不会接管其未过期任务，按租约规则恢复后才可继续；全部结果与单进程基线逐字段对账。灰度期间出现差异可回退到旧路径，确认阶段、日志和结果文件完整后才允许移除旧的进程内直接执行路径。
指针：backend/jobs/runner.py，core/store.py，core/jvm_debug.py，lessons §二十八 / §六十五 / §六十六

### 条目：proj-3 · 第三期收尾：摘抽屉里最后那块本地投影
状态：open
依赖：无
优先级：P2
背景：`matched_html` 回填已交付（机制见 lessons §七十五）。剩下的是把抽屉里那块
  本地投影彻底摘掉，但必须先定子项列出的两处。
约束：拆子项逐步实现——两处定不下来之前不许摘 `proj-3-drop`。
子项：
- proj-3-drop / proj-3-bookurl / proj-3-attr
验收：三个子项全部落地后，抽屉里每个段的「命中源码」都走引擎，没有投影退路。
指针：lessons §七十三 / §七十五

### 条目：ux-pick · 前端暴露 `pick`：换一条重试
状态：todo
依赖：无
优先级：P2
背景：搜索结果多条时选第 N 条重试。后端 `pick` 参数已支持，Legado 固定取第 0 条。
约束：key 由我们拼装，用 App 那边已有的 `pick` 语义，别新造一套。
验收：前端有一个「换一条」入口，点了之后能拿到另一条的结果。
指针：core/jvm_debug.run_jvm_debug，Debug.kt

### 条目：ux-chip-jump · 点击摘要里的变化数跳到对应筛选
状态：todo
依赖：无
优先级：P2
背景：摘要里的变化数点了没反应。
约束：要与统计条 chip 的筛选状态协同，不能各管一套筛选。
验收：点变化数后列表筛到对应子集，且 chip 状态同步。
指针：frontend/src/views/SourcesView.vue

### 条目：ux-timeline · 校验历史时间线
状态：todo
依赖：无
优先级：P2
背景：每次校验的前后对比，而不只是聚合数。
约束：**依赖放宽 `checks` 的保留策略**——现在每源只留最近一条，时间线至少要留
  N>1 条。先定 N 定多大、以及它带来的库增长，再动手。
验收：一个源能看到最近 N 次校验的前后对比。
指针：lessons §二十八

### 条目：ux-trash-url · `TrashDrawer` 的批量恢复对齐同一套 URL 语义
状态：todo
依赖：无
优先级：P2
背景：它仍是页级勾选 + 行对象，与列表页 `ec93ad4` 之后的 URL 语义不一致。
约束：对齐列表页那套 URL 语义，别再造一份状态。
验收：回收站的批量恢复与列表页行为一致（同一 URL 的选中 / 恢复语义）。
指针：frontend/src/components/TrashDrawer.vue

### 条目：dup-bc · 合并重复源：B/C 档并排显示可用性
状态：todo
依赖：无
优先级：P2
背景：只读的三档（镜像 / 同域名 / 同名）只展示不动作；B/C 档要并排显示可用性来
  辅助判断留哪条。
约束：口径与写路径见 lessons §三十四；**不自动合并**、不按域名批量去重。
验收：B/C 档每组能看到并排的可用性对比。
指针：lessons §三十四，core/dups.py

### 条目：dup-keep · 合并重复源：「都留着并记住」（需持久化）
状态：todo
依赖：无
优先级：P2
背景：「忽略这组」和「这组合并过」形状一样，都要持久化。
约束：用**一张表** `dup_decisions(kind, key, decision, note, created_at)` 装，
  **别塞 settings**——`POST /api/settings/reset` 会把它们**静默清掉**；「用户对
  数据的决定」在本项目里一律存库，settings 只放可重置的参数。
验收：忽略 / 已合并的决定在重启与 settings reset 后都还在。
指针：lessons §三十四 / §十

### 条目：syntax-gap · 本地回放的语法缺口
状态：open
依赖：ai-verify
优先级：P2
背景：本地回放**已不在校验与调试链路上**，还在用它的只剩 AI 修复那条链
  （`core/repair/*`）——在 `ai-verify` 拍板之前，下面这张缺口表只是
  「本地验不了」的账，不是待办。
约束：只有**还留在 AI 修复链上**的语义才急，动手前先确认它在不在那条路径上；
  别为了「让本地能验」把 webView 去掉——去掉就真的读不了。
子项：
- syntax-bang · 排除索引 `li!0` / `dd!0:1:2`：`findIndexSet` 里 `.` 与 `!` 语义相反，只实现了点式（最大一块）
- syntax-fallback · 执行期兜底的「选择器解析不了」：样例（URL 模板、JSONPath 片段、碎片）先逐个归类再实现
- syntax-jsonpath · JSONPath 超出子集：`[*]` / `['键']` 已支持，`[1:3]` 切片没有，扩展时已有写法行为不变
- syntax-xpath · `//` 开头的 XPath：引引擎或恒 unknown，别静默当 CSS（AGENTS #4），决议归 xa-1111
- syntax-bracket · 方括号索引 `[-1]` / `[0]` / `[1,3]` 与区间 `[0:10]`：未实现，与点式保持同一套归一
- syntax-shorthand · `text.` / `children.` 简写：语义已查清，实现即可，判定半边一律 unknown 不动
验收：每行语义能在本地回放跑通或有明确归因，带正反例测试。
指针：lessons §二十三 / §四十四 / §四十九 / §六十，core/rules/replayer.py

### 条目：webview-content · webView 型正文（本地与 JVM 都验不了）
状态：todo
依赖：无
优先级：P1
背景：（单独排：等真靶子在手）这一条不是「语法回放不了」，是「**取数**拿不到」——正文本身就是 `params`
  加密 + `xhr_mode`，图片地址只在解密后的 JS 对象里；漫画鱼章节页就是该形态，静态 HTML
  没有图片，运行时图片还会变成 `blob:` URL。
约束：**别把 `content_ok=None` 当成源有问题**；别为了「让本地能验」把 webView
  选项去掉。调试工作台要明确标出 L2/L3，静态网页视图不能框选时给出「使用本机引擎 / 连
  App 调试」动作；运行时 DOM / 命中片段的来源必须标明，不能把静态补抓冒充 App 页面。
验收：调试工作台对这类源能拿到运行时正文或明确给出不可判定原因；若能取得运行时 DOM，
  命中源码与框选高亮使用同一份材料；跑批那条路要么同样能验，要么结论里明确标「这类源跑批
  不可信」。
指针：lessons §四十九 / §六十 / §七十五，appservice/test/io/legado/app/service/ShadowBackstageWebView.kt

### 条目：strengthen-contract · 用契约测试钉住那个隐式约定
状态：todo
依赖：无
优先级：P2
背景：`verify.skipped && !verify.error` 蕴含「用户没选验」；以及「验证结果必带
  `source` 或 `local_approx` 之一」——后者是「加通道时忘了设 `source` → 静默退回
  空标签」那颗地雷。
约束：判据是「会静默过期 / 隐式到没人验证得了」，只补测试不改行为。
验收：两条约定各有契约测试钉住，破坏任一条测试变红。
指针：lessons §七十八，tests/test_jvm_debug_contract.py


### 条目：ux-debug-flow · 调试与规则验证闭环
状态：todo
依赖：无
优先级：P1
背景：调试工作台、证据摘要、DOM 候选和动作层级共同服务同一条用户流程：定位当前步骤、判断材料是否可信、选择规则、应用并用真实引擎验证。原先拆成多个条目会让同一闭环的完成条件分散。
约束：保留 `useDebugSession`、`steps`、`activeStep`、`diffRows` 作为唯一状态来源；首屏先展示当前步骤、结论、证据来源和推荐动作；候选必须按搜索/目录/正文/媒体意图生成，并显示示例；规则候选只能提议，最终以真实引擎验证；每个步骤只有一个推荐主动作，应用、重调和连 App 验证按状态合并；详细命中数、重复率、稳定性、事件流和整页源码属于可展开诊断；移动端退化为横向步骤导航；重跑保留旧结果和差异。
验收：用户进入步骤后无需长距离滚动即可知道卡在哪里、材料能否作为依据以及下一步做什么；候选可直接应用并验证，结果能与前一轮对比；App 实测、本机引擎、补抓页面和本地投影不混淆；搜索、目录、正文、媒体四类字段均有意图化候选；fail、unknown、stale、pass+notes 的主动作稳定；桌面和窄屏均无主动作被挤走；相关纯函数和组件测试通过。
指针：frontend/src/components/DebugWorkbench.vue，frontend/src/utils/debugNextAction.js，frontend/src/utils/debugEvidence.js，frontend/src/utils/selector.js，frontend/src/utils/debugCompare.js

### 条目：ux-debug-editor · 调试页承接编辑与保存闭环
状态：todo
依赖：ux-debug-flow
优先级：P1
背景：已有源的规则编辑、源级配置、证据、重跑和保存应在独立调试页完成；弹窗只承担新建、快速生成、快速编辑和摘要。
约束：当前步骤规则在工作台编辑；基本信息、类型、标签、请求、发现和原始 JSON 放入源设置抽屉；`session.source` 是唯一编辑事实；区分应用到会话、验证当前规则和保存落库；保存保留标签、锁定状态、脏状态和另存为语义；刷新或离开前明确提示未保存修改。
验收：已有源从列表进入调试页后，不返回弹窗即可修改规则和源级配置、重跑、对比并保存；保存结果与原编辑路径逐字段一致；刷新、返回、源 URL 变化和另存为都有明确行为；弹窗不维护第二套长期规则状态。
指针：frontend/src/views/DebugWorkbenchView.vue，frontend/src/components/DebugWorkbench.vue，frontend/src/components/SourceEditDialog.vue，frontend/src/composables/useDebugSession.js

### 条目：ux-debug-config · 调试入口状态记忆
状态：todo
依赖：无
优先级：P1
背景：调试入口已经完成层级简化，但同一源重复调试仍需重新填写目标和关键词。
约束：按归一化源 URL 记住最近目标、关键词、通道和缓存档；不保存规则、cookie 或登录态；不得改变 `debugKeyOf` 和 App 分派语义；环境检查成功时只显示状态标签，失败才展开原因。
验收：同一源第二次打开时目标和关键词可直接复跑；首屏可见开始调试和上次关键词；本机环境正常时不常驻展开明细；连 App 仍能就地完成 IP、预检和推送；有纯函数测试钉住记忆键和默认值。
指针：frontend/src/views/DebugWorkbenchView.vue，frontend/src/composables/useDebugSession.js

### 条目：ux-debug-reading · 调试高级信息与响应式阅读体验
状态：todo
依赖：ux-debug-flow
优先级：P2
背景：运行态、事件流、整页源码、语法速查和详细 AI 信息对熟悉用户有用，但不应挤走首次调试所需的结论和动作。
约束：默认层只展示结论、原因、主动作、核心值和证据来源；高级材料可展开；运行态继续消费 `useDebugSession`，固定显示等待、预算和取消状态；移动端保持当前步骤、结论和主动作在首屏；不复制运行态或结果状态。
验收：滚动到证据区仍能找到运行态；取消等待与后端任务状态文案明确；展开高级信息后原有材料仍可用；窄屏下步骤、结论、来源和主动作可触摸访问且无横向页面溢出。
指针：frontend/src/components/DebugWorkbench.vue，frontend/src/views/DebugWorkbenchView.vue，frontend/src/composables/useDebugSession.js


---

## 3 · 待决策

### 条目：ai-verify · 十-7 AI 提议验收换真引擎
状态：todo
依赖：无
优先级：P2
背景：AGENTS #3 已按 2026-09-29 拍板改写（校验与生成验证由本机引擎完成，AI 提议的
  候选由回放器初筛）。本条剩最后一环：`core/repair/suggest.py` 的 `replay_step` 换成
  跑一次本机引擎；`dry_run` 免费的 `preselect`（用 `replayer.parse_rule` /
  `extract_all`）要单独设计。
约束：换引擎只动修复循环的验收那一步；`dry_run` 的候选初筛仍走回放器——那是筛选
  不是验收（lessons §七十三）。
验收：`replay_step` 换成真引擎后验收结论与今天一致或更好。
指针：lessons §二十六 / §七十三 / §八十，AGENTS.md #3，core/repair/suggest.py

### 条目：s5a-a2 · A2 之后可评估：跑批不再剥 webView 选项
状态：blocked
依赖：无
优先级：P2
背景：跑批那条路（`ValidateService`）今天仍是「剥掉 webView 选项 → 渲染 → 另喂
  `BookList`」。shadow 到位后跑批也可以不剥、直接走 App 自己的链路——能删掉一整条
  支路。
约束：**会改变跑批结论**（真 webView 语义 ≠ 渲染后喂解析器）→ 要配 `CACHE_VERSION`
  全量重跑；**未评估前不要动手**。
阻塞于：未评估（要先量「换语义会翻多少条结论」）
验收：评估结论 + 用户拍板；若做，全量重跑一次并记 `CACHE_VERSION`。
指针：lessons §六十 / §七十三 / §八十七

### 条目：proj-3-bookurl · 详情段（`bookUrl`）没有命中
状态：todo
依赖：无
优先级：P2
背景：`ruleSearch.bookUrl` 是在**搜索页**的每个 `bookList` 节点内求值的——证据在
  搜索页上，而 Python 按「段自己的 url + 段名」取（详情段的 url 是详情页），
  `(url, 段名)` 这把键表达不了。
约束：要么改契约（把记录扩成 `{page_url, step, container_selector}`，或 App 侧
  `DebugService.kt` 新增按列表节点求 bookUrl 的分支并定义同页多条书取哪个节点），
  要么让它继续走投影；两条选一，别两边都改。
验收：一张实测的「详情段想看的其实是搜索页的那块 DOM」样例，据此定契约或维持投影。
指针：lessons §七十五

### 条目：proj-3-attr · 末段是属性名的规则
状态：todo
依赖：无
优先级：P2
背景：兜底只认那五个动作词；属性名与标签名同形（`title` / `style`）。
约束：要做得先有与 `core/rules/replayer._is_attr_or_action_name` 对齐的词表 + 逐词
  比对测试（AGENTS #22⑤）与 App 侧末段回填分支（`DebugService.kt`）。
验收：词表与逐词比对测试落地，末段属性名规则能给出命中。
指针：lessons §七十五，core/rules/replayer.py

### 条目：norl-ambig · 「没结果」的二义性
状态：todo
依赖：无
优先级：P2
背景：JVM 搜索对单关键词跑，某源没结果可能是「没这本书」。
约束：缓解靠多词复核（全不命中才判坏），落地点在做 `no_result` 复核批次时。
验收：做一个 `no_result` 复核批次，单关键词无结果的源经多词复核后才判坏。
指针：lessons §五十三，core/checker.py

### 条目：concl-feed · JVM 结论要不要反向喂给本地口径
状态：todo
依赖：无
优先级：P2
背景：health / stars 的映射与来源标注——那是「结论互通」的事，等三段结论稳定后
  再议。
约束：**动本地口径才涉及 `CACHE_VERSION`**。
验收：先给出「三段结论是否稳定」的判断，再决定要不要互通。
指针：lessons §七十二 / §七十三

### 条目：ua-axis · UA 要与设备对齐，得先给结论行加一根 UA 轴
状态：todo
依赖：无
优先级：P2
背景：UA 是三条通道各一条。要「与设备对齐」得先给结论行加一根 UA 轴（把当次实际
  用的那条记进结论；行里已有 `webview_stripped` 这个「当次条件」的先例），否则历史
  结论与新结论不是同一口径却看不出来（与 AGENTS #5b 同构）。
约束：**现在不动**——改 UA 值会改变站点返回的页面，与库里已有结论不可比。
验收：UA 轴落地（结论行记录当次 UA），且能区分新老口径。
指针：AGENTS.md #5b，lessons §五十一

### 条目：note-half · 正文附注只做了一半：疑似错误页
状态：todo
依赖：无
优先级：P2
背景：JVM 面会带「正文较短」的附注；「疑似错误页」那一半要**全文**匹配
  `CONTENT_NOISE_MARKERS`，而结论行的 `content_sample` 只有 60 字。
约束：要做得先把它放宽（上限 `SHORT_CONTENT_CHARS`）；报告与列表要用**同一个函数**。
验收：疑似错误页的附注在报告与列表上都出现，且用的是同一个函数。
指针：lessons §二，core/quality.py

### 条目：antibot-jvm · anti-bot 墙在 JVM 侧仍报 no_result / error
状态：todo
依赖：无
优先级：P2
背景：`login_wall` 只认 `LOGIN_MARKERS`（「请登录」这类）；验证码 / Cloudflare 那类
  （`ANTI_BOT_MARKERS`）没有对应状态，于是「需人工过一下」被读成「没出结果」。
约束：判据照 `core/checker.is_login_wall` 那套，**别新造**。
验收：这类源不再被判成 no_result，而是有一个「需人工过一下」的状态。
指针：lessons §五十八，core/checker.py

### 条目：jvm-keys · `jvm.*` 五个跑批键今天没有写入口
状态：todo
依赖：无
优先级：P2
背景：弹框只读它们当种子、设置页没有控件，想改永久默认值只能手改 JSON。
约束：两种收法——**删键**（弹框种子用 `LIMITS` / 常量兜底，行为不变）或给设置页一个
  显式的「跑批默认值」区。动它要一起改 `DEFAULTS` / `_SPECS` / `JvmSettingsPatch`
  / `tests/test_settings_api.py`。
验收：选定一种收法并落地，设置接口与默认值的唯一来源仍在 `core/settings_store.py`
  （AGENTS #8）。
指针：AGENTS.md #8，core/settings_store.py，tests/test_settings_api.py

### 条目：research-type · 调研一：类型判定补齐
状态：blocked
依赖：无
优先级：P2
背景：`infer_type_static` 判不出音频 / 下载源；真正的解法是 `book.isWebFile`
  （`downloadUrls` 决定）。
约束：**先摸清库里有哪些能定案的结构信号，再谈判据换不换**；在那之前不要动出口
  （没有信号就加出口 = 把「猜域名」换成「猜别的东西」）。换判据时顺手拿掉「被审
  对象给自己投票」（declared 参与计分，AGENTS #11）。
阻塞于：库里能定案的结构信号尚未摸清（要先做一次只读统计）
验收：一次实测统计给出可定案的信号清单，据此决定换不换判据。
指针：lessons §五十五，AGENTS.md #11

### 条目：research-postproc · 调研二：正文后处理字段
状态：blocked
依赖：无
优先级：P2
背景：App 用、我们不用，构成「同源不同判」。`replaceRegex` 风险方向单一（替换后
  变空才误放），低风险高覆盖。
约束：**未评估前不要动手**。
阻塞于：未评估（覆盖与风险要先量）
验收：评估结论 + 用户拍板。
指针：lessons §五十五

### 条目：xa-1111 · XPath 引擎：引还是判 unknown
状态：todo
依赖：无
优先级：P2
背景：XPath 引擎**不在「已明确不做」的名单里**——它是 `syntax-xpath` 那条「还没定」：
  要么引引擎、要么一直 unknown。
约束：两条选一，别让 `//` 开头的写法静默走到 CSS 分支（AGENTS #4）。
验收：结论 + 落地；若判 unknown，原因要一路走到用户眼前。
指针：lessons §二十六，core/rules/replayer.py

---

## 已完成

> 已交付的事项只在这里留一行指针——**机制看 lessons，细节看 `git log`**（AGENTS #10）。
> 这一区只允许 `状态：done`。

### 条目：debug-false-pass · 空 content 规则的假 pass 与段失败的下一步
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（1ab4a19）：空 content 规则的短路段判 unknown 带可执行原因，
  不再跟着「正文页解析完成」落成假 pass；渲染失败段带下一步附注。机制 lessons §八十。
约束：空规则的段显式标「没验（规则为空）」，不许落成 pass；段失败按原因给可执行下一步（AGENTS #4）。
验收：断言钉住（空规则段显示「没验」而非通过、失败段带动作）；细节看 git log（1ab4a19）。
指针：core/app_debug.py，lessons §八十

### 条目：jvm-debug-budget · 调试的墙钟预算分层
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（06d1fc1）：整链预算收进 `settings_store`（`debug.timeout`，默认 90、
  区间 30–300，不得与桥的渲染上限 60 同值）；`/rules/jvm-debug` 不传参吃设置、显式越界 400。
约束：默认值与上下界只在 settings_store 一处（AGENTS #8）；「渲染预算按剩余整链动态取」
  有意不做，留给真出现「合法渲染吃满 60s」的靶子再议。
验收：webView 源调试不再因「渲染预算=整链预算」提前判超时；细节看 git log（06d1fc1）。
指针：core/jvm_debug.py，core/settings_store.py

### 条目：ux-debug-wait · 调试等待态可见可取消
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（210e3e3）：等待区显示已等待秒数与预算；AbortController「取消
  等待」只断前端的等，后端 lane 那次仍跑完（两条调试通道共用）。
约束：排队可见、阶段状态、服务端取消的服务端完整版不在这里装完成，另立口径。
验收：调试进行中可见等待时长与预算；点取消后界面立即恢复可操作；细节看 git log（210e3e3）。
指针：frontend/src/components/SourceEditDialog.vue，core/jvm_debug.py

### 条目：ux-debug-loop · 重跑不清场，上一份结果可对比
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（210e3e3）：重跑不清场——重跑期间旧结果照常显示，新结果到来后
  旧份精简为对比基线，页签逐段标「相同 / 变了 / 新失败 / 换了目标 / 新出现」+ 汇总行；
  判据在 `utils/debugCompare.js`。
约束：对比键是（段名, URL）；结果容器一次定型为「最近 K 次运行」；过期判定与
  strengthen-src 是同一份事实（改了哪段规则哪段过期），别做两套。
验收：断言钉住「重跑不清空结果」；细节看 git log（210e3e3）。
指针：frontend/src/utils/debugCompare.js，frontend/src/components/RuleDebugDrawer.vue

### 条目：jvm-webview-nav · webView 段的相对地址静默等满渲染预算
状态：done
依赖：无
优先级：P0
背景：2026-09-26 交付（18b79ab）：`BrowserBridge` 的 navigate 改走 `sendForResult` 并用
  `navigationFailure` 判 CDP 命令级错误立即带原因返回；`ShadowBackstageWebView.resolveAgainstTag`
  在导航前按源 URL 补全相对地址。实测同 key 75.5s fail → 常驻复用 2.8s（渲染本体 0.9s）。
约束：只判命令级 error，不把 `result.errorText` 判进来（ERR_ABORTED 会误杀马上被 JS
  重定向的正常页）；不动上游 `Debug.kt` 的分派语义；webView 取数能力归 webview-content。
验收：8 条 Kotlin 测试（WebViewNavigationTest）；细节看 git log（18b79ab）。
指针：appservice/test/io/legado/app/service/BrowserBridge.kt，ShadowBackstageWebView.kt，WebViewNavigationTest.kt

### 条目：jvm-runtime-snapshot · 让 JVM 实际运行环境与 dump 对拍
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（406941c）：refresh / gradle / validate_daemon / direct 四份 snapshot
  齐全（按 mode+entry 后缀各留一份），差异逐条归因为三类口径性差异 + 一处直起静默继承
  （已修）。机制 lessons §九十。
约束：actual 差异报告只进排障，不进入源健康判定链。
验收：修复与边界测试看 git log（406941c）。
指针：core/jvm_runtime_snapshot.py，lessons §九十

### 条目：jvm-scheduler-policy · 调试优先但不能饿死批量校验
状态：done
依赖：无
优先级：P1
背景：2026-09-27 交付（17117c6）：lane 从 FIFO asyncio.Lock 升级为 _Lane——unit 边界按
  有效优先级发放许可（debug=0 恒定；batch=10，等待超阈值后逐级老化到 floor=2，仍高于
  调试），被取消的获许可者立即转交许可；排队现状经 GET /api/jobs/lane 可观测。
约束：优先级只影响排队顺序，不绕过同一 JVM/profile 的独占约束；老化常量集中在 runner
  模块顶部（无实测依据不进 settings）。
验收：lane 单测四条（插队/老化/快照/取消转交）；细节看 git log（17117c6）。
指针：backend/jobs/runner.py，lessons §六十八 / §七十四

### 条目：jvm-batch-chunk · 批量校验按可恢复分块执行
状态：done
依赖：无
优先级：P1
背景：2026-09-27 交付：批量按 `jvm.chunk_size`（settings 新键，默认 25、限幅 [5,200]，
  提交时冻结进 manifest）分块；块完成即入库并写 DONE 标记，块间交还 lane 重排队；重试
  只补失败块。真实环境两块验收通过。
约束：分块引用冻结的 runtime snapshot；重试不覆盖旧产物；单条不分块，取消语义不变。
验收：单元测试钉住分块调用数/失败中止/重试恢复；细节看 git log（同日 batch-chunk 提交）。
指针：backend/api/jvm.py，backend/jobs/runner.py，core/settings_store.py，lessons §五十三 / §五十四

### 条目：strengthen-src · 给生成后的验证标出处
状态：done
依赖：无
优先级：P1
背景：2026-09-27 交付：分步新鲜度——各规则组在验证时刻定格快照，改哪组规则只让映射到
  的步骤过期（ruleSearch 波及 search+bookUrl），未改动步骤结论保留可用；过期步骤带
  「重新调试本步」入口。
约束：过期判据唯一一份在 utils/verifyFreshness，与抽屉「重新调试本步」同一条纪律，
  不新造通道。
验收：node 断言钉住（倒着写会复活旧误导）；细节看 git log（同日 strengthen-src 提交）。
指针：frontend/src/utils/verifyFreshness.js，lessons §七十八


### 条目：jvm-env-readiness · JVM 环境收尾与跨平台启动器
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付：SDK 发现跳过不完整的候选根（诚实报因并继续试下一候选），refresh
  与 prepare_gradle 改用 readiness 解析出的同一份 runtime；跨卷与非 Windows 主机由代码
  路径与单测覆盖，真非 Windows 硬件未实测。
约束：自检不下载依赖、不隐式构建；准备态与执行态各自只检查自己该检查的。
验收：细节看 git log（同日 env-readiness 提交，含边界测试）。
指针：core/jvm_env.py，scripts/prepare_gradle.py，lessons §六十五

### 条目：jvm-task-manifest · 固定每次任务的输入、环境、产物和执行方式
状态：done
依赖：无
优先级：P1
背景：2026-09-26 交付（abda384 及同日提交）：单条/批量与调试的运行目录在执行开始落盘
  manifest.json（信封 job/owner/chunk/generation/retry_of + 提交冻结的 inputs）与
  runtime-snapshot 副本，Gradle 全量 stdout/stderr 同落；保留策略=成功/取消清理、
  失败/崩溃保留现场（上限 20 修剪）。
约束：SQLite 仍是任务管理事实源；重试天然生成新运行目录（retry_of 记录它替代谁），
  旧产物不被覆盖。
验收：细节看 git log（abda384）。
指针：backend/jobs/runner.py，core/jvm_debug.py，lessons §六十五 / §六十八

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

### 条目：jvm-dump-gate · dump 缺字段时禁止静默继承调用方环境
状态：todo
依赖：无
优先级：P1
背景：`load_dump()` 现在只检查文件存在与新鲜度。`run_direct()` 和 daemon 启动又把缺失的
  `workingDir` / `environment` 退化成调用方 CWD / 环境，`classpath`、`jvmArgs`、
  `systemProperties` 的缺失也会悄悄改变直起参数。
约束：在 dump 入口和直起/常驻边界都做显式 schema 闸门；必需键要校验存在、类型与非空语义，
  不许用 `or None` / `or {}` 兜底。`javaLauncher` / `javaHomeEnv` 与 `maxHeapSize` 的
  fallback 也要明确是否属于闸门范围。错误必须逐字段说明，并指向「运行一次 `--refresh`」；
  产品回到 Gradle 时也必须把该原因带到用户眼前。
验收：手工删掉 dump 的 `workingDir`、`classpath`、`environment` 各跑一次直起与常驻入口；
  均在启动子进程前明确报错或留下可见原因，不得继承当前 CWD / 环境继续运行。
指针：core/jvm_direct.py，core/jvm_daemon.py，core/jvm_debug.py


---

## 1 · 排队

### 条目：jvm-runtime-snapshot · 让 JVM 实际运行环境与 dump 对拍
状态：todo
依赖：jvm-dump-gate
优先级：P1
背景：dump 记录的是 Gradle 任务声明的环境，当前没有证据证明 JVM 实际拿到的环境与它一致。
  本条吸收原「jvm-dump-parity」：refresh 与产品都使用同一个 test task，但仍需把声明与实际
  运行时逐字段对拍，不能用任务名相同代替。
约束：snapshot 只进排障，不进入任何判定链。编码统一复用 `ServiceJson`；`ServiceJsonTest`
  只负责无 Robolectric 的编码器形状测试，实际快照挂在 `DebugService.main` / `ValidateService.main`
  等真实入口。对拍口径必须先定义清楚：路径归一化后比较 `workingDir` ↔ `user.dir`；按项比较
  `classpath` ↔ `java.class.path`；把 `maxHeapSize` 派生的 `-Xmx` 纳入后再比 `jvmArgs` ↔
  `getInputArguments()`；dump 声明的 `systemProperties` 与 JVM 对应键比对，不能把显式属性表
  冒充完整 `System.getProperties()`。不同入口的 snapshot 不得互相覆盖。
验收：refresh、直起、常驻各留一份可追溯 snapshot；完成一次真实对拍，workingDir / classpath /
  jvmArgs / systemProperties / environment 每个差异逐条归因。差异若会导致今天的静默继承或
  参数漂移，转入 `jvm-dump-gate` 的约束或修复范围。
指针：core/jvm_direct.py，appservice/legado-test.init.gradle，appservice/test/io/legado/app/service/ServiceJson.kt，
  appservice/test/io/legado/app/service/ServiceJsonTest.kt，appservice/test/io/legado/app/service/DebugService.kt，
  appservice/test/io/legado/app/service/ValidateService.kt，lessons §六十五

### 条目：proj-3-drop · 前端摘掉本地投影
状态：todo
依赖：proj-3-bookurl, proj-3-attr
优先级：P1
背景：`replayResult` / `canReplay` / `doReplay` / `/replay-step` 那条链现在是
  「引擎没覆盖的段」的退路。摘之前先定详情段与末段两处怎么办，否则那两段的
  「命中源码」会空掉。
约束：先满足下面三处前置，否则摘了会静默丢功能。**A、B 两处待用户就「改契约
  vs 维持投影」拍板后另开任务**；本条只等那两件。
验收：摘掉后抽屉里每个段的「命中源码」仍能取到值，或明确显示「本段取不到」。
指针：lessons §七十三 / §七十五，frontend/src/utils/layers.js
  前置三处（都落地才能摘；依据与来源行号在
  `frontend/src/components/RuleDebugDrawer.vue` 的 `matchedFrom` / `matchedHint` 旁）：
  - **C · 每种空值有可执行的一句话**：已落地。
    App 通道空 / 本机引擎空 / 没有页面 / 规则不支持（后两者是既有原因，
    优先级不变）各有一句。摘投影不依赖它，但它是
    TODO 原定验收那半「明确显示本段取不到」的落地。
  - **A · 契约带取值页上下文**：待拍板。详情段的命中证据属**搜索页**的
    `bookList` 节点作用域（`tests/test_legado_rules.py:479-510`），而现有键是
    `(段自己的 url, 段名)`（`core/app_debug.py:634-652`）——表达不了。
    选一条：把记录扩成 `{page_url, step, container_selector}`，或让 search 页同时输出
    bookUrl 命中（App 侧 `DebugService.kt` 需新增按列表节点求 bookUrl 的分支，并定义同页
    多条书取哪一个节点）。跨 Kotlin / Python / 测试 / 前端协议，需逐词/形状测试。
  - **B · 属性末段有明确模型**：待拍板。五动作词表恰好五词
    （`text` / `textNodes` / `ownText` / `html` / `all`，大小写归一），`title` / `style` / `label`
    归属性名判定（AGENTS #21）。现状已是此语义
    （`core/rules/replayer.py:55-66`），缺的是逐词契约测试与 App 侧末段回填分支
    （`DebugService.kt:519-526,536-541`）。

## 2 · 按需

### 条目：fe-drawer-tests · 抽屉的提示规则没有自动化覆盖
状态：todo
依赖：无
优先级：P2
背景：`frontend/src/utils/*.test.js` 只覆盖 `htmlView` / `layers` /
  `selector`，`RuleDebugDrawer.vue` 一行断言都没有。而它那两处提示（命中源码的来源与空值口径）
  正是读者判断“这结果该不该信”的依据。实测代价：一处误删 `v-if`
  （把局部投影的说明渲染给 App 实测）已经靠读 diff 才捕到，自验全绿。
约束：提示逻辑在 `.vue` 里，而现有 node 套件跑的是 `utils/*.js`
  纯函数——要么把判定抽成 `utils/` 里的纯函数再钉（同 `layers.js` 那条路），
  要么引入组件测试。别为了过测把提示文案搬到别处又不钉。
验收：命中源码的来源选择与空值口径各有一条可跑的断言；且修改该逻辑而回退断言时
  套件会变红。
指针：frontend/src/components/RuleDebugDrawer.vue，frontend/src/utils/layers.js

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

### 条目：s5a-d3 · D3 跑批也走常驻
状态：todo
依赖：无
优先级：P2
背景：批量校验也走常驻（**另起一个实例**，不与调试共用），省掉每次跑批的 Gradle
  + JVM 拉起。收益主要在「频繁跑小批次」。
约束：**「频繁跑全量会被封 IP」是硬约束**，比任何提速优化都优先（lessons §六十八）；
  共用实例与浏览器 profile 独占要先处理。
验收：跑批结论与今天逐字段一致。
指针：lessons §六十六 / §六十八 / §七十四，core/jvm_debug.py

### 条目：s5a-a1 · A1 唯一没做完的验收：与设备 WS 逐事件对拍
状态：todo
依赖：无
优先级：P2
背景：结构同构已由契约测试保证；逐事件对拍需要手机开着 Web 服务，等设备在场时补。
约束：对拍要逐事件，不接受「结构同构」代替。
验收：设备在场时跑一次逐事件对拍，差异逐条有归因。
指针：lessons §五十一 / §六十四，tests/test_jvm_debug_contract.py

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
约束：受「频繁跑全量会被封 IP」这条硬约束，等下次真要跑量时顺带量（lessons §六十八）。
验收：给出上调前后的一档实测对比（时长与失败率）。
指针：lessons §七十四，appservice/ValidateService.kt

### 条目：perf-jvm · 多开 JVM（未评估）
状态：todo
依赖：无
优先级：P2
背景：参数文件单份共享要按分片参数化；每个 fork 有入场费；N 份内存得先量 RSS。
约束：未评估前不动手。
验收：给出「分片参数化 + RSS 实测」两项结论，再决定要不要做。
指针：lessons §六十五 / §六十六，core/jvm_debug.py

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
依赖：无
优先级：P2
背景：本地回放**已不在校验与调试链路上**，今天还在用它的只剩 AI 修复那条链
  （`core/repair/*`）。所以下面这张缺口表的价值取决于 `ai-verify` 拍板——在那
  之前它只是「本地验不了」的账，不是待办。
约束：只有**还留在 AI 修复那条链上**的语义才急；动手之前先确认它还在不在那条
  路径上。别为了「让本地能验」把 webView 去掉——去掉就真的读不了。
子项：
- syntax-bang / syntax-fallback / syntax-jsonpath / syntax-xpath / syntax-bracket / syntax-range / syntax-shorthand
验收：每个子项各自落地；缺口表里每行的语义都能在本地回放上跑通或有明确归因。
指针：lessons §二十三 / §四十四 / §四十九 / §六十，core/rules/replayer.py

### 条目：syntax-bang · 排除索引 `li!0` / `dd!0:1:2`
状态：todo
依赖：ai-verify
优先级：P2
背景：最大一块。`findIndexSet` 里 `.` 与 `!` 是**相反语义**的分隔符，我们只实现了点式。
约束：按 App 的语义实现，别新造。
验收：带 `!` 的排除索引能回放出与 App 一致的结果，并有正反例测试。
指针：lessons §一，core/rules/replayer.py

### 条目：syntax-fallback · 执行期兜底的「选择器解析不了」
状态：todo
依赖：ai-verify
优先级：P2
背景：样例是 URL 模板、JSONPath 片段、碎片——**还没逐个归类**。
约束：先归类再实现，别一次性全当「不支持」。
验收：每条样例有归类结论（能做到 / 做不到 / 需引引擎）。
指针：lessons §四十四，core/rules/replayer.py

### 条目：syntax-jsonpath · JSONPath 超出子集（`[1:3]` 切片等）
状态：todo
依赖：ai-verify
优先级：P2
背景：`[*]` 与 `['键']` 已支持，切片类还没。
约束：扩展子集时要保证已有写法行为不变。
验收：切片类写法能回放，且有正反例测试。
指针：lessons §一，core/rules/replayer.py

### 条目：syntax-xpath · `//` 开头的 XPath
状态：todo
依赖：ai-verify
优先级：P2
背景：要引入 XPath 引擎，或者一直判 unknown。
约束：**别把 `//` 开头的写法静默当成 CSS 处理**（AGENTS #4）。XPath 引擎不在
  「已明确不做」的名单里——它是「还没定」（见 `xa-1111`）。
验收：要么引入引擎并给正反例，要么明确判 unknown 且原因一路走到用户眼前。
指针：lessons §二十六，core/rules/replayer.py

### 条目：syntax-bracket · 方括号索引式 `[-1]` / `[0]` / `[1,3]`
状态：todo
依赖：ai-verify
优先级：P2
背景：方括号索引式还没实现。
约束：与点式语义保持同一套归一。
验收：三种写法都能回放，并有正反例测试。
指针：lessons §一，core/rules/replayer.py

### 条目：syntax-range · 区间索引 `[0:10]`
状态：todo
依赖：ai-verify
优先级：P2
背景：区间索引还没实现。
约束：与切片语义保持一致。
验收：区间索引能回放，并有正反例测试。
指针：lessons §一，core/rules/replayer.py

### 条目：syntax-shorthand · `text.` / `children.` 简写
状态：todo
依赖：ai-verify
优先级：P2
背景：语义已查清，实现即可。
约束：判定半边一律 unknown 已是当前行为，别在这一项里顺手改判定。
验收：简写能回放，且判定半边行为不变。
指针：lessons §二十三 / §四十四，core/rules/replayer.py

### 条目：webview-content · webView 型正文（本地与 JVM 都验不了）
状态：todo
依赖：无
优先级：P1
背景：这一条不是「语法回放不了」，是「**取数**拿不到」——正文本身就是 `params`
  加密 + `xhr_mode`，图片地址只在解密后的 JS 对象里；漫画鱼章节页就是该形态，静态 HTML
  没有图片，运行时图片还会变成 `blob:` URL。
约束：**别把 `content_ok=None` 当成源有问题**；别为了「让本地能验」把 webView
  选项去掉。调试工作台要明确标出 L2/L3，静态网页视图不能框选时给出「使用本机引擎 / 连
  App 调试」动作；运行时 DOM / 命中片段的来源必须标明，不能把静态补抓冒充 App 页面。
验收：调试工作台对这类源能拿到运行时正文或明确给出不可判定原因；若能取得运行时 DOM，
  命中源码与框选高亮使用同一份材料；跑批那条路要么同样能验，要么结论里明确标「这类源跑批
  不可信」。
指针：lessons §四十九 / §六十 / §七十五，appservice/test/io/legado/app/service/ShadowBackstageWebView.kt

### 条目：unknown-outlet · 「没结论」缺产品出口
状态：todo
依赖：无
优先级：P1
背景：步骤级 unknown、动态正文和生成后未验证，界面上只有解释文案，没有把用户带到已经
  存在的调试工作台。
约束：优先回到当前源的调试工作台；只有本机引擎无法复现、需要用户网络出口或 WebView
  登录态时，才继续指向 `S5B-real`。不新造第三条验证通道。
验收：unknown 结果旁边有可点击动作；点击后能带着当前源、步骤和 URL 打开调试；需要真机
  时动作明确显示「连 App 调试 / 真机复检」，并保留原因。
指针：lessons §五十二，frontend/src/components/RuleDebugDrawer.vue

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

### 条目：strengthen-hint · 没验成时补一句可执行的话
状态：todo
依赖：无
优先级：P1
背景：生成或验证没成时，当前提示仍容易停在原因文字；用户不知道下一步应进入哪个步骤、
  该看哪份 HTML。
约束：文案只指向动作：打开调试工作台、选择失败步骤、查看命中源码或框选节点；不把未知
  说成规则失败，也不重复解释引擎机制。
验收：生成失败、验证 unknown、动态正文三种结果各有一句可点击的下一步提示，并能带入对应
  步骤和 URL。
指针：lessons §四十六 / §七十八，frontend/src/components/RuleDebugDrawer.vue

### 条目：strengthen-src · 给生成后的验证标出处
状态：todo
依赖：无
优先级：P1
背景：它是**生成时那一版规则**的结果，**改完规则要重验才作数**。今天这块 UI 没说，
  属于静默过期。
约束：验证结果必须标明本机引擎 / App 实测、生成时规则快照和当前规则是否一致；规则回填
  后只让对应步骤过期，不能把整份结果继续显示成当前结论。与抽屉「重新调试本步」同一条纪律。
验收：生成结果条上标明出处；改搜索、目录或正文任一规则后，对应验证结论显示过期并提供
  「重新调试本步」；未改动的步骤仍保留可用结论。
指针：lessons §七十八，frontend/src/components/SourceEditDialog.vue，frontend/src/components/RuleDebugDrawer.vue

---

## 3 · 待决策

### 条目：jvm-runtime-tmp · 明确 java.io.tmpdir 的归属
状态：blocked
依赖：jvm-runtime-snapshot
优先级：P2
阻塞于：先完成 JVM 实际运行时快照，确认 tmpdir 的真实来源与落盘位置
背景：当前 dump 没有 `java.io.tmpdir`；它可能由 JVM 按平台规则推导，也可能在未来被显式注入。
  在快照完成前，不能把「统一运行目录」表述成已经覆盖临时目录。
约束：根据一次真实快照二选一：① 在 dump / 注入点显式设置到 `data/` 下目录，并保持单一
  事实来源；② 明确记录 tmpdir 是平台继承值、不属于 dump 推导范围。不要同时保留两套来源，
  也不要把快照内容接入判定链。
验收：能用一次实际运行证明临时文件落点，并在一处明确写出 tmpdir 的来源；若选择显式注入，
  同时验证直起、常驻与 Gradle 的落点一致。
指针：core/jvm_direct.py，appservice/legado-test.init.gradle，appservice/test/io/legado/app/service/BrowserSession.kt

### 条目：ai-verify · 十-7 AI 提议验收换真引擎
状态：blocked
依赖：无
优先级：P2
背景：`core/repair/suggest.py` 的 `replay_step` 换成跑一次本机引擎；`dry_run` 免费的
  `preselect`（用 `replayer.parse_rule` / `extract_all`）要单独设计。
约束：**先改 AGENTS #3**（「验证必须由规则回放器完成」）与 lessons §二十六 / §七十三
  的决议，用户拍板后再动。
阻塞于：AGENTS #3 的决议需用户拍板（「验证必须由规则回放器完成」）
验收：AGENTS #3 改完，且 `replay_step` 换成真引擎后验收结论与今天一致或更好。
指针：lessons §二十六 / §七十三，AGENTS.md #3，core/repair/suggest.py

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
约束：要么改契约，要么让它继续走投影；两条选一，别两边都改。
验收：一张实测的「详情段想看的其实是搜索页的那块 DOM」样例，据此定契约或维持投影。
指针：lessons §七十五

### 条目：proj-3-attr · 末段是属性名的规则
状态：todo
依赖：无
优先级：P2
背景：兜底只认那五个动作词；属性名与标签名同形（`title` / `style`）。
约束：要做得先有与 `core/rules/replayer._is_attr_or_action_name` 对齐的词表 + 逐词
  比对测试（AGENTS #22⑤）。
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

### 条目：ten5-move · 把验证搬出生成 job（2026-09-21 决定先不做）
状态：todo
依赖：无
优先级：P2
背景：界面拿到源之后自动调 `POST /api/rules/jvm-debug`。它的两条主要收益已经成立；
  剩下的只有「把引擎调用挪出生成 job」这点整洁性，而代价是实打实的：多一次往返、
  慢的那半从「job 里等」变成「页面里等」、**关页面就白等**。
约束：**重估的触发条件**：「生成」要被非界面的调用方复用（CLI / 批处理）时再回来。
验收：触发条件出现时重估一次，给出「搬 / 不搬」的结论与依据。
指针：lessons §七十七 / §七十八

### 条目：decl-label · 声明式标签（后端给 provider + 深度 + 环境摘要）
状态：todo
依赖：无
优先级：P2
背景：前端只查表；抽屉 / 生成预览 / 跑批结果条**共用一份映射**。
约束：**等第三台引擎（真机复检 `S5B-real`）接进来时一起做**。今天只有两种来源、
  一个推导点，提前做等于拿 2–3 个组件的回归面去防一个还没到来的问题。
验收：`S5B-real` 接进来后，三种来源的标签由后端一处给出、前端只查表。
指针：lessons §五十二 / §八十

### 条目：tag-snapshot · 标签是快照，导入即开始过期
状态：todo
依赖：无
优先级：P2
背景：写进 App 的分组标签是校验瞬间的结论，App 侧无刷新通道。要动它属于大改
  （反向同步通道）。
约束：**先记录不排期**。
验收：给出「要不要做反向同步通道」的结论；做的话要有刷新语义的设计。
指针：lessons §五十二，AGENTS.md #17

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

### 条目：copy-kotlin · Kotlin 侧文案还没进机检
状态：todo
依赖：无
优先级：P2
背景：`TARGETS` 里没有 `appservice`，而 `render_reason` 这类串是**直接显示给用户**的。
约束：要加得先写一个跳过注释的 Kotlin 提取器（那个目录里中文注释远多于文案串，
  直接扫会把注释算成文案），加完还要有人守基线——**别只把目录塞进 `TARGETS`**。
验收：Kotlin 提取器能只抓文案串，且 `python tools/check_copy.py` 覆盖到 `appservice`。
指针：lessons §四十六，tools/check_copy.py

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

### 条目：done-engine-exit · 本地引擎退场（十-1~十-6）
状态：done
依赖：无
优先级：P1
背景：代理搬成全局两处注入；执行体、`check.*`、前端面板、CLI 五子命令、
  `data/check_cache` 全部退场。
约束：不留回退占位。
验收：见 lessons 对应节。
指针：lessons §八十六 / §八十七，git commit 9b29ece / 405db1e

### 条目：done-l4-verify · L4 生成验证的收尾验收
状态：done
依赖：无
优先级：P0
背景：把 L4「观察到的请求」那条链的边界验完（真靶子由浏览器桥扫出来，库里的源
  选不出来——能写出 `searchUrl` 的源恰恰不需要 L4）。
约束：受保护站点要先人工预热 profile；快照只覆盖加载期发出的请求，懒加载 / 滚动
  后才发的抓不到——这是设计边界，不要当缺陷修。
验收：见 lessons §八十九——观察链路在靶子上通（`GET dogemanga.com/_search` +
  95KB 响应体、判据齐全）；快照边界做成了**受控可复现演示**；顺带修掉一个真 bug
  （2 字节的 `CN` 被当成「拿到了页面」→ 生成链落一条 rc=0 的空源，原因被压掉）。
指针：lessons §八十九，git commit 88c48c6；
  证据脚本 `data/out/l4_probe.py` / `l4_snapshot_boundary.py`

### 条目：done-s5a · S5-A 调试通道（A1–A4 + D0–D2 + 第三期 matched_html 回填）
状态：done
依赖：无
优先级：P1
背景：通道 + cookie 注入 + 接进产品 + 直起 / 常驻 daemon + `matched_html` 回填。
约束：接口能力与协议见 lessons §五十一。
验收：见 lessons 对应节。
指针：lessons §五十一 / §五十九 / §六十 / §六十五 / §六十六 / §七十五

### 条目：done-app-engine · 引擎收成一台：健康改由 App 引擎判（B0–B3）
状态：done
依赖：无
优先级：P1
背景：校验与调试都收成 App 引擎；撤「证书问题」档；B3 调试迁移。
约束：见 lessons §七十二 / §七十三。
验收：见 lessons 对应节。
指针：lessons §七十二 / §七十三，git commit d7d6ceb

### 条目：done-l3-l4 · L3 判据补齐 + 正文规则两个前提 + 拦截页识别 + App gate
状态：done
依赖：无
优先级：P1
背景：`core/js_hints.py` 把页面引用的脚本纳入判据；引擎交回拦截页时不许当站点分析；
  判据并上 App 那一句 + `human_gate`；第三种「不是站点」（浏览器错误页）。
约束：见 lessons §八十一 ~ §八十三。
验收：见 lessons 对应节。
指针：lessons §八十一 / §八十二 / §八十三，core/js_hints.py

### 条目：done-l4-observe · L4：观察请求那一半
状态：done
依赖：无
优先级：P1
背景：桥开 `Network` 域只留 XHR / Fetch，侧车写 `network`；Python 侧
  `core/net_hunt.py` 挑「像数据的那一条」并把**响应**变成规则。
约束：条数 / 字节有上界。
验收：本地后端界面抓到若干条 Fetch、挑出与理由都对。
指针：lessons §八十四，core/net_hunt.py

### 条目：done-debug-layers · 调试体验：先定层，再写规则（九-1~九-4）
状态：done
依赖：无
优先级：P2
背景：定层 / 点选 / AI 前置条件 / 对数验收四批全部交付。
约束：层与动作的枚举只有一份。
验收：见 lessons 对应节。
指针：lessons §七十九，frontend/src/utils/layers.js，frontend/src/utils/selector.js

### 条目：done-replay-fixes · 本地回放已修的三条
状态：done
依赖：无
优先级：P2
背景：`bookUrl` / `chapterUrl` 的列表作用域、`text.` / `children.` 简写的判定半边、
  `_probe_toc` 接入 `tocUrl`（含 `verify.py` 把 tocUrl 当 URL 字符串的附带 bug）。
约束：**别再重复查**。
验收：见 lessons 对应节。
指针：lessons §二十三 / §四十四

### 条目：done-jvm-service · JVM 校验服务（来源阶梯 + 三段结论）
状态：done
依赖：无
优先级：P2
背景：来源阶梯（本地回放 < JVM < 真机）、JVM 结论存 meta 不写 checks、三条通道各管
  一段、每源总预算是软上界。
约束：见 lessons §四十八 ~ §五十四 / §六十二。
验收：见 lessons 对应节。
指针：lessons §四十八 ~ §五十四 / §六十二，README.md

### 条目：done-no-do · 已明确不做（决策记录）
状态：done
依赖：无
优先级：P2
背景：App 路线方案、逐源调试 WS 批量化、本地补 JS、星级收紧、appservice 拆独立
  仓库等，都已记了理由。
约束：那是决策记录，不是待办——别往这里加新条目。
验收：见 lessons 对应节。
指针：lessons §二十六

### 条目：done-ua-counts · 会过期的计数与运行口径
状态：done
依赖：无
优先级：P2
背景：auth / gfw 各有多少条看 `GET /api/sources/stats`；合并源的实测规模、UA 值、
  `CACHE_VERSION` 的当前值都属历史快照。
约束：**别在 TODO 里抄一份会过期的计数**（AGENTS #23）——历史可查 git / lessons。
验收：需要时现查接口或 lessons，不在文档里维护数字。
指针：lessons §三十一 / §五十三 / §七十二，README.md

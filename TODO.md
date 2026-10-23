# TODO

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）；机制与理由不在这里复述，
> 要看就翻代码与 `git log`。**已完成的事项从这里删掉**（AGENTS #10）。
>
> **结构由机检守着**：五档状态、四段分区、必填字段、P0 上限四条在
> `tests/test_todo_structure.py`（跟全量测试一起跑）。手改本文件前先看它。
>
> **历史批次（S1–S4、文案批 1、S5-A 的 A1–A4）不在这里复述**——机制看代码，
> 细节看 `git log`。本文件只留：还没做的 + 动手前的判据。

条目格式：`### 条目：<ID> · <标题>`，随后是行首锚定的字段行（值可续行，续行缩进 2 空格）。
必填 `状态：` `依赖：` `优先级：` `背景：` `约束：` `验收：` `指针：`；`open` 另须
`子项：`，`blocked` 另须 `阻塞于：`。指针只接受**路径 / git commit**。

---

## 0 · 现在做

> 后续调试体验主线：“入口降噪 → 工作台闭环 → 证据前置 → 编辑能力合入 → DOM 意图化生成”。

---

## 1 · 排队

### 条目：local-verify-l2-l4 · 本机材料快验：L2–L4 不依赖 App 验收
状态：todo
依赖：无
优先级：P1
背景：AGENTS #3 写的是「验证由本机引擎（App 真源码）完成」，但今天只有 L1 真成立：`rule_try`
  的入参是一份现成 HTML，只回答「选不选得中」，L2–L4 的验收只能回真机。目标是把验收权威从
  真机下移到常驻 daemon + CDP 桥，让 L2–L4 也有秒级试取。架构一句话：**快验 = 取材料 +
  规则求值**，而求值那一半不用改——`RuleTry.run` 走 `AnalyzeRule`，CSS / `@js:` / `$.` JSON
  规则本来就都认，缺的只是「对味的 body」。三处现状说明**不必新造取数原语**：
  ① 「取渲染后的材料」产品里已经有一个动作——「重调本步验证」那条单步调试（`--URL` 正文起步），
  源声明了 webView 时它带回来的页面就是引擎手上的渲染后 DOM，而 `fetch_debug_pages` 对这份材料
  **一个请求都不发**（`pages[].origin == "engine"`）；② 材料出身的账**已经在结果里**：
  `pages[].origin` / `fetched_at` / `len` / `truncated` 加运行级的 `result.cookie_len` /
  `cookie_note` / `webview_unsupported` / `hint`——缺的是界面把它当判据用；③ 另外两层的材料也已
  具备：`ruleContent.webJs` 走 `getStrResponseAwait(jsStr=…)`、`isRule=false`，本机 CDP 路径
  **已经能跑页面环境 JS**（只有 `isRule=true` 的 `getWebJsResult` 报 `unsupported_is_rule`），
  L4 的抓包材料 `BrowserBridge.networkRequests` → `result.network` 已经带回。
  **真实数据（2026-10-21 只读统计：3139 条在用源 + 26 个已跑任务共 95 步）**：层分布 **L1 71 ·
  L3 24 · 判不出 2**，**L2 / L4 各 0 例**；那 24 个 L3 步**全是同一条源**（口袋漫画，content 步，
  `verdict=unknown`、detail 是「正文规则为空…补上规则再重新调试本步」、`origin=fetch`、
  `shadow_rendered=0`）；声明 `webJs` 的源 6 条、任一处声明 `webView` 的 96 条（3%）；保留的 26 个
  运行目录 **26/26 未渲染**。→ 真实出现的痛点是「**规则为空 + 没走渲染**」，不是「材料是另抓的」；
  用力点按 **L3 ≫ L2/L4** 排。
  分期（每期一个可独立回退的提交）：
  - 期 A（先做，前端 + 1 行后端）：**材料出处 + 「这次渲染过没有」两件事摆出来**。出身行写
    `材料：引擎那份（0 请求）· 这次没走渲染` / `另抓的一份（抓取于 …）`，加 `带登录态 N 字符` /
    `没带登录态`（`cookie_note` 现在被 `jvm_debug` 丢掉，顺手带出来——「没带」要说得出为什么）。
    **判据是「层 × `rendered`」，不是 `origin`**：`origin == "engine"` 只说明那份 HTML 是引擎取回来的，
    源没声明 webView 时它同样是静态页——2026-10-21 本地样张实测：未开 webView 时 `origin=engine`、
    `shadow_rendered=0`、层 L2、正文取不到；章节点 URL 带上 `{"webView":true}` 后 `shadow_rendered=1`、
    层 L1、正文取到。`rendered` 从 sidecar 的 `shadow_rendered` 带出来（Python 侧一直没带）。
    那句话按层说、不推假动作：L2 → 「给那条 URL 规则加 `,{"webView":true}` 再重调本步」；
    L3 → 「加 webView 后重调，再看运行时形状」；`rendered === null`（连 App 通道没这份事实）→ 半句
    都不写（读不到 ≠ 没渲染）。**按钮措辞保持「重调本步验证」**：`rendered === false` 时再调一次
    结果一样（源没声明），叫「取渲染材料」是假承诺。**`assume_webview` 不做**。这次的本地
    样张（静态页 + `setTimeout` 填充 + 章节点 URL 带不带 `{"webView":true}` 两步对照）**尚未
    固化成脚本**；要落盘时按「手动跑，不挂 unittest」写（它要 JVM 与浏览器），并在注释里记下
    两个坑：`http.server` 默认服务**进程 cwd**、样张要留一个空壳容器才会被判成 L2。
  - 期 A′（**先量后处理，不是先写 op**）：已量到暖机单步调试 **27.9s = 引擎 10.1s + 我们自己的
    后处理 ~18s**（`fetch_debug_pages` 的补抓 + `classify_with_scripts` 判层取外部脚本）。而**材料
    不随规则变**（本条目约束第一条）：一次取材料之后，同页所有规则迭代都只花 `rule_try` 的 0.06s。
    所以先做「**只要材料**」模式：为取材料跑单步调试时跳过补抓 / 判层取脚本 /（必要时）matched 与
    structured 采集，直接用引擎交回来的 `engine_html`——目标 ≤12s，**零 Kotlin**。开工第一步是把
    那 18s 拆开量一次（引擎链 vs 采集 vs 补抓/判层），量完再决定要不要动引擎侧；**只有实测证明还不够
    快**，才谈新 op（那时按下面那条纪律做：走 `AnalyzeUrl`、留着 `,{...}`、被占用如实拒绝）。
    开工前先把 sidecar 里那三项事实带出来（`shadow_webview_calls` / `shadow_rendered` /
    `shadow_last_reason`）——它们是「这次走没走渲染、为什么没走」的现成账，与期 A 的判据同一份事实。
  - 期 B0/B0′（草稿生成，**已收掉**）：重启前先满足三条——同类形状的源够多 + 判据落在事实层
    + 不与 `core/agent_plan` 争判据位；理由见 `git log`（2026-10-21 收掉那次提交），渲染那半条约束本条目已有。
  - 期 B0″（便宜且真实，与上面无关）：`ruleContent.webJs` 非空但整条源没有任何 `webView` 声明 =
    那段 JS 是死的（库里今天 2 条：西瓜看书、轻小说文库2.0——后者把 `{'webView': true}` 塞进了
    `webJs` 字段）。校验里给一句提示即可。
  - 期 B1（等真要做草稿生成时再谈）：候选 `webJs` 写进草稿源再重调本步（引擎真跑、事件流里有结论，正是
    AGENTS #3 的分工）；只有「一轮多条候选必须共享同一份材料」被实测证明是瓶颈，才引入材料机 +
    三条 JS 闸（只读 / ES5 / 返回值可序列化）。
  - 期 C（L4）：同样先用现成循环试（把 `sourceRegex` / 接口规则写进草稿重跑）。**真实数据里 0 例**，
    排在最后：只有「同一份接口响应上连试多条 JSONPath」成为瓶颈时才做接口材料机。
  - 期 D（L5）：本机会话（`scripts/jvm_login.py` 预热 + `SourceCookies` 注入）接进同一台材料机。
  - 期 E（收口）：AI 自查改成按层试取、撤掉「未验证草稿」标记；AGENTS #3 与 README 措辞同步。
约束：材料是 `(层, url, 会话, 时间窗)` 的函数、**不随规则变**——AI 一轮 N 条候选共享同一份材料，
  规则改了绝不重取；材料必须带 `fetched_at` 并显示出来。**不新增取数原语**（除非期 A′ 的数字够得上）：
  自己驱动 CDP 取 `outerHTML` 等于在 Python 里重实现 App 的取数语义（UA / cookie / URL 选项 /
  页面 `js` 选项 / 嗅探）——`xhr_mode` 那类源会得到空页并判成「源取不到」；而
  `BrowserSession.get()` 在固定 profile 被占用时会**静默换临时 profile**（登录态就没了），
  试取要是排队等调试跑完又会废掉秒级承诺。真要做定向 op 时的纪律：一律走 `AnalyzeUrl`
  （继承 headers / cookie / 代理 / `concurrentRateLimiter` 限速）；URL 里那串 `,{...}` **必须留着**
  交给 `AnalyzeUrl` 解析（与 AGENTS #20 剥选项的方向相反，理由要写在代码注释里，否则下一个人剥掉它，
  渲染永远不生效）；被占用就如实拒绝并给一个动作，**不静默换 profile 当成功**；材料种类 / 地址 /
  字节 / 耗时 / 是否命中缓存要能看出来。候选 `webJs` 三条闸：只读、ES5、返回值可序列化（静态拒收
  `fetch(` / `XMLHttpRequest` / 写 DOM）——这是「别让一条错规则搅了页面」，**不是安全边界**，别在
  文档里说成安全边界。试取如实反映源当前声明：`webJs` 只在某条
  URL 规则开了 `webView` 时生效，**不偷偷替它开**，把 `source_declares.webview=false` 带出来。
  **本机覆盖不了的那几类现在先不做**（`isRule=true` 的页面环境 JS 桥、真机环境差异、登录态）：
  碰到时前端只出一条告警（说清是哪一类 + 给「连 App 调试」这一个动作），不产生假结论，也不在本机
  重实现 `WebJsExtensions` / `$JSBridgeResult`。不把「本机试取」说成真机验收：结果里必须标材料
  种类与新鲜度——本机与真机差在登录态、网络出口、UA。
验收：期 A——出身行同时读出「材料出处」与「这次渲染过没有」，并按层给出那句可执行的话（L2 → 加
  `{"webView":true}`；L3 → 加 webView 后看运行时形状），`rendered === null` 时不出现「没走渲染」；
  快验全程零请求（材料来自任务行）。**验收靠本地样张**（手工跑，脚本未落盘）：章节点 URL
  不带 webView → `L2 / origin=engine / rendered=0 / 正文取不到`；带上 → `L1 / rendered=1 / 正文取到`
  （2026-10-21 手工实测通过）。期 A′——先给出那 ~18s 后处理的拆分量与「只要材料」模式的实测
  秒数（目标 ≤12s）；只有量完证明还不够快，才谈引擎侧与新 op。
  期 B1/C/D——候选 `webJs`（写进草稿重跑）与 L4 接口材料各一条真实源在本机材料上取到值，全程不
  连真机；AI 一轮 N 条候选只取一次材料（材料行数 = 1）；材料账本能看出种类 / 地址 / 字节 / 耗时 /
  是否命中缓存；撞到必须连 App 的场景时界面只有一条告警、没有结论、主动作是「连 App 调试」。
  不联网的钉子：Kotlin 侧照 `RuleTryTest` / `BrowserSniffTest` / `BrowserBridgeNetworkTest` 的形态，
  Python 侧 `tests/test_rule_try_contract.py` 守住 op 与应答形状；材料出处那条另有
  `frontend/src/utils/debugMaterial.test.js` 与本地样张脚本。
指针：core/rule_try.py，core/app_debug.py，frontend/src/utils/debugMaterial.js，
tests/test_rule_try_contract.py

### 条目：jvm-range-disabled-source · 校验时把「已停用」与「不存在」分开说
状态：todo
依赖：无
优先级：P2
背景：勾到停用源时只报「一条都没匹配上（可能已删除，或 URL 改过）」，分不出是停用还是不
  存在。表格已经能显示「已停用」（`tagCellsOf` 从 `enabled` 派生），缺的是提交时的具体提示。
约束：确切的停用 URL 要单独查一次——导出结果里它与「不存在」长得一样；不要为了提示把停用
  写进 `group_name`。停用源是否纳入显式勾选的范围要先定，筛选 / 全量仍跳过。
验收：勾一条停用源时，界面说的是哪几条已停用、不会被校验。
指针：backend/api/jvm.py，backend/jobs/jvm_exec.py

### 条目：import-preview-conflict-count · 预览里的冲突数不该随策略变
状态：todo
依赖：无
优先级：P2
背景：`POST /api/import/preview` 的 conflict 与 update 是 `elif` 互斥分支：选「保留现有的」
  时同一批显示「规则冲突 N」，切到「用导入的覆盖」就变成「将覆盖 N」、「规则冲突」显示 0
  ——同一个文件在两种策略下冲突数不同，覆盖时看不出有几条是冲突、会被覆盖。
约束：冲突计数与策略解耦（冲突永远计，覆盖时另加 update），覆盖的正当性正是建立在「预览
  已把冲突摊开」之上；口径只改这一处，不动 `POST /api/import` 的判定。
验收：同一次预览在两种策略下「规则冲突」数一致；覆盖策略下两个数字同时可见，各有断言。
指针：backend/api/imports.py，frontend/src/components/ImportDialog.vue

## 2 · 按需

### 条目：s5a-a1 · A1 唯一没做完的验收：与设备 WS 逐事件对拍
状态：todo
依赖：无
优先级：P2
背景：结构同构已由契约测试保证；逐事件对拍需要手机开着 Web 服务，等设备在场时补。
约束：对拍要逐事件，不接受「结构同构」代替。
验收：设备在场时跑一次逐事件对拍，差异逐条有归因。
指针：tests/test_jvm_debug_contract.py

### 条目：source-edges · 源关系边表：合并 / 换址 / 忽略记成一张账
状态：todo
依赖：无
优先级：P2
背景：Legado 拿 `bookSourceUrl` 当身份，整理动作其实都在回答「这几行 URL 是不是
  同一个源、现在哪个地址算数」。用一张边表把**人确认过**的关系存下来（取代原
  dup-keep，吸收其「忽略也要持久化」的诉求）：
  `source_edges(from_url, to_url, kind, decided_at, note)`，kind = merged_into /
  superseded_by / ignored。撤销 = 删一条边；行生命周期不动（软删 / 回收站照旧），
  列表与导出按「没有出边的行 = 正典行」过滤。
约束：边只由人点确认写入，机器测算的疑似关系不进表（AGENTS #11：派生关系要
  可追溯）；**别塞 settings**——`POST /api/settings/reset` 会静默清掉，「用户对
  数据的决定」一律存库；「在用 URL 唯一」不受影响（AGENTS #9）。换址替换
  （superseded_by）是新动词：旧的下岗、用户标签跟到新条、继承关系留档。
验收：三种动词各能写入并撤销一条边；重启与 settings reset 后边还在；列表与
  导出正确过滤正典行，软删 / 回收站行为不变。
指针：core/store.py，services/merge_sources.py

### 条目：webview-material · 设备不在场时的运行时 DOM 材料（原 webview-material-fallback）
状态：todo
依赖：agent-layer-router, agent-action-schema
优先级：P1
背景：设备不在场时，撞 WebView 能力边界的源要能拿到**渲染后的运行时 DOM** 材料——CDP 桥本来就在渲染、`EngineHtmlCollector` 本来就在收整页，几乎零新基建。同族还差两处：撞边界而 App 通道不可用时那个按钮点了没用；`matched`（命中回填）阶段的缺口只影响证据、不影响结论，不该占首屏判据位。
约束：**只出材料、不判结论**（判定仍回 App 或交人工 / AI）；材料口径用 `MATERIAL_KINDS` 的 `runtime_dom`，不新造第三种语义；不许把「材料」说成「验证」；App 通道不可用时改给「本机渲染取材料」或明说原因，不给点了没用的动作（判据 context 里已有 `capabilities`）；`matched` 阶段缺口只进证据区提示；判据仍然只有 `core/agent_plan` 一处；运行时 DOM、命中片段和框选高亮必须来自同一份运行时材料，不能把静态补抓冒充 App 页面；静态网页视图不能框选时给出**取证**动作，不许写成解决方案（口径见 AGENTS #24）。
验收：设备不在场时，撞边界的源仍能拿到渲染后的运行时 DOM 材料，界面明确标这是取证而不是结论；撞边界且 App 通道不可用时，界面给的是能执行的动作或明确原因；`matched` 阶段缺口不出现在五格主动作位；若取得运行时 DOM，命中源码与框选高亮使用同一份材料。
指针：core/js_hints.py，frontend/src/utils/webviewCapability.js，tests/test_page_layer.py，
skills/legado-book-source/SKILL.md

### 条目：agent-evidence-budget · 限制 Agent 取证范围与调用次数
状态：todo
依赖：agent-context, agent-action-schema
优先级：P2
背景：Agent 的价值是压缩调试循环，而不是扩大模型上下文；需要先固定按需取证工具和每轮预算，防止模型反复索取整页材料。
  2026-10-16 工作台减法后主流程是「规则输入 → 本步实测摘要 → 材料」，诊断与建议块连同前端决策词表/Agent 连跑文件已删（`debugDecision.js` / `debugAction.js` / `agentBudget.js` / `useAgentSession.js` 等）；Agent loop 重接时按 core/agent_actions 权威词表重建前端契约，入口长在 inline-rule 旁。
  同批发现：详情步（bookUrl）的 `matched_html` 引擎未覆盖——Kotlin `DebugService.MATCHED_STEP_NAMES` 没有 bookUrl（详情段是 ruleBookInfo.* 字段规则族，不是单条列表/正文规则）；补全要加 Kotlin 分支 + Python 契约同步（tests/test_jvm_debug_contract.py::TestMatchedStepNameParity）。
  结构化采集（2026-10-17 深度审修后**真引擎对拍通过**，Agent 的验收材料接口）：Kotlin `StructuredResult.kt` 用引擎已取回的整页 + App 解析器算每步字段值（搜索段是 `BookList.analyzeBookList` 的完整书目，不止首条），写侧车 `structured` 键（带版本轴 `v`）；段→URL 由 App 的 payload 管道枚举（`Debug.EventKind`）直接配，列表段按运行入口（`key.startsWith("发现::")`）只记一个段名。解析用**完整 body**（内存上限 MAX_PARSE_CHARS，4MB），侧车仍按 MAX_ENGINE_HTML_CHARS 截断——截断的 JSON 硬解析会产出「字段全空」的假结果，两份材料分开（同一成因：dirs 页 40 万字符曾让目录段结构化缺席，已修）。`AnalyzeUrl` 按 App 方式构造（searchUrl + key），搜索规则里的 `{{key}}` 才有值；详情段 `setRedirectUrl` 对齐 BookInfo（URL 字段绝对化）。worker 线程 + 主 looper 驱动（同 matched）。列表项只留定位字段（无 intro/cover），Python `structured_map` 兜一层逐字段截断（上限两侧契约测试钉住）。实测（SF轻小说）：search total=10 与事件一致、bookUrl cover/toc_url 绝对化、toc 20 条样本、explore 不再多算。Agent 验收读 `step.structured` 做第二样本复核。
约束：只允许读取证据行附近片段、运行时键摘要、字段类型/长度、网络响应形状和指定脚本命中片段；禁止读取完整 Cookie、密钥和无界 bundle；每轮最多一次模型调用和有限次取证，超过预算转为用户动作；预算与错误原因写入结果，不静默截断。
验收：完整 HTML/脚本不会默认进入模型请求；超出预算时界面显示具体原因和手动入口；同一失败不会自动循环调用；脱敏与截断有单测覆盖。
指针：backend/api/rules.py，core/js_hints.py，frontend/src/components/DebugWorkbench.vue，appservice/test/io/legado/app/service/DebugService.kt（MATCHED_STEP_NAMES），tests/test_jvm_debug_contract.py

### 条目：webview-render-fidelity · 渲染自有 html 时上游语义还没对齐的几处
状态：todo
依赖：无
优先级：P2
背景：`render(content=…)` 的两条分支里，**无 baseUrl 那一支（`Page.setDocumentContent`）没法声明编码**：CDP 这个命令没有 charset 参数，而 html 自带 `<meta charset="gbk">` 时浏览器会按 gbk 解析我们送去的 UTF-8 字节 → 乱码。`Fetch` 那一支不受影响（响应头 `Content-Type: …; charset=utf-8` 在 HTML 的编码判定里优先于 meta）。同主题还有三处上游语义没对齐，都是**所有渲染共用**的老缺口、不是这次引入：`blockNetworkImage = true`（上游拦图片）、UA 取自 `headerMap`、`cacheFirst` 的缓存优先。
约束：修编码要**始终按 UTF-8 送**并用前 1024 字节内的 `<meta charset="utf-8">` 强制解析——照搬 `encode` 去转码反而会把已解码的字符串弄坏；图片/UA/缓存三处要么照上游实现、要么在注释里写明是近似，别让人读成「已完全对齐」。
验收：一个自带 gbk 声明的 html 走无 baseUrl 分支，取回的文本与原文逐字一致；另三处各自有明确的「实现或标注为近似」的结论。
指针：appservice/test/io/legado/app/service/BrowserBridge.kt，上游 `BackstageWebView.kt`

## 3 · 待决策

### 条目：batch-condition-axis · 结论行记录当次执行条件
状态：todo
依赖：无
优先级：P2
背景：结论行今天**没有任何「当次条件」轴**：`keyword_used` / `webview_stripped` / UA 都只在
  引擎侧产出，`core/jvm_health.checks_row` 落库时丢掉了——历史结论与新结论不是同一口径却看不出来
  （AGENTS #5b）。两个下游都在等它：① JVM 搜索对单关键词跑，某源没结果可能是「没这本书」，
  源自带的 `checkKeyWord` 被采纳但没落库，结论行分不出这次用的是哪个词（实测 2026-10-09：
  1100/3761 条声明了 `checkKeyWord`；库里 359 条是「搜索真跑过、没命中」，health=pending 且
  `search_probed=1`）；② UA 要与设备对齐，得先把当次实际用那条记进结论。
约束：**记录轴 ≠ 改值**——加轴不改变任何请求条件，UA 取值仍不动（改值会让站点返回的页面变，
  与库里已有结论不可比）；轴新增或编号含义平移都要 bump `CACHE_VERSION`（AGENTS #5b）；
  落库只加列、不改判词。
验收：结论行能记下当次关键词与 UA（以及 `webview_stripped`），新旧口径可区分；在此之上做一次
  `no_result` 复核批次——单关键词无结果的源经多词复核后才判坏。
指针：AGENTS.md #5b，core/jvm_health.py

### 条目：note-half · 正文附注只做了一半：疑似错误页
状态：todo
依赖：无
优先级：P2
背景：JVM 面会带「正文较短」的附注；「疑似错误页」那一半要**全文**匹配
  `CONTENT_NOISE_MARKERS`，而引擎侧只留 `content_sample`（60 字，`content_len`
  是全文长度）——**结论行里没有 `content_sample` 这一列**，别去库里找。
约束：要做得先把它放宽（上限 `SHORT_CONTENT_CHARS`）；报告与列表要用**同一个函数**。
验收：疑似错误页的附注在报告与列表上都出现，且用的是同一个函数。
指针：core/quality.py

### 条目：xa-1111 · XPath 规则在引擎侧的结论
状态：todo
依赖：无
优先级：P2
背景：本地回放器退场后，XPath 是否被支持**只由 App 引擎决定**：`//` 开头的规则在
  `AnalyzeRule` 上是走 XPath 还是被当成 CSS 失败，要给一条实测结论。
约束：别在 Python 侧再补一个 XPath 判断（那正是这次删掉的东西）；结论以引擎实测为准，
  取不到时原因要一路走到用户眼前（AGENTS #4）。
验收：一条带 `//` 规则的源在本机引擎下的实测结论 + 界面上的归因。
指针：core/jvm_debug.py

### 条目：explore-depth · 批量校验要不要加「发现页」深度档
状态：todo
依赖：无
优先级：P2
背景：上游校验有五开关（搜索/发现/详情/目录/正文，`checkDiscovery` 级联到发现页
  第一个分类）；本机引擎的 depth 只有 search/toc/content 三档，死在发现页的源
  （exploreUrl 配错/失效）验不出来。
约束：`PROBE_DEPTHS` 是「只增不减、编号含义稳定」的轴，加档必须 bump
  `CACHE_VERSION`（AGENTS #5b）；失败归因走 root_kind/health_for 那一份，引擎侧
  别另造判词；先实测「库里带 exploreUrl 且搜索通过」的源规模，没有数量就不值得加。
验收：给出一次实测的规模数字 + 用户拍板；若做，新档位有正反例测试与
  `ServiceJson` 形状钉，结论行的 `stage` 词表同步扩。
指针：appservice/test/io/legado/app/service/ValidateService.kt，core/settings_store.py，
AGENTS.md #5b，core/settings_store.py

### 条目：structured-provenance · structured 材料的请求来源记进侧车
状态：todo
依赖：无
优先级：P2
背景：structured 二次解析用的规则可能带 `@js:`，里面的 `java.ajax` 会**新发请求**——
  structured 的值可能来自事件流之外的另一次抓取，与「用引擎手上那页」的注释承诺
  有出入，侧车里没有记录材料来自哪次请求。
约束：失败纪律同 matched（只记 fails 不动判定）；不要为了记录去改上游 Debug 流。
验收：侧车里能看出 structured 各段的页是事件流那次还是另发的请求；出现第一例
  「structured 值与事件对不上」时这条是排查入口。
指针：appservice/test/io/legado/app/service/DebugService.kt（runStructuredWithPhase），
StructuredResult.kt，core/app_debug.py

### 条目：sidecar-per-page-render-facts · 侧车按页记渲染事实
状态：todo
依赖：无
优先级：P2
背景：侧车只记「最后一次渲染」的 url / js_len / reason（`shadow_last_*`），所以除末页外
  每一步的 `material_kind` 都是 `unknown`；`core.rule_try.unfaithful_reason` 因此只能
  「说不清就照跑」——快验在非正文步无法收紧，页面脚本材料漏判的那一档也拦不住。
约束：**只加键**（`shadow_render_calls`：每次调用的 url / js_len / ok / reason 列表），
  旧引擎读不到就沿用「最后一次」那一档；`_annotate_render` 由按单地址匹配改成按列表匹配；
  键名与形状两侧由契约测试钉住（同 AGENTS #22）。
验收：同一源同一页，非末页也能拿到 `rendered` / `render_js`；`unfaithful_reason` 的
  unknown 档可收紧为「拒绝」并补用例；`material_kind` 的四档在新侧车下各有实测例。
指针：core/jvm_debug.py（_annotate_render），appservice/test/io/legado/app/service/DebugService.kt，
tests/test_jvm_debug_contract.py

---

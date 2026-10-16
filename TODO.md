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

> 后续调试体验主线：“入口降噪 → 工作台闭环 → 证据前置 → 编辑能力合入 → DOM 意图化生成”。

---

## 1 · 排队

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
指针：lessons §五十一，tests/test_jvm_debug_contract.py

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
指针：lessons §三十四 / §十，core/store.py，services/merge_sources.py

### 条目：agent-pocket-comic · 口袋漫画 L3 WebView 正文专项策略
状态：todo
依赖：agent-layer-router, agent-action-schema
优先级：P1
背景：口袋漫画正文页的图片地址在 WebView 执行后的 `params.chapter_images`，静态 HTML 没有图片，运行时图片还可能变成 `blob:` URL；图片签名会过期，目录选错还会导致重复章节。第一版应验证运行时数据，不应让用户配置 AES 或复制旧图片地址。
  前置能力（原 webview-material-fallback 条目并入）：设备不在场时，撞 WebView 能力边界的源要能拿到**渲染后的运行时 DOM** 材料——CDP 桥本来就在渲染、`EngineHtmlCollector` 本来就在收整页，几乎零新基建。同族还差两处：撞边界而 App 通道不可用时那个按钮点了没用；`matched`（命中回填）阶段的缺口只影响证据、不影响结论，不该占首屏判据位。
约束：入口要求用户选择具体章节；Agent 只生成 `requires_webview / runtime_field / content_mode` 策略；本机/App 实测确认 `params` 已为对象、`chapter_images` 非空且图片可访问后，才生成 ES5 `webJs + content + imageStyle`；调试工作台明确标出 L2/L3，静态网页视图不能框选时给出**取证**动作（「用本机引擎 / 连 App 取运行时材料」），不许写成解决方案（口径见 AGENTS #24）；运行时 DOM、命中片段和框选高亮必须来自同一份运行时材料，不能把静态补抓冒充 App 页面；每次调试重新获取图片地址；批量校验不能可靠覆盖这类源时必须明确标注；章节与图片失败原因不得压成“解密失败”。
  取材料那条的前置约束（原 webview-material-fallback）：**只出材料、不判结论**（判定仍回 App 或交人工 / AI）；材料口径用 `MATERIAL_KINDS` 的 `runtime_dom`，不新造第三种语义；不许把「材料」说成「验证」；App 通道不可用时改给「本机渲染取材料」或明说原因，不给点了没用的动作（判据 context 里已有 `capabilities`）；`matched` 阶段缺口只进证据区提示；判据仍然只有 `core/agent_plan` 一处。
验收：选定一章能显示页面、WebView、运行时字段、图片数量和可访问性；成功时生成源草稿正文规则并由 App 实测复验；params 仍为字符串、图片为空、签名过期、目录地址不具体、静态页面无运行时材料时分别给出对应下一步；若取得运行时 DOM，命中源码与框选高亮使用同一份材料；批量校验对这类源要么同样能验，要么结论明确标「这类源批量校验不可信」；不在源中硬编码站点 AES 或图片 URL。
  取材料那条的验收（原 webview-material-fallback）：设备不在场时，撞边界的源仍能拿到渲染后的运行时 DOM 材料，界面明确标这是取证而不是结论；撞边界且 App 通道不可用时，界面给的是能执行的动作或明确原因；`matched` 阶段缺口不出现在五格主动作位。
指针：skills/legado-book-source/SKILL.md，core/js_hints.py，tests/test_page_layer.py，data/app_probe/source.json，lessons §七十五；取材料部分另见 appservice/test/io/legado/app/service/BrowserBridge.kt，appservice/test/io/legado/app/service/DebugService.kt，frontend/src/utils/webviewCapability.js

### 条目：agent-evidence-budget · 限制 Agent 取证范围与调用次数
状态：todo
依赖：agent-context, agent-action-schema
优先级：P2
背景：Agent 的价值是压缩调试循环，而不是扩大模型上下文；需要先固定按需取证工具和每轮预算，防止模型反复索取整页材料。
  2026-10-16 工作台减法后主流程是「规则输入 → 本步实测摘要 → 材料」，诊断与建议块连同前端决策词表/Agent 连跑文件已删（`debugDecision.js` / `debugAction.js` / `agentBudget.js` / `useAgentSession.js` 等）；Agent loop 重接时按 core/agent_actions 权威词表重建前端契约，入口长在 inline-rule 旁。
  同批发现：详情步（bookUrl）的 `matched_html` 引擎未覆盖——Kotlin `DebugService.MATCHED_STEP_NAMES` 没有 bookUrl（详情段是 ruleBookInfo.* 字段规则族，不是单条列表/正文规则）；补全要加 Kotlin 分支 + Python 契约同步（tests/test_jvm_debug_contract.py::TestMatchedStepNameParity）。
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
指针：appservice/test/io/legado/app/service/BrowserBridge.kt，上游 `BackstageWebView.kt:100-155`

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
指针：AGENTS.md #5b，core/jvm_health.py，core/checker.py，lessons §五十一 / §五十三

### 条目：note-half · 正文附注只做了一半：疑似错误页
状态：todo
依赖：无
优先级：P2
背景：JVM 面会带「正文较短」的附注；「疑似错误页」那一半要**全文**匹配
  `CONTENT_NOISE_MARKERS`，而引擎侧只留 `content_sample`（60 字，`content_len`
  是全文长度）——**结论行里没有 `content_sample` 这一列**，别去库里找。
约束：要做得先把它放宽（上限 `SHORT_CONTENT_CHARS`）；报告与列表要用**同一个函数**。
验收：疑似错误页的附注在报告与列表上都出现，且用的是同一个函数。
指针：lessons §二，core/quality.py

### 条目：antibot-jvm · anti-bot 墙在 JVM 侧仍报 no_result / error
状态：todo
依赖：无
优先级：P2
背景：`login_wall` 只认 `LOGIN_MARKERS`（「请登录」这类）；验证码 / Cloudflare 那类
  （`ANTI_BOT_MARKERS`）没有对应状态，于是「需人工过一下」被读成「没出结果」。
  异常归因已机器可读（`root_kind`）；anti-bot 是**页面内容**特征，不是
  异常分类，别塞进那个码表。
约束：判据照 `core/checker.is_login_wall` 那套，**别新造**。
验收：这类源不再被判成 no_result，而是有一个「需人工过一下」的状态。
指针：lessons §五十八，core/checker.py

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
AGENTS.md #5b，lessons §五十三

---

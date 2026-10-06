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

> 当前排期：引擎与调试执行链的基础设施已完成；后续调试体验以“入口降噪 → 工作台闭环 → 证据前置 → 编辑能力合入 → DOM 意图化生成”为主线。worker 的 lane、分块恢复、daemon 复用与冷启动批前准备、任务详情的块级证据与执行时间线均已交付（lessons §六十五 / §六十六）。
> P0 的 unknown 出口与可执行提示已交付，当前前端待办集中在新调试页的视觉层级、编辑闭环和证据可信度；本机引擎的启动耗时观测与重复准备优化尚未完成。

---

## 1 · 排队

### 条目：jvm-request-coalesce · 合并重复的进行中请求
状态：blocked
依赖：jvm-coalesce-measurement
优先级：P2
背景：同一源、同一规则快照和同一 JVM 参数可能由调试抽屉、生成后验证和批量入口重复提交；单纯排队只能延后重复工作，不能减少请求和站点压力。但它不是当前“重启后单条校验慢”的根因，必须等 worker 和分块边界稳定、且有重复提交数据后再决定是否实现。
约束：合并键必须包含归一化 URL、规则/源快照、阶段、验证深度、搜索词及本次运行参数；不能只按 URL 合并。只合并仍在运行或可复用的同口径任务，每个调用方仍有自己的 job 观察关系；取消一个观察者不能取消共享执行，除非没有观察者且明确执行取消。
验收：只有在日志证明重复提交达到值得优化的数量后才实施；实施时完全相同的重复提交只产生一次 JVM 执行和一份底层结果，任一调用方都能收到同一结论及来源，任一合并键字段变化都会产生独立执行，旧结果不会静默复用。
阻塞于：先统计重复提交率；没有数据证明收益前不实现。
指针：backend/jobs/runner.py，backend/api/jvm.py，core/jvm_debug.py，AGENTS.md #5b，lessons §五十三 / §七十八

### 条目：jvm-startup-latency · 本机引擎启动耗时与重复准备优化
状态：todo
依赖：无
优先级：P1
背景：本机引擎首次响应前可能经历 readiness、JVM lane 排队、daemon 准备、daemon 请求或 Gradle 回退；当前已有 daemon 复用和批前准备，但缺少分段耗时证据，无法确认“每次启动慢”究竟卡在哪一段。
约束：先记录 readiness、排队、daemon prepare、daemon request、Gradle fallback 的耗时与执行模式，再改策略；daemon 忙时不能杀正在执行的进程，也不能把忙/不确定静默当成源失败；readiness 与 Kotlin source signature 的缓存必须有源码、classpath、Java、SDK、runtime snapshot 等失效条件，不能只按 TTL 复用旧环境；启动阶段文案必须来自真实执行路径。
子项：
- jvm-startup-measure · 启动阶段分段测量
- jvm-daemon-busy-policy · daemon 忙时短等待或回退的策略评估
- jvm-readiness-cache · readiness 与 source signature 重复扫描优化
- jvm-daemon-warmup · 受控 daemon warm-up
验收：时间线或任务详情能区分各启动阶段及执行模式；daemon 热复用时不重复做不必要的完整准备；daemon 忙时按明确策略等待或回退并显示原因；源码或运行环境变化后不会复用旧 daemon；冷启动、热复用、忙回退三类场景均有测试与一次真实耗时对比。
指针：backend/api/jvm.py，backend/jobs/runner.py，backend/jobs/jvm_exec.py，core/jvm_env.py，core/jvm_daemon.py，core/jvm_validate_daemon.py，lessons §六十五 / §六十六

### 条目：jvm-coalesce-measurement · 校验重复执行收益评估
状态：todo
依赖：无
优先级：P2
背景：任务级完全重复请求与同站同指纹源级共享探测都可能减少 JVM 执行，但目前没有真实重复率和可减少请求数的统计，先统一完成收益评估，避免两条计划各做一套重复统计。
约束：分别统计完全相同请求与同站同指纹组；键必须包含归一化 URL、规则/源快照、阶段、验证深度、搜索词及运行参数；统计只读，不改变现有执行与结论；没有数据证明收益前不实现复用。
验收：给出两类重复的数量、占比、可减少的 JVM/站点请求数和样本口径；据此分别决定是否实施 `jvm-request-coalesce` 与 `batch-fingerprint-coalesce`。
指针：backend/api/jvm.py，core/dups.py，backend/jobs/runner.py，lessons §五十三 / §七十八
### 条目：proj-3-drop · 前端摘掉本地投影
状态：todo
依赖：proj-3-bookurl, proj-3-attr
优先级：P1
背景：`replayResult` / `canReplay` / `doReplay` / `/replay-step` 那条链是
  「引擎没覆盖的段」的退路；摘之前先定详情段（proj-3-bookurl）与末段属性名
  （proj-3-attr）两处，否则那两段的「命中源码」会空掉。
约束：等那两条拍板落地才动手；「每种空值有可执行的一句话」前置已落地。
  摘的位置与依据在 `frontend/src/components/DebugWorkbench.vue` 的
  `matchedFrom` / `matchedHint` 旁。
验收：摘掉后抽屉里每个段的「命中源码」仍能取到值，或明确显示「本段取不到」。
指针：lessons §七十三 / §七十五，frontend/src/components/DebugWorkbench.vue

## 2 · 按需

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
指针：lessons §七十四，appservice/test/io/legado/app/service/ValidateService.kt

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

### 条目：ux-chip-jump · 点击摘要里的变化数跳到对应筛选
状态：todo
依赖：无
优先级：P2
背景：摘要里的变化数点了没反应。
约束：要与统计条 chip 的筛选状态协同，不能各管一套筛选；动手时顺手把筛选条
  拆成独立组件，别再往 SourcesView 续写。
验收：点变化数后列表筛到对应子集，且 chip 状态同步。
指针：frontend/src/views/SourcesView.vue

### 条目：job-detail-poll-consolidate · 合并任务详情与时间线轮询
状态：todo
依赖：无
优先级：P2
背景：任务详情弹窗同时轮询任务状态、详情和执行时间线；这不改变 JVM 实际启动时间，但会增加弹窗打开时的请求、SQLite 读取和前端刷新竞争。
约束：不能复制任务状态判据；终态后必须停止所有轮询；时间线增量游标和结果详情的静态字段要保持现有语义；接口失败仍需保留具体原因。
验收：进行中任务打开详情时，状态/详情/时间线不再为同一任务重复建立独立轮询；进度、时间线增量、终态结果和取消行为与现有测试一致。
指针：frontend/src/components/TaskDetailDialog.vue，frontend/src/components/JobTimeline.vue，backend/api/jobs.py，backend/api/job_timeline.py

### 条目：timeline-scan-incremental · 时间线失败源改为增量消费
状态：todo
依赖：无
优先级：P2
背景：时间线轮询每次都重新扫描各块 `results.jsonl` 统计失败源；批量结果越大，弹窗轮询成本越高。失败源当前来自结果快照，不应伪装成逐源实时引擎事件。
约束：执行线程追加的骨架事件仍按游标消费；失败源计数必须精确，截断清单不能改变总数；终态仍以 `result_json` 为唯一事实；不能把文件尚未写完整的行判成源失败。
验收：运行中不再每轮从头扫描全部结果文件；大批量时间线轮询的读取量随新增数据增长而非随全量结果重复增长；失败总数、截断提示和终态清单与现有口径一致。
指针：backend/api/job_timeline.py，backend/jobs/jvm_exec.py，tests/test_job_timeline.py，lessons §二 / §二十八
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

### 条目：pair-probe · 同源裁定：任意两条源的成对比对
状态：todo
依赖：source-edges
优先级：P2
背景：整理源的五档（rules / mergeable / host / name / mirror）都是单键聚类的
  线索，「这两条是不是同一个源」没有判定工具——同名不同域（换址转发）尤其
  没人管。分工：候选靠人眼与现有分档，裁定靠引擎——同一个搜索词两边各跑一次，
  diff 返回书目，结论落库可追溯，不是弹窗即焚。
约束：列表页多选两条发起；两侧差异与比对结论要落库（复盘要能翻）；「同一个源」
  的结论只能人点确认后写进 source-edges，机器不下结论（AGENTS #3 / #11）；
  name / host / mirror 三档保持只读线索（可用性展示归 dup-bc），不为裁定再造
  相似度分档。
验收：任选两条源能发起比对、看到两侧结果差异与证据出处；确认后写入对应边；
  比对记录重启后可查。
指针：core/checker.py，core/dups.py，frontend/src/components/TidyDrawer.vue，lessons §三十四

### 条目：import-preview-coalesce · 导入预演标出同站同指纹源
状态：todo
依赖：无
优先级：P2
背景：导入只拦同 URL；同站且行为指纹全等、URL 只差署名或端口写法的转发源仍会照单全收，之后才靠整理抽屉清理。
约束：只提示同站 + 指纹全等的候选并默认建议归并；跨站同名照旧走人工；预演不静默合并或删除用户数据。
验收：导入含署名转发源的批次，预演标出候选归并对；确认后不产生重复在用行，未确认时原导入行为不变。
指针：backend/api/imports.py，core/dups.py，lessons §三十四

### 条目：batch-fingerprint-coalesce · 批量校验按同站同指纹复用探测
状态：blocked
依赖：jvm-coalesce-measurement
优先级：P2
背景：同站且行为指纹全等的转发源可共享一次探测，但现有批量按源逐条执行；共享结论必须能追溯来源，不能把一次失败静默扩散到整组。
约束：先量同站同指纹组在实际批量中的占比，证明收益后再实现；仅组内指纹全等时复用，跨站同名不合并；每条结论标明共享来源，不复用旧批次结果；实现时开关纳入 settings_store，默认值与限幅只定义一处；与 `jvm-request-coalesce` 的完全重复请求统计分开评估。
阻塞于：先统计真实批量中同站同指纹重复源的占比；没有数据证明收益前不实现。
验收：评估报告给出重复组规模与可减少的请求数；若实施，同组只探测一次、每行结论均带来源，指纹或站点不同则独立执行，显式关闭开关时不共享探测。
指针：backend/api/jvm.py，core/dups.py，AGENTS.md #5b，lessons §五十三

### 条目：syntax-gap · 本地回放的语法缺口
状态：open
依赖：无
优先级：P2
背景：本地回放**已不在校验与调试链路上**，剩下的两个读者都不判定源：`preselect` 的免费初筛
  （`core/repair/suggest.py`）与前端 `/rules/replay-step` 的投影退路。所以下面这张缺口表
  是「本地验不了」的账，不是待办。
约束：只有当某个缺口挡了初筛或投影时才急（初筛遇到不支持的语法时如实标「只能连 App 试」
  即可，不因此判源坏）；别为了「让本地能验」把 webView 去掉——去掉就真的读不了。
子项：
- syntax-bang · 排除索引 `li!0` / `dd!0:1:2`：`findIndexSet` 里 `.` 与 `!` 语义相反，只实现了点式（最大一块）
- syntax-fallback · 执行期兜底的「选择器解析不了」：样例（URL 模板、JSONPath 片段、碎片）先逐个归类再实现
- syntax-jsonpath · JSONPath 超出子集：`[*]` / `['键']` 已支持，`[1:3]` 切片没有，扩展时已有写法行为不变
- syntax-xpath · `//` 开头的 XPath：引引擎或恒 unknown，别静默当 CSS（AGENTS #4），决议归 xa-1111
- syntax-bracket · 方括号索引 `[-1]` / `[0]` / `[1,3]` 与区间 `[0:10]`：未实现，与点式保持同一套归一
- syntax-shorthand · `text.` / `children.` 简写：语义已查清，实现即可，判定半边一律 unknown 不动
验收：每行语义能在本地回放跑通或有明确归因，带正反例测试。
指针：lessons §二十三 / §四十四 / §四十九 / §六十，core/rules/replayer.py

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


### 条目：ux-debug-reading · 调试高级信息与响应式阅读体验
状态：doing
依赖：无
优先级：P1
背景：运行态、事件流、整页源码、语法速查和详细 AI 信息对熟悉用户有用，但不应挤走首次调试所需的结论和动作。
约束：默认层只展示结论、原因、主动作、核心值和证据来源；高级材料可展开；**「哪一个是 gap、谁是主动作」的口径见 AGENTS #24，本条只管呈现**；运行态继续消费 `useDebugSession`，固定显示等待、预算和取消状态；移动端保持当前步骤、结论、核心值和主动作在首屏；不复制运行态或结果状态。
验收：滚动到证据区仍能找到运行态；取消等待与后端任务状态文案明确；默认层能看到当前步骤的核心值（无权威值时明确说明未返回）；展开高级信息后原有材料仍可用；窄屏下步骤、结论、核心值、来源和主动作可触摸访问且无横向页面溢出。
指针：frontend/src/components/DebugWorkbench.vue，frontend/src/views/DebugWorkbenchView.vue，frontend/src/composables/useDebugSession.js


### 条目：agent-layer-orchestration · 按 Layer 编排受限调试 Agent
状态：open
依赖：无
优先级：P1
背景：当前项目已经有 `core/page_layer.py` 的 L1-L4 判定、`debugNextAction.js` 的下一步动作、`/rules/suggest-rule` 的 AI 提议，以及 JVM/App 两条真实引擎通道；缺的是把它们按 Layer 串成一个小上下文、有限动作、逐步验证的 Agent。第一目标是因地制宜支持口袋漫画的 L3 动态正文，不建设通用逆向平台。
约束：Agent 只输出结构化动作和受控提议，不直接改源、不自行联网、不判定成功、不猜密钥；L1 优先走本地候选，L2/L3 优先转 App/JVM 实测，L4 只消费已观测接口摘要，L5 只提示登录上下文；所有提议必须经过 `replay-step`、`jvm-debug` 或 `app-debug` 验证；上下文只传本地压缩摘要，完整 HTML/脚本/事件流按需取证；AI 调用必须由用户显式触发，免费 dry-run 不发模型请求；动作与提议的结构按 AGENTS #24 的五格合同来，不另设计一套。
验收：对 L1 静态页、L2 空容器、L3 口袋漫画正文、L4 接口页、L5 登录提示各有一条结构化动作链；Agent 输出不能绕过真实引擎；口袋漫画能从选定章节得到 `webView + webJs + content` 草稿并通过图片数量与可访问性验证；未知、缺证据和验证失败均保留具体原因。
子项：
- agent-context
- agent-action-schema
- agent-layer-router
- agent-pocket-comic
- agent-workbench
- agent-evidence-budget
指针：core/page_layer.py，frontend/src/utils/debugNextAction.js，frontend/src/components/DebugWorkbench.vue，backend/api/rules.py，lessons §七十三 / §八十

### 条目：agent-pocket-comic · 口袋漫画 L3 WebView 正文专项策略
状态：todo
依赖：agent-layer-router, agent-action-schema
优先级：P1
背景：口袋漫画正文页的图片地址在 WebView 执行后的 `params.chapter_images`，静态 HTML 没有图片，运行时图片还可能变成 `blob:` URL；图片签名会过期，目录选错还会导致重复章节。第一版应验证运行时数据，不应让用户配置 AES 或复制旧图片地址。
约束：入口要求用户选择具体章节；Agent 只生成 `requires_webview / runtime_field / content_mode` 策略；本机/App 实测确认 `params` 已为对象、`chapter_images` 非空且图片可访问后，才生成 ES5 `webJs + content + imageStyle`；调试工作台明确标出 L2/L3，静态网页视图不能框选时给出**取证**动作（「用本机引擎 / 连 App 取运行时材料」），不许写成解决方案（口径见 AGENTS #24）；运行时 DOM、命中片段和框选高亮必须来自同一份运行时材料，不能把静态补抓冒充 App 页面；每次调试重新获取图片地址；跑批不能可靠覆盖这类源时必须明确标注；章节与图片失败原因不得压成“解密失败”。
验收：选定一章能显示页面、WebView、运行时字段、图片数量和可访问性；成功时生成源草稿正文规则并由 App 实测复验；params 仍为字符串、图片为空、签名过期、目录地址不具体、静态页面无运行时材料时分别给出对应下一步；若取得运行时 DOM，命中源码与框选高亮使用同一份材料；跑批对这类源要么同样能验，要么结论明确标「这类源跑批不可信」；不在源中硬编码站点 AES 或图片 URL。
指针：skills/legado-book-source/SKILL.md，core/js_hints.py，tests/test_page_layer.py，data/app_probe/source.json，lessons §四十九 / §六十 / §七十五

### 条目：agent-evidence-budget · 限制 Agent 取证范围与调用次数
状态：todo
依赖：agent-context, agent-action-schema
优先级：P2
背景：Agent 的价值是压缩调试循环，而不是扩大模型上下文；需要先固定按需取证工具和每轮预算，防止模型反复索取整页材料。
约束：只允许读取证据行附近片段、运行时键摘要、字段类型/长度、网络响应形状和指定脚本命中片段；禁止读取完整 Cookie、密钥和无界 bundle；每轮最多一次模型调用和有限次取证，超过预算转为用户动作；预算与错误原因写入结果，不静默截断。
验收：完整 HTML/脚本不会默认进入模型请求；超出预算时界面显示具体原因和手动入口；同一失败不会自动循环调用；脱敏与截断有单测覆盖。
指针：backend/api/rules.py，core/js_hints.py，frontend/src/components/DebugWorkbench.vue

---

## 3 · 待决策

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

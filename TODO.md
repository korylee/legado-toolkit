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

---

## 1 · 排队

### 条目：jvm-scheduler-policy · 调试优先但不能饿死批量校验
状态：todo
依赖：无
优先级：P1
背景：当前 `jvm` lane 已把调试、批量校验和生成后验证收进同一条进程内有序队列，能够避免并发改写 JVM 参数与输出；但严格 FIFO 会让交互调试被长批次挡住，简单地让调试永远插队又会让批量任务长期不运行。
约束：调试请求优先；批量任务按有限工作单元让出执行权，并设置老化/公平规则，不能依赖 HTTP 连接是否仍存活。优先级只能影响排队顺序，不能绕过同一 JVM/profile 的独占约束。生命周期状态固定为 `queued`、`running`、`cancel_requested`、`succeeded`、`failed`、`cancelled`；执行阶段至少区分 `waiting_readiness`、`starting_worker`、`starting_gradle`、`configuring`、`compiling`、`running_validate`、`reading_results`、`saving_results` 和 `finished`。状态、阶段、取消和原因继续写入 jobs/SSE，不能只存在内存。
验收：同时提交一个长批量任务、多个调试任务和第二个批量任务；调试任务在当前工作单元结束后优先获得执行权，第二个批量任务最终也能运行。前端能看到当前阶段、实际执行方式和最近一段启动日志；服务端重启或关闭页面不改变已提交任务的生命周期语义，取消不会在子进程退出前伪装成已完成。
指针：backend/jobs/runner.py，core/store.py，lessons §六十八 / §七十四

### 条目：jvm-batch-chunk · 批量校验按可恢复分块执行
状态：todo
依赖：jvm-scheduler-policy, jvm-task-manifest
优先级：P1
背景：批量请求目前以一个 JVM job 持有 lane；即使结果已逐条落盘，长批次仍会长时间占住交互调试，取消或进程退出后也缺少明确的分块边界。该条同时承载批量执行的阶段状态、日志尾部和运行目录隔离，不另建“Gradle 进度”条目。
约束：分块单位必须是带归一化 URL、参数快照和来源的独立记录，并引用任务 manifest 中冻结的 runtime snapshot；每块完成即落盘并可从已有结果跳过，不能把整批结果重新拼成唯一事实。每个 job/chunk 使用 manifest 规定的独立 args、结果文件和运行目录；块大小、重试和并发上限由设置/测量决定，不在调用点散落常量；块之间释放调度权。单条失败只影响该条，环境错误不得归因给源。
验收：一个批次被拆成多个块；中途取消或模拟进程退出后重启，已完成块不重复请求、未完成块可继续。每块能报告 queued、starting、running_validate、reading_results、saving_results 和失败原因；启动失败保留日志尾部；报告能区分批次、块、URL、stage、来源和失败原因。
指针：backend/api/jvm.py，core/jvm_debug.py，core/store.py，lessons §五十三 / §五十四

### 条目：jvm-request-coalesce · 合并重复的进行中请求
状态：blocked
依赖：jvm-batch-chunk
优先级：P2
背景：同一源、同一规则快照和同一 JVM 参数可能由调试抽屉、生成后验证和批量入口重复提交；单纯排队只能延后重复工作，不能减少请求和站点压力。但它不是当前“重启后单条校验慢”的根因，必须等 worker 和分块边界稳定、且有重复提交数据后再决定是否实现。
约束：合并键必须包含归一化 URL、规则/源快照、阶段、验证深度、搜索词及本次运行参数；不能只按 URL 合并。只合并仍在运行或可复用的同口径任务，每个调用方仍有自己的 job 观察关系；取消一个观察者不能取消共享执行，除非没有观察者且明确执行取消。
验收：只有在日志证明重复提交达到值得优化的数量后才实施；实施时完全相同的重复提交只产生一次 JVM 执行和一份底层结果，任一调用方都能收到同一结论及来源，任一合并键字段变化都会产生独立执行，旧结果不会静默复用。
阻塞于：等待 `jvm-task-manifest`、worker/分块路径稳定，并先统计重复提交率；没有数据证明收益前不实现。
指针：backend/jobs/runner.py，backend/api/jvm.py，core/jvm_debug.py，AGENTS.md #5b，lessons §五十三 / §七十八

### 条目：jvm-worker-lease · 给跨进程 worker 增加 SQLite 租约
状态：todo
依赖：jvm-batch-chunk
优先级：P1
背景：当前 lane、`RUN_LOCK` 和执行中的 asyncio task 都是进程内状态；直接增加 API/uvicorn worker 会让不同进程各自认为自己拿到了 JVM，现有 jobs 表也没有 worker owner/generation，无法安全认领和恢复任务。它解决的是跨进程一致性，不与调度策略合并。
约束：以 SQLite 原子认领为跨进程事实来源，至少记录 owner、generation、heartbeat、attempt 和运行目录；同一 job/块只能有一个有效 owner。认领、续租、完成和失败必须校验 owner/generation，旧 owner 不能覆盖新结果。进程启动、优雅停止和异常退出都要有明确回收/重试规则，不能用一次固定超时把源判坏；运行 manifest 与结果文件必须按 job/块隔离。
验收：启动两个 worker 并发抢同一 job/块时只有一个成功；杀掉 owner 后任务能按规则恢复且不覆盖已完成结果；旧 owner 延迟回写会被拒绝；重启后 jobs、租约、运行目录和结果状态能逐项对账。
指针：core/store.py，backend/jobs/runner.py，core/jvm_debug.py，lessons §二十八 / §六十五 / §七十四

### 条目：jvm-task-manifest · 固定每次任务的输入、环境、产物和执行方式
状态：doing
依赖：jvm-runtime-snapshot
优先级：P1
背景：核心已交付（2026-09-26）：单条/批量与调试的运行目录在执行开始落盘 `manifest.json`
  （信封：job_id/owner_pid/chunk/generation/created_at + 提交冻结的 inputs，含归一化 urls、
  生效参数、runtime fingerprint、各文件路径，schema 2 带 sha256 防篡改）、`runtime-snapshot.json`
  副本与 Gradle 全量 `stdout.log`/`stderr.log`；保留策略为成功/取消清理、失败/崩溃保留现场
  （上限 20 修剪）。剩余三件：① refresh 入口的 manifest（现有 candidate+对拍报告只算半个）；
  ② attempt 语义——`runner.submit` 已有 `retry_of`，重试要生成新运行目录而不是覆盖旧产物；
  ③ chunk/owner/generation 目前是占位值，随 `jvm-batch-chunk` / `jvm-worker-lease` 实义化。
约束：SQLite 仍是任务管理事实源，manifest.json 是崩溃后可逐项对出的复查交付物；提交后不得
  静默切换 runtime、参数文件或运行目录；失败现场按 `FAILED_RUN_DIR_CAP` 修剪，不无限堆积。
验收：重启或异常退出后可据 manifest 判断已完成、未完成和失败阶段；同一时间运行的任务不共享
  args、profile、daemon 信息、结果或日志文件；日志和界面能从 job/chunk 追溯到实际执行方式及
  失败原因；重试生成新 attempt 且旧产物不被覆盖。
指针：backend/api/jvm.py，backend/jobs/runner.py，core/jvm_debug.py，core/store.py，lessons §六十五 / §六十八

### 条目：jvm-env-readiness · JVM 环境收尾与跨平台启动器
状态：todo
依赖：jvm-runtime-snapshot, jvm-task-manifest
优先级：P1
背景：基础路径发现、Android SDK 平台校验和 readiness 接口已经具备，但当前“准备态”与“已有 snapshot 的单条执行态”仍混在一条检查里；此外，Gradle `.bat` 启动器的跨平台边界和静态检查是否覆盖真实启动条件还需要收尾。本条是后置环境治理，不应阻塞有效 snapshot 的单条快速路径。
约束：①准备态明确显示 App 仓库声明的 `compileSdk`、实际 SDK 根目录和缺失平台，`platform-tools` 目录不能被当作完整 SDK 根目录；②单条执行态只检查 snapshot 引用文件、Java 启动器和源码/runtime 签名，不重复要求 Gradle Wrapper 或 SDK 平台检查；③按 App 仓库 wrapper、Gradle daemon criteria 和 toolchain 配置分别推导启动 Gradle 与项目编译所需的 JDK，不用单一最低版本常量替代；④遵循 Gradle 默认值或明确配置解析 Gradle User Home，检查目录可创建、可写和跨卷场景，不恢复盘符根目录回退；⑤明确支持的操作系统，非 Windows 要么使用对应启动器，要么在启动前明确拒绝；⑥readiness、准备命令和实际启动共用同一份已解析 runtime snapshot；⑦自检不下载依赖、不隐式构建，下载和预热继续由独立准备命令负责。
验收：在缺少 `android-37`、SDK 根目录仅含 `platform-tools`、非标准 JDK、不同 Gradle User Home、跨卷仓库和非 Windows 环境下，准备态与执行态能分别说明采用或拒绝原因；准备态能完成一次 snapshot 刷新，执行态能复用有效 snapshot 完成单条校验；支持范围外系统在启动前明确拒绝；本条未完成时不阻塞任务 manifest 和已有单条校验路径。
指针：core/jvm_env.py，core/jvm_direct.py，core/jvm_daemon.py，backend/api/jvm.py，scripts/prepare_gradle.py，core/jvm_runtime_snapshot.py，lessons §六十五

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

### 条目：jvm-worker-roadmap · JVM 调试与校验的推荐拆分路线
状态：open
依赖：无
优先级：P1
背景：当前单后端 + 单 JVM lane 已解决同一进程内的参数、profile 和输出互相覆盖问题；直接增加多个 API/uvicorn 服务会复制进程内锁与调度状态，不能自然获得安全并发。更合适的边界是保留一个 API 入口，先把 runtime snapshot 和任务 manifest 固定下来，再修复单条校验冷启动，随后优化批量粒度，最后拆两个职责单一的 JVM worker。
约束：按以下顺序推进：①完成 `jvm-runtime-snapshot`，把一次性准备产物与 actual snapshot 对拍分开；②`jvm-task-manifest` 固定每次任务的输入、runtime、阶段、执行方式和产物；③`jvm-single-fast-path` 让已有有效 snapshot 的单条校验优先进入 Validate worker/daemon，完整 SDK/Gradle 只用于首次准备、刷新和 fallback；④将 `jvm-env-readiness` 后置为准备态/执行态两层检查及跨平台启动器收尾；⑤完成调度公平和批量分块，阶段状态与运行文件隔离；⑥以 SQLite job/块租约取代跨进程依赖内存锁；⑦建立交互调试 worker 和批量校验 worker，各自独占 JVM daemon、浏览器 profile、参数文件、运行根目录和 worker 身份；⑧只有实测证明重复执行或多 worker 有收益时，才做请求合并或多开 JVM。任何阶段都不直接横向复制 API 服务，也不共享 `args.properties`、daemon info、cookie/profile 或固定输出文件。
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

### 条目：jvm-validate-worker · 批量校验专用常驻 worker
状态：todo
依赖：jvm-worker-lease, jvm-batch-chunk
优先级：P1
背景：批量校验需要独立于交互调试的常驻 worker，省掉频繁小批次的 Gradle + JVM 拉起；它与交互调试 worker 分开，避免长批量占住交互请求，也避免两个用途共享 profile、cookie、args 或输出。本条承接原 `s5a-d3`，不再单独维护一条重复的“跑批常驻”路线。
约束：**「频繁跑全量会被封 IP」是硬约束**，比任何提速优化都优先（lessons §六十八）。批量 worker 必须拥有独立 JVM、浏览器 profile、参数文件、运行根目录和优雅停止/重启流程；内部并发只能在分块与资源测量后设置，不能把一个常驻 JVM 当成无限并发池。结果必须保留与一次性运行同样的 stage、来源、原因和逐条可恢复性；未证明恢复、隔离和逐字段一致前，不切默认路径。
验收：在同一批次、同一参数和同一快照下，对比一次性运行与常驻 worker 的逐字段结果；中途杀掉 worker 后按租约恢复且不重复已完成块；并验证调试 worker 能在批量 worker 工作时独立接收请求，两个 worker 不读写对方的 profile、args、运行目录或结果文件。
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
状态：blocked
依赖：jvm-worker-lease, jvm-batch-chunk
优先级：P2
背景：worker 拆分后是否增加批量 worker 数量，取决于 JVM 启动成本、RSS、站点限流、失败率和调试延迟；不能从 CPU 核数直接推导。这里评估的是专用 worker 的容量，不是直接增加 API/uvicorn 进程；在没有真实容量数据前不做。
约束：先完成 `jvm-worker-lease`、分块和独立运行目录；按同一快照分片，分别测单 worker 与多 worker 的吞吐、P50/P95 延迟、RSS、错误率和站点请求量。未完成测量前不增加实例；若收益不覆盖内存/限流代价，保持一个批量 worker，允许本条最终关闭而不实施多开。
验收：给出可复核的单 worker/多 worker 对比和容量结论；只有在结果支持时才调整 worker 数量，并确认每个实例的参数、profile、JVM 和运行目录完全独立。
指针：core/jvm_debug.py，core/store.py，lessons §四十七 / §六十五 / §六十六
阻塞于：等待 `jvm-worker-lease`、`jvm-batch-chunk` 完成并取得单 worker 基线；若容量收益不足，直接关闭本条，不实施多开。

### 条目：jvm-debug-worker · 交互调试专用常驻 worker
状态：todo
依赖：jvm-worker-lease, jvm-scheduler-policy, jvm-batch-chunk
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

### 条目：jvm-single-fast-path · 单条校验脱离 Gradle 冷启动
状态：done
依赖：jvm-runtime-snapshot, jvm-task-manifest
优先级：P0
背景：2026-09-26 实测验收通过：准备态能报出 compileSdk=android-37 与 SDK 根；单条校验 daemon 冷启动 16.1s、复用常驻 2.5s；同源 fallback 逐字段对账零差异；运行目录无残留。
约束：执行态检查只钉 classpath 负载文件（jar/zip），目录缺失交由 daemon 运行期失败回退；接线测试须把 dump_path 隔离出真机状态，否则真实 snapshot 一存在就整批变红。
验收：细节与提交看 `git log`（fb472ee / 0572eb8 / 406941c / 9b1c0b0，更早的 groundwork 8d152a0 / 2d3fd68 / 8074ada / a99835e）。
指针：backend/api/jvm.py，core/jvm_direct.py，core/jvm_validate_daemon.py，lessons §六十五 / §六十八

### 条目：jvm-runtime-snapshot · 让 JVM 实际运行环境与 dump 对拍
状态：done
依赖：无
优先级：P1
背景：2026-09-26 完成真实对拍：refresh / gradle / validate_daemon / direct 四份 snapshot 齐全（按 mode+entry 后缀各留一份，互不覆盖），全部差异逐条归因为三类口径性差异 + 一处直起静默继承（已修），见 lessons §九十。
约束：actual 差异报告只进排障，不进入源健康判定链；对拍口径（workingDir 归一、classpath 按项、-Xmx 纳入后比 jvmArgs、systemProperties 逐键）已按原约束落地。
验收：修复与边界测试看 `git log`（406941c classpath 只钉负载文件；同日 run_direct 注入本次 args 路径，修复直起 0 事件）。
指针：core/jvm_direct.py，core/jvm_runtime_snapshot.py，appservice/test/io/legado/app/service/ServiceJson.kt，lessons §九十

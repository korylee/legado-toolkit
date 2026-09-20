# 待办与二期候选

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）。可复用的机制与踩坑
> 写进 `skills/legado-source-lessons`，这里只留一行指针；**已完成的事项从这里删掉**
> ——细节看 `git log`，机制看 lessons（AGENTS #10）。
>
> **已收口**（2026-09-19）：S1 JVM 搜索档全量 / S2 接进产品 / S3 三段 + 浏览器桥 /
> S4 健康六档 + 存量迁移 / 本地回放边界与 `bookUrl` 作用域 / 界面文案批 1。
> 机制与实测见 lessons §四十八～§五十六，文案那批见 §四十六。

---

## 路线图（剩下的）

| 阶段 | 做什么 | 依赖 | 完成判据 |
|---|---|---|---|
| **S5-A JVM 调试通道** | 接替「连 App 调试」，含 cookie 注入——方案见 §一点八。**含第二期常驻 daemon**（先量再定） | S1–S3 ✅ | §一点八 的四批各自判据 |
| **调试体验（§九）** | 先定层再写规则：定层横幅 + 候选按层改写（容器与字段成对、补 id）+ AI 提议加前置条件 | **S5-A 走通**（只为「通道选择」一次做到位） | §九 四批各自判据 |
| **S5-B 真机复检** | **只做只读那条**（`/searchBook` + 读回 App 自带结论），一行 App 数据都不写 | —— | §二 |
| **文案收口** | 机检基线还剩 **147 条**：批 2（前端）、批 3（后端话术 + 断言同步） | —— | §八 |
| **本地回放语法缺口** | 按需，**先确认还在不在交互路径上** | —— | §三 |

> ⚠️ **CACHE_VERSION 合并 bump**：凡翻转判定结论的改动，**做完一批再 bump 一次**、
> 付一次全量重跑，别每步付一次。当前 **14**（13 = 健康档位词表，14 = `bookUrl` 作用域）。

---

## 一、JVM 校验服务（已落地；机制与环境坑在 lessons §四十八～§五十四）

**来源阶梯与产品定位（已定，不再展开论证）**：

- 每源结论带来源阶梯：**本地回放 < JVM(App 引擎) < 真机(App + 真实环境)**；
  JVM 结论存 meta（`jvm_check:<batch>:<url>`），**不写 checks**——与本地回放是
  两条证据，混在一个字段里就分不出谁说的。
- 三条通道各管一段：**校验/调试归 JVM**（调试那条它能交回 HTML，设备 WS 给不了）、
  **真机只做复检**（登录墙 / WebView 依赖 / 用户网络出口，见 §二）。
  「调试归 JVM」的**动手方案已定**：见 §一点八（S5-A），做之前「连 App 调试」
  仍是唯一可用入口。
- **换机器 / 新用户上手**：那几步（App 仓库 + JDK 21 + SDK + 同盘 Gradle 缓存、
  设置 → JVM 校验页签填 App 源码目录、先自检再跑批）已移进 `README.md` 的
  「JVM 校验（可选）」一节——它属于「使用者需要知道」，不留在待办里。

**S3 之后悬着的两个设计**（都不阻塞，做真机通道或结论互通时再回来）：

- **「没结果」二义性**：JVM 搜索对单关键词跑，某源没结果可能是「没这本书」——
  缓解靠多词复核（全不命中才判坏），落地点在做 `no_result` 复核批次时。
- **JVM 结论要不要反向喂给本地口径**（health/stars 的映射与来源标注）：
  那是「结论互通」的事，等三段结论稳定后再议，
  **动本地口径才涉及 CACHE_VERSION**。

## 一点八、S5-A：JVM 调试通道（2026-09-19 定方案；接替「连 App 调试」）

**为什么做**：单源调试目前唯一的入口是连 App（`SourceEditDialog.appDebugRun`：
填 IP → 预检 → 推送 → WS 1123）。而设备 WS 有一个结构性天花板——**`matched_html`
恒为空串**（`core/app_debug.py` 的 `build_steps`，App 只推文本事件不给 HTML）。
JVM 通道跑的是同一段 App 代码（`Debug.kt` 管线），手里还握着完整 HTML，建成后
信息量**超过**设备调试；同时消掉「填 IP / 预检 / 推送」整条前置链。登录/代理源的
可调试性已查证（lessons §五十六）：代理跟着源走完全支持；登录态三层里只有
「登录过程本身」要回设备/浏览器做一次，cookie 注入参数把第二层也自动化。

**两个上游接口已核过签名**（App 仓库当前版本）：

- **事件流**：`Debug.startDebug(scope, bookSource, key): Session`（`Debug.kt`），
  `Session.events` 是公开 Flow（Channel 承载，Error/Completed 自动关流）。
  key 语法照抄 App：纯关键词=搜索、绝对 URL=详情、`::`=发现、`++`=目录、`--`=正文
  ——前端本来就懂这套。**不需要 WS、不需要设备**，JVM 里直接 collect。
- **webView 依赖面**：`BackstageWebView.getStrResponse()` 干**两步**——
  ① WebView 加载页面（loadUrl / loadDataWithBaseURL），② `EvalJsRunnable` 在渲染
  结果上执行 `js` 选项（无则默认 `document.documentElement.outerHTML`），
  **非空才收**，1 秒重试至 30 次超时。调用点三处：`AnalyzeUrl.kt`（取数）、
  `AnalyzeRule.kt`、`JsExtensions.kt`（规则里的 `java.` 调用）。

**ShadowBackstageWebView 是整个方案的关键设计**——比「渲染后喂解析器」多一步：

- S3-4 校验通道可以「CDP 渲染 → 喂 `BookList.analyzeBookList`」，因为校验只要终态；
  **调试通道不行**：`Debug` 管线（WebBook.searchBook → infoDebug → …）是不透明的，
  重实现它等于把 App 调试语义抄一遍。
- 正确接法：Robolectric shadow 掉 `BackstageWebView`，`getStrResponse()` 委托给
  `BrowserBridge.renderSerial`（渲染）+ `Runtime.evaluate(js选项)`（求值），
  组装 `StrResponse` 返回。Debug 管线**一行不改**，webView 源在整条调试链路里透明走 CDP。
- shadow 语义要对齐 App 的重试纪律：**JS 求值结果为空时重试**，
  不是「渲染一次拿 outerHTML 交差」——这是与 S3-4 校验桥的本质差异，别复用错了。
- 与 S3-4 共享一个 Session（renderSerial 已串行）；`js` 选项求值可能改 DOM
  （isRule 注入），每次求值用独立 Runtime.evaluate，不缓存。

**分四批，每批独立可验、独立提交**：

| 批 | 做什么 | 验收判据 |
|---|---|---|
| **S5-A1 事件流贯通** | `DebugService.kt`（appservice 侧）：args.properties 读 `file/key/out/timeout`，复用 `ensureStarted()` Koin 桩；`Debug.startDebug` + collect events → 每事件一行 NDJSON。先不做 webView/cookie | 普通源 5 条：NDJSON 事件流与设备 WS 推的同构（`┌/└/◇`、段名齐全），`app_debug.py` 的 `_split_segments` **不改一行**能解析；**零事件必须显式报错退出非 0**，不许产出空文件当成功 |
| **S5-A2 shadow 桥** | **先做半天 spike**（空壳名单 1 条源：shadow 只挂上、验证 Robolectric 下真能 collect 到事件流），走通了再铺开。`ShadowBackstageWebView`（@Implements，只挂测试编译）；getStrResponse 委托 BrowserBridge（渲染 + js 求值 + 空结果重试）；复验 S3-4 的空壳源名单 | 空壳源名单里至少 1 条：调试事件流走通（App 自己管线的日志里出现解析完成），而非「渲染后另喂解析器」 |
| **S5-A3 cookie 注入** | args 加 `cookie=`；走 `CookieStore.setCookie(url, cookie)` 预填 CacheManager（实现时验证 `enabledCookieJar` 开关与拦截器链路这一环）；管理库存档的 cookie 可直接用 | 带 `loginHeader`/已知 cookie 的源各 1 条：搜索段带登录态跑通 |
| **S5-A4 接进产品** | `core/jvm_debug.py` + `POST /api/rules/jvm-debug`（subprocess → NDJSON → 复用 `app_debug.py` 分段解析；`matched_html` 本批留空）；前端 `SourceEditDialog` 加通道切换，**默认 JVM**，连 App 降为备选（预检/推送逻辑保留不删） | 界面上选「本机引擎」：不填 IP、不推送，直接看到分段调试结果 |

**第三期（S5-A 之后，独立可做）：`matched_html` 回填**——在 shadow / OkHttp 拦截器里
记 (url, body) 环形缓冲，按每段事件里的 `≡获取成功:<URL>` 回填。做完这条，
JVM 调试对「看规则命中了什么」就**全面超过**设备 WS。

**第二期：常驻 daemon（把 10–20 秒的入场费消掉）**

A1–A4 形态是**每次调试起一次 JVM**，那 10–20 秒几乎全是一次性入场费：
JVM 启动 + Robolectric bootstrap（android-all jar、应用环境、两类静态初始化，
lessons §四十八点五）+ App 的 Koin 桩。**边际成本其实很小**——S1 全量是
「3774 条 / 17 分钟」≈ **0.27 秒/源**，那是同一个 JVM 跑完全程摊下来的。所以
常驻的收益就是把这条曲线搬到交互场景：从「每次 10–20 秒」变成「一次入场 + 每次亚秒级」。

**但先量再定，不要凭估**（lessons §四十七）。量法：同一源跑三次，
分别记 ① 完整 `legado-gradle.bat` 调用 ② 已编译后直接 `java -cp` 起 JVM
③ 常驻后单次请求——看 ①−② 是 Gradle 配置阶段的占比、②−③ 才是 Robolectric+App 初始化的占比。
**若 ③ 之后单次耗时的大头是页面渲染/请求（本会话正文段 18 秒就是这么来的），
那 daemon 也消不掉它** —— 那种情况下先做 §九（少跑几轮、跑得更准）比建常驻更值。

形态照旧：`DebugServiceDaemon`（Robolectric 进程驻留 + localhost socket 收请求），
后端持长连接、前端无感。**动手时的额外约束**（比 A1–A4 多了四条，别漏）：

| 约束 | 为什么 |
|---|---|
| **串行队列** | `Debug.log` 只在 `debugSource == sourceUrl` 时 emit（`Debug.kt`）——同一进程同时跑两个调试会互相污染事件流 |
| **请求间清状态** | cookie（`enabledCookieJar`）、`CacheManager`、`Debug` 静态态、Rhino 的 `shareScope`（按源缓存）都会跨请求残留。**要么每请求换 scope，要么每次跑完显式清**，别指望它自己干净 |
| **崩溃自愈** | 常驻进程挂了以后，不重启就是「之后每次调试都失败」。后端要有健康探针 + 自动拉起，前端不能挂死 |
| **版本失效即重启** | App 源码更新、或我们改了 `appservice/` → 进程里还是旧类。用 App 仓库 + appservice 的 hash 当键，对不上就换进程 |
| **与跑批分开实例** | 批量校验跑同一套 Robolectric 管线。**共用一个实例会让跑批把调试堵住**（反之亦然）；同一份 daemon 代码，两个入口各起一个 |

> lessons §二十六 记过一句「改成常驻服务是把测试框架当生产容器用」——那句的结论
> 是**不重写后端**，不是「不能常驻」。常驻的代价就是要正面处理上面这张表。

**关键约束（动手时重读）**：

- **零入侵边界不放松**：shadow 只在测试编译里挂（`@Config(shadows=[])` 加一行），
  App 源码一行不动；每批跑完验 App 仓库 `git status` 为空（lessons 里已有此纪律）。
- **调试结论不落 checks / meta 的批次体系**：它是交互产物（人看着改规则），
  与校验批次的「每源每 URL 取最深」语义不同；落点只在前端会话（或另立
  `jvm_debug:<ts>` 键），别混进 `jvm_check`。
- **`Debug.log` 只在 `debugSource == sourceUrl` 时 emit**（`Debug.kt`）：一次一个
  会话，A4 的后端要保证串行（同一 JVM 实例同时只调一个调试），或每请求起新 JVM。
- **key 语法照抄不发明**：`::`/`++`/`--` 前缀是 App 的方言（`Debug.kt`），
  前端已有的 key 拼装逻辑直接复用，别做一层自己的翻译。
- **两侧契约只有一份，A4 动手前先钉死**：NDJSON 行 schema / `args.properties`
  键表 / key 方言，落点 `core/jvm_debug.py` 顶部注释 + 一条契约测试
  （Kotlin 产出的样例 NDJSON 存成 fixture，Python 解析端断言能吃）。
  行 schema 建议直接写 `Event` 的三件套 `{"kind","elapsed_ms","text"}`——
  `text` 用 `Event.message`，**它已含 `[mm:ss.SSS]` 前缀**（`Debug.log` 的
  `debugTimeFormat` 自己加的，A1 不用算时间、别再包一层）。
- **A1 的「同构」要靠对拍，不靠眼看**：同一条源分别走设备 WS 与 A1，
  逐事件比（条数、`┌/└` 成对、`◇` 统计行、段名序列）。取样口径写死：
  `health=ok` 且不含 `@js:`/webView 的源各取 5 条。
- **零事件有两种成因，症状相同**：① 传入源的 `bookSourceUrl` 与 App 内部
  用的不一致（表单态源最容易出）→ `debugSource` 不匹配，一条都不 emit；
  ② Robolectric 主 looper 默认 PAUSED，scope 选错 dispatcher 事件收不到。
  A1 对两者都要**显式报错**（分不清成因就都写上），不能静默空文件——
  这与连 App 的 `_ZERO_EVENT_HINT` 是同一条教训。
- **批次顺序：A4 放最后**：它要动 `SourceEditDialog.vue`，而前端正被并行改动
  （2026-09-19 实测 `TidyDrawer.vue`/`styles.css` 同时被改且落了提交）。
  动手前先 rebase、改动收敛到「通道切换」一处；开发在分支上跑，落地即删
  （lessons §五十七 的前科 + AGENTS #19）。

**风险与预案**：

| 风险 | 预案 |
|---|---|
| `Debug.log` 走 `Log.d`/Handler/Looper 等 Android 运行时 | ~~Robolectric 全都 shadow 了~~ **2026-09-19 审阅订正：事件流根本不走 `Log.d`**——`Debug.log` 把 `Event`（kind/message/timestamp/elapsedMillis）emit 进 `Session.events` 的 Channel Flow，`Log.d` 只是 `BuildConfig.DEBUG` 旁枝，失败也不影响事件流（都查证过符号：`Debug.kt` 的 `log`/`Session.emit`）。真正要防的是上面「零事件两种成因」那两条。要新 shadow 的确实只有 WebView 一层 |
| A2 做成「渲染后喂解析器」的形态 → 假阴性 | **这是 A2 最贵的错**：`xhr_mode` 类源渲染完的 DOM 里没有图片地址（页面用 XHR 拉成 blob），解析器判「正文取不到」而真机是好的——比延期糟，因为它长得像结论。shadow 必须执行源自己的 `webJs`（渲染 + `Runtime.evaluate` + 空结果重试），语义对齐 App 的重试纪律；spike（见批次表）先把这个形状验出来 |
| A2 整体受阻不交付 | **有降级路径，不阻塞主线**：`@js:` 规则跑在 Rhino 里、不经过 webView（S1 全量已证），A1+A3+A4 照常发——得到「不填 IP、不推送、能跑 JS 规则」的调试器；只有带 webView 选项的源（空壳名单那批）要等 A2，而那批同时卡 §九 的 L2/L3 → §九 推迟，主线照常 |
| SnifferWebClient（sourceRegex 嗅探）语义 shadow 不全 | 首版只支持 HtmlWebViewClient 路径（绝大多数源）；`sourceRegex` 非空的源显式报「暂不支持」，不静默给错结果——AGENTS #4 |
| isRule 注入（`source`/`java` 对象加进 JS 上下文） | `addJavascriptInterface` 在 Robolectric WebView 里本来就 shadow；我们的 shadow 求值时把 `WebJsExtensions` 语义留待按需补，先显式报不支持 |
| 每次 JVM 冷启动太慢打断调试节奏 | 先量再定（第二期那段有量法）：若大头是**页面渲染/请求**，daemon 消不掉它，先做 §九；若大头是**入场费**（JVM+Robolectric+App 初始化），做常驻——**别一开始就建，那是整个方案里最大的工程增量** |
| 同 URL 调试时源未保存（表单态 vs 库里态） | 调试用**表单态源**（序列化传给 JVM，与连 App 的 `confirmPush` 语义对齐），不要求先保存——这是比连 App 更顺的一点，也是「不写库」的自然推论 |

## 二、真机复检通道（原「让 App 承担校验」的归宿）

**定位（2026-09-19，随 §一点八 收窄）**：不再是校验主力——**主线是 JVM 服务**。
真机只在三类上不可替代，保留为**用户主动触发的「复检」**：① **登录墙**（`loginUrl`
491 / `enabledCookieJar` 2004 / 判出 auth 1051——JVM 无设备登录态，§一点八 A3 的
cookie 注入能覆盖「**用**登录态调试」，覆盖不了「**取得**登录态」）；② **WebView
依赖**（浏览器桥已兜大半，残余归这）；③ **用户自己的网络出口**（IP / 代理 / DNS
与电脑不同）。

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

## 三、本地回放：语法缺口（按需）

> **已修的三条别再重复查**：`bookUrl`/`chapterUrl` 的**列表作用域**、
> `text.`/`children.` 简写的**判定半边**（一律 unknown）、`_probe_toc` 接入 `tocUrl`
> （含 `verify.py` 把 tocUrl 当 URL 字符串的附带 bug）——机制与实测见 lessons
> §二十三「第二次实证」与 §四十四，细节看 `git log`。

**定位**：本地回放现在是「App 不在场的快速筛选 + 调试/修复需要 HTML 的那条线」。
下面这些「本地验不了」的语法**由 JVM 服务兜住**，只有还留在交互路径（调试抽屉 /
AI 修复）的语义才急。**做之前先确认它还在不在那条路径上。**

| 写法 | 源数 | 说明 |
|---|---|---|
| **排除索引 `li!0` / `dd!0:1:2`** | **589** | 最大一块；`findIndexSet` 里 `.` 与 `!` 是**相反语义**的分隔符，我们只实现了点式 |
| 执行期兜底的「选择器解析不了」 | 257 | 样例是 URL 模板、JSONPath 片段、碎片——**还没逐个归类** |
| JSONPath 超出子集（`[1:3]` 切片等） | 163 | `[*]` 与 `['键']` 已支持 |
| `//` 开头的 XPath | 122 | 要引入 XPath 引擎，或者一直 unknown |
| 方括号索引式 `[-1]` / `[0]` / `[1,3]` | 117 | |
| 区间索引 `[0:10]` | 37 | |
| `text.` / `children.` 简写 | 16 | 语义已查清，实现即可 |

**另一个缺口：webView 型正文（本地与 JVM 都验不了，只有真机验）**

上面那张表是「**语法**回放不了」；这一条是「**取数**拿不到」。实测
`口袋漫画` 暴露的口径：它的正文本就是 `params` 加密 + `xhr_mode`，
图片地址**只在解密后的 JS 对象里**（DOM 里是 blob/空），所以

- 本地回放器：`@js:` 不支持 → `content_ok=None`（诚实报「验不了」，没问题）；
- JVM 校验服务：默认**剥掉 webView 选项**，`webJs` 因此不执行 → 同样拿不到；
  就算不剥，Robolectric 里的 `BackstageWebView` 也是桩，渲染不出东西；
- **只有真机（App 自己的 WebView）能验** —— 本次就是这么验的（连 App 调试，
  四段全通，正文 5 张 / 58 张与站点解密结果逐一对上）。

**动手的落点**：S5-A2 的 `ShadowBackstageWebView`（见 §一点八）——它按设计就是
「`getStrResponse()` 委托浏览器桥（渲染 + 求值 `js` 选项 + 空结果重试）」，
补上它，这一类源在 JVM 里就能验；在那之前，**别把 `content_ok=None` 当成源有问题**，
也别为了「让本地能验」把 webView 去掉——去掉就真的读不了。

## 四、分类侧：剩下呈现与出口

> **档位重设计 2026-09-19 已完成**（9 档 + 未校验 → 6 档：ok / auth / gfw / cert /
> pending / dead，判据是「下一步动作是否相同」；timeout、error、no_search、skipped
> 并入 pending）。存量的实测分布、判据与迁移见 lessons §五十二 与 AGENTS #17。

- **「没结论」缺产品出口**（步骤级 unknown、以及 ❓待验证）：界面上只有解释文案，
  没有转化动作（如「连 App 验一次」）——
  归宿是真机复检通道（§二，S5）。
- **标签是快照，导入即开始过期**：写进 App 的分组标签是校验瞬间的结论，App 侧
  无刷新通道；叠加 ok 档 14 天 TTL，最坏差两周以上。要动它属于大改（反向同步
  通道），先记录不排期。

## 五、「合并重复源」剩 P2

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
  Legado 固定取第 0 条，见 `Debug.kt`）。§一点八 A4 做通道切换时一并考虑：
  JVM 侧 key 由我们拼装，pick 语义可以直接进 A4 的参数面。
- 点击摘要里的变化数 → 按对应筛选跳转（需与统计条 chip 的筛选状态协同）
- 校验历史时间线（每次校验的前后对比，而不只是聚合数）——
  **依赖放宽 `checks` 的保留策略**：现在每源只留最近一条，时间线至少要留 N>1 条
  （改一个数字的事，但得先定 N 定多大、以及它带来的库增长），见 lessons §二十八
- `TrashDrawer` 的批量恢复对齐同一套 URL 语义（它仍是页级勾选 + 行对象，
  与列表页 `ec93ad4` 之后的 URL 语义不一致）

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

## 八、界面文案收口（批 2 / 批 3）

**机检**：`tools/check_copy.py`（术语表与检查规则的**唯一事实来源**）+
`tests/test_copy.py`（闸门，跟全量测试一起跑）；基线 `tools/copy_baseline.json`
**只减不增**，当前剩 **147 条** error 档。规范与前后对照见 lessons §四十六。

- **批 2（前端）**：`SourcesView` 与各抽屉的状态/统计/toast。最大一类是**半角冒号**
  （`ElMessage.error("xx失败: ")` 形态）。顺手清术语表盲区——机检只抓表里有的词，
  「本地复盘」这类漏网词要么进表、要么改掉。
- **批 3（后端进界面的话术）**：`core/checker.py`、`core/quality.py`、
  `core/app_debug.py`、`backend/api/*`、`cli/main.py`。
  **改文案必须同步改钉住它的断言**（实测 6 处：`test_quality.py` 的
  「无法离线回放」「正文提取为空」「疑似错误页」「章节数偏少」、
  `test_legado_rules.py` 的「选择器无法解析」）——AGENTS #12。
- **永久例外**：必须逐字保留的串（如换词表的键，见 AGENTS #17）在行尾写
  `# copy-ok: 理由`，**不进基线**（AGENTS #18）——基线里出现永久条目等于把闸门关掉。

---

## 九、调试体验：先定层，再写规则（**排在 S5-A 走通之后**）

**为什么现在不够好用**（四条都是实测，源就是本仓的 `口袋漫画`，机制见 lessons §五十八）：

- **候选只提「字段」半边**：`frontend/src/utils/ruleCandidates.js` 给的是 `.X@href`
  这种，而 `bookUrl` **单独拿出来没有意义**——它按 Legado 语义在 `bookList` 节点内
  求值，容器写宽了（如 `tag.a`）就会先命中导航栏第一条链接，详情页变成站点首页。
  现在要用户自己把两半配起来，而**容器配错时字段规则看起来完全正常**。
- **只认元素的第一个 class，永远不会提 id / 结构选择器**。实测这个源的正确目录规则
  是 `#chapter-grid li`（id），而候选里只会出现 `.bg-gray-100@tag.a@href` —— 后者
  取到 **1228** 条，混着 30 条重复的「最新章节」。
- **判据只有「取到几条」，没有「这个集合准不准」**。1228 与 1198 在界面上都只是个
  数字，**重复 30 条、顺序倒置完全看不出来**。缺的是：去重后条数 / 与页面链接总数的
  占比 / 是否混进导航页脚。
- **AI 提议在这类页面上是「在错材料上生成」**：`POST /rules/suggest` 把**我们抓的
  原始 HTML** 交给模型，而 L2/L3 页面的数据根本不在那份 HTML 里（实测章节页原文只有
  `params = '<2264 字符 base64>'` 和一个空的 `#chapter-images`）。模型不知道自己在
  瞎猜，而 `dry_run=false` 还是花钱动作——AGENTS #3 的闭环（提议→回放验收）**两头
  都断了**：材料不对、验收也跑不了。

**范式：先定层，再写规则。** 分层的判据是「下一步动作是否相同」（同 AGENTS #17），
**全部不需要执行 JS、也不需要解密**：

| 层 | 自动判据 | 下一步动作 |
|---|---|---|
| **L1 静态直出** | 抓下来的原文里就有目标（`pageStats`/`hasWanted` 已能算） | 写选择器 |
| **L2 JS 壳** | 目标容器存在但为空（`imagesWithSrc == 0` / `#app` 空）；或源已带 webView 标记 | 换能跑 JS 的通道 + `webJs` |
| **L3 加密负载** | ① `var\s+\w+\s*=\s*['"][A-Za-z0-9+/=]{200,}['"]` ② 含 `CryptoJS` / `_0x` 混淆 / `decrypt(` ③ `xhr_mode` / `createObjectURL` 字样 | `webJs` 读**解密后的全局对象**（**不自己实现解密**：密钥会轮换、混淆会变） |
| **L4 接口取数** | 原文里没有列表，但有 `fetch(` / `axios` / `/api/`；渲染后才有 | 抓接口 + JSONPath |
| **L5 需登录/被墙** | 401/403/空内容 + `loginUrl`/`enabledCookieJar`；或 DNS/TLS 信号（`diagnose` 已有） | 真机通道 + cookie |

**分四批**（前三批是纯前端 + 纯函数，与 S5-A **无技术耦合**；排在它后面只是为了让
「通道选择」一次做到位，不用改两遍）：

| 批 | 做什么 | 验收判据 |
|---|---|---|
| **九-1 定层** | 新纯函数 `classifyLayer(html, source)` → 层 + **命中的证据行**；抽屉顶部横幅展示。零后端改动 | 拿本仓 `口袋漫画` 判出 L3 且列出 ≥3 条证据；拿一个静态源判 L1 且不误报 |
| **九-2 候选按层改写** | 容器与字段**成对**给（`bookList` + `bookUrl` 一起）；补 id / 结构选择器；每条标「命中 N / 去重 K / 占页面链接比」；**L2–L4 时隐藏候选面板**（在这页上它只会给出「能选中但不是数据」的东西） | 口袋漫画目录能提出 `#chapter-grid li`（现在提不出）；1228 与 1198 能被「去重 K」分开 |
| **九-3 AI 提议加前置条件** | 层不是 L1（或回放器跑不了这一步）时**禁用按钮并说明原因**；`dry_run=true` 那部分免费逻辑保留 | L3 页面上按钮禁用且有文案说清「数据要渲染/解密后才有，AI 看不到」；L1 页面行为不变 |
| **九-4 对数验收** | 同一段用本地与引擎各跑一次，**并排比数量**（如「本地 1198 / 引擎 1198」） | 数量不一致显式标红；一致给「已对数」。这是本会话唯一真正管用的验收手段 |

**关键约束**：

- **判层要给出证据行**，不能只给结论——用户要能反驳它（「你说加密，哪一行看出来的」）。
- **别在候选面板里保留「原文 HTML 上的选择器」这条路**：对 L2–L4 它是**假证据**
  （能选中，选中的不是数据），这正是「找出来的不符合预期」的根因。
- **判官是源自己声明的能力**，不是用户的偏好：`ruleContent.webJs` 非空 / URL 规则带
  webView → 直接进 L2/L3 分支，不用等页面统计（AGENTS #13：能推导的别让用户填）。

---

## 已明确不做（决策记录）

**在 `skills/legado-source-lessons` §二十六**——那是决策记录，不是待办。放这里会和
「还没做」混成一片。本节只留一句：**一/五/六/二/(A) 那批 App 路线方案、逐源调试 WS
批量化、本地补 JS、XPath 引擎、星级收紧、appservice 拆独立仓库等，都已在那里记了理由。**

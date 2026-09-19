# 待办与二期候选

> **分工**：本文件只放**要做什么**（动作 + 够动手用的依据）。可复用的机制与踩坑
> 写进 `skills/legado-source-lessons`，这里只留一行指针；**已完成的事项从这里删掉**。
>
> **最近已修**（09-19 这批，细节看 `git log`，机制见 lessons §四十八～§五十一）：
> 回放边界判定（简写/索引式/排除索引/XPath 一律 unknown，顺带修掉 JSON 下标取不到值
> 的路由 bug）、`tocUrl` 按规则在详情页求值、**取值类规则末段语义**（290 条误放项）、
> `HEALTH_ORDER` 补 CERT、**修复循环接登录墙**（evidence 用 `checker.is_login_wall`）、
> **JVM 校验服务探针跑通**（零入侵挂载，App 仓库 `git status` 恒为空）。
> 更早的以 lessons 与 git 历史为准——这份清单只回答「这条是不是刚做过」。
>
> **已定决策：缓存不做向后兼容**（`CACHE_VERSION`）。它是纯派生数据，口径一变就整体
> 作废；做兼容反而危险——旧缓存里的结论是旧逻辑的产物，混着用就是「修了等于没修」。

---

## 路线图（排期）

> 判据：**先让主线跑起来，再往产品里接，最后清存量**。每阶段都能独立交付。

| 阶段 | 做什么 | 依赖 | 完成判据 |
|---|---|---|---|
| **S0 收尾**（立刻） | 提交当前这批（改动文件 + `appservice/`）；`cd frontend && pnpm build` | —— | `git status` 干净、745 个测试绿、前端产物与源码一致 |
| **S1 服务化·搜索档闭环** ✅（2026-09-19，剩全量跑） | ValidateService（收源 → searchBookAwait → 结论 NDJSON）+ Launcher（参数走 `appservice/args.properties`）；(a) 剥 webView 选项 ✅；(a2) 壳归因 ✅（empty_js_shell）；100 条验收：46 ok / 18 no_result / 35 error（归因细分见 lessons §五十二）/ 1 empty_js_shell；结论已写 meta（`jvm_check:<batch>:<url>`） | S0 | **✅ 全量已跑完（2026-09-19，17 分钟/3774 条，8 并发 + 3g 堆）**：ok 1324 / no_result 935 / error 1444（被墙重置 462、JS 失败 313、DNS 死亡 291、JSONPath 不符 152、超时 66、其他 220）/ timeout 61 / empty_js_shell 10；剥 webView 的 147 条里 16 条被救回 ok；**环境缺口 0** |
| **S2 接进产品** | 设置项（App 源码目录 + JDK/SDK/gradle-home 自动推导）+ **自检接口/按钮** + 前端页签 + `checks` 来源阶梯（本地/JVM/真机） | S1 | 界面上能配、能自检、能看见结论来自哪一层 |
| **S3 补深度** | 目录段 + 正文段（`getBookInfoAwait` → `getChapterListAwait` → `getContentAwait(needSave=false)`）；**(b) 浏览器桥**（复用本机 Edge/Chrome + CDP） | S2 | 全链路可跑；(b) 上线后那批 webView 源不再是盲区 |
| **S4 清存量** ✅（2026-09-19） | Health 档位重设计 **+** 分类侧可行动性（**同一件事的两面，必须一起做**） | 可与 S3 并行 | **✅ 9 档 + 未校验 → 6 档**（ok/auth/gfw/cert/pending/dead，按「下一步动作」合并）；「未校验」降为派生筛选（`health=none`）；CACHE_VERSION 13；`checks` 旧值一次性映射 + 组名换词（`Store.migrate_health_tiers_once`）。**残留**：unknown 的产品出口、标签快照过期 → 见 §四 |
| **S5 按需** | 真机复检通道（只读那条先做）、本地回放的 `bookUrl` 作用域、体验类 P2 | —— | —— |

> ⚠️ **CACHE_VERSION 合并 bump**：S3/S4/S5 里凡是翻转判定结论的改动，**做完一批
> 再 bump 一次**、付一次全量重跑，别每步付一次。
> （S4 已 bump 到 **13**——档位词表变了，旧缓存的 health 是旧词表的产物。）

---

## 一、JVM 校验服务（主线；下一个动手的就是它）

**探针结论（2026-09-19，代码在本仓库 `appservice/`）：通了。**
Robolectric 下对一条真源（天堂深圳，无 JS 的 CSS 源）调 `WebBook.searchBookAwait`
**真实联网返回 8 本书**——App 的规则引擎在纯 JVM（无设备、无模拟器）里跑得起来。

**要补的依赖尾巴 = 全部代价，实测只有三项**（探针是一路撞出来的，每次都记了）：

| # | 缺口 | 补法 | 平台 |
|---|---|---|---|
| 1 | Koin 未启动（`AnalyzeUrl` 要 `DownloadCacheSettingsGateway`） | 测试里 `startKoin { modules(module { single<DownloadCacheSettingsGateway> { stub } }) }`——**纯数据类**（UA/线程数），约 5 行 | 全平台 |
| 2 | `appCtx` 未初始化（`appDb`/`CacheManager` 依赖它） | `RuntimeEnvironment.getApplication().injectAsAppCtx()`——splitties 的**公开 API**，1 行 | 全平台 |
| 3 | **9 处 assets 路径用 `File.separator` 拼接**（`DefaultData.kt` ×8、`DirectLinkUpload.kt` ×1）——JVM 跑在 Windows 时它是 `\`，而 `AssetManager` 只认 `/` | **已改成零入侵：测试侧 shadow 垫片**（`appservice/test/io/legado/app/probe/WindowsPathAssetManagerShadow.java`）——继承 Robolectric 的 `ShadowArscAssetManager14`，只把 `nativeOpenAsset` 等五个静态 native 的**路径归一成 `/`**，其余透传。**App 源码一行不动**。⚠️ 用 **Java** 写：该链路上拦的是静态 native 方法，Kotlin 覆盖不了静态方法（先试过 Kotlin 版，`Unresolved reference`）。**升级 Robolectric 时要一起看**：shadow 继承的类名（`ShadowArscAssetManager14`）随版本会变，失效时改用备用补丁 | **仅 Windows**（Linux 上本 shadow 无害） |

> **「不入侵阅读源码」已达成并验证（2026-09-19）**：App 仓库 `git status` **恒为空**
> （无已跟踪改动、也无未跟踪文件）——我们的全部代码住在 `legado-source/appservice/`，
> 由 init 脚本在构建时挂进去，App 仓库只被「读 + 构建」（唯一写入是它自己的
> `build/`，本来就 gitignore）。跟上游同步零冲突。

**环境一次性成本（已在本机装好，记录备查）**：

    JDK 17  D:\Program Files\Java\jdk-17          （sdkmanager 用）
    JDK 21  D:\Program Files\Java\jdk-21.0.12.1+1 （Gradle daemon 要 21；清华 Adoptium 镜像）
    SDK     D:\Android\Sdk                        （cmdline-tools + platforms;android-37.0
                                                   + build-tools;37.0.0 + platform-tools）
    Gradle  D:\.gradle                           （**GRADLE_USER_HOME 必须在 D 盘**）

**我们的代码全在本仓库 `appservice/`，App 仓库里一个文件都没有（2026-09-19 达成）**：

    appservice/
      legado-gradle.bat              启动器：设好四个环境变量 → pushd 进 App 仓库 → 调它的 gradlew
      legado-test.init.gradle        init 脚本：把下面的源码/资源挂进 :app 的 test 编译
      test/io/legado/app/WebBookProbeTest.kt
      test/io/legado/app/probe/WindowsPathAssetManagerShadow.java
      test/resources/probe_source.json
      windows-assets-portability.patch   备用：9 行 File.separator→/（shadow 失效时才用）

    跑法（在 appservice 目录下，或写成绝对路径）：
        legado-gradle.bat :app:testDebugUnitTest --tests io.legado.app.WebBookProbeTest

    ✅ 已验证：`git status` 在 App 仓库里**恒为空**（无 M、无 ??），跑通探针返回 8 本书。
    换机器只需改启动器里的 LEGADO_REPO / JAVA_HOME / GRADLE_USER_HOME / ANDROID_HOME。

**四个必须知道的坑（全部实测踩过）**：

1. **`GRADLE_USER_HOME` 必须与项目同盘**。默认在 `C:\Users\<u>\.gradle` 时 KSP 报
   `this and base files have different roots`（跨盘符无法相对化路径）——把整个
   `.gradle` 移到 `D:\.gradle` 即解，与代码无关。
2. **sdkmanager 自带的下载器会卡死**（实测卡在 16MB 不动），curl 直接拉包正常。
   手动装法：下 `platform-37.0_r02.zip` + `build-tools_r37_windows.zip` 解压到
   `platforms/android-37.0`、`build-tools/37.0.0` 即可（`source.properties` 自带）。
3. **挂载外部源码只能用「编译任务 `.source(dir)`」，不能碰 `android.sourceSets`**：
   后者会让 Gradle 的**测试发现**整个失效——连 App 自带的测试都报
   「No tests found for given includes」。原因：测试识别依赖「类文件 ↔ 源码路径」的
   相对映射，源码根一旦跑出项目根，映射就断。init 脚本里已按前者实现。
4. **启动 App 仓库的构建必须让 CWD 就是仓库根**（`pushd` 进去），**不能用 `-p`**：
   同理，`-p` 下 CWD 停在别处会让测试发现静默失效。另外从 git bash 调 `.bat` 时
   **不要给 `--tests` 的值加引号**——MSYS 会把 `"` 转义成 `\"` 原样传进去，Gradle
   收到的过滤串带反斜杠 → 同样报「No tests found」（在 cmd/PowerShell 里没这个问题）。

**保真度分层（探针只验证了第一层，别当成全通）**：

| 层 | 内容 | 状态 |
|---|---|---|
| 已验证 | 无 JS 的 CSS 源：`AnalyzeUrl` → okhttp → `BookList` 解析 | ✅ 返回 8 本书 |
| 大概率可用 | `{{}}` 模板 / `@js:` 纯文本后处理（Rhino 在 JVM 已由仓库自带
  `JsTest`/`RhinoContextEntryTest` 证明）；`java.ajax`（OkHttp 可实现） | 未实测 |
| 存疑 | `cacheFile` / `androidId`（Robolectric 有 shadow，行为未必等价）；
  `webView` / `startBrowserAwait`（要真浏览器，JVM 里结构性跑不了） | 未实测 |

**下一步（服务化，真做时按此动手）**：把 `WebBookProbeTest` 从「测试方法」变成
「常驻 JVM 服务」——Ktor（仓库已在用）+ 一个 `/validate` 端点，收内联书源 JSON +
关键词 → 依次调 `searchBookAwait` / `getBookInfoAwait` / `getChapterListAwait` /
`getContentAwait(needSave = false)` → 返回我们设计的结论结构。放进 `app/src/test/`
保持「**不改 App 源码**」的边界（已由 shadow 垫片证明可行，见上表第 3 行）。

**为什么它是当前推荐（2026-09-19 定，含产品侧的取舍）**：

- **技术侧**：批量、无人值守、CI 可跑、不碰任何 App 数据、不需要设备；规则语义是
  App 的（不是我们复刻的），失真只剩「环境差异」这一小截。
- **产品侧（决定性理由）**：它是**唯一能当"功能"而不是"流程"的那条**。校验在界面上
  就是一个按钮——点、等、看结果；而连真机那条要求「设备在同一局域网 + App 在跑 +
  先备份后还原」，任何一环缺了，用户看到的是"工具坏了"，不是"这个源有问题"。
  一个**随时能用**的校验器，比一个**绝对准确但设备在场才能用**的校验器更常被用，
  而"常被用"决定数据是否新鲜、决定这个功能有没有价值。
- **真机那条的定位（不是淘汰，是升级成"复检"）**：它有两个 JVM 给不了的东西——
  ① **登录态**（`loginUrl` 491 条 / `enabledCookieJar` 2004 条）：JVM 无 cookie，
  登录墙后的源一律判「需验证」，而那批源在用户的 App 里可能完全好用；
  ② **真实阅读面**（书架加权健康度：我在读的书，源还好吗）——这是 JVM 结构上
  拿不到的、也最贴近用户价值的一层。
- **要落到数据模型上的**：每源结论带**来源阶梯**——`本地回放 < JVM(App 引擎) <
  真机(App + 真实环境)`，与既有的 `measured / static` 是同一件事的延伸。界面按
  这个阶梯展示，用户才知道该不该信、以及「要不要连手机复检一次」。

**服务化与设置项（2026-09-19 定，动手前的设计约束）**：

- **App 源码目录进全局设置**（`core/settings_store.DEFAULTS` 新增一个 section，前端经
  `GET /api/settings` 读写；**不在前端硬编码、不写死在本仓库的脚本里**——现在的
  `appservice/legado-gradle.bat` 里那几个路径是开发期的临时形态）。路径这类
  「算不出来的状态」才让用户填（AGENTS #13）。
- **但只能要一个路径，其余全部推导**：JDK 走 `JAVA_HOME` → 常见安装目录扫描；
  Android SDK 走 `local.properties 的 sdk.dir` → `ANDROID_HOME` → 常见默认路径；
  **Gradle 用户目录由仓库路径推导**（必须与仓库同盘，跨盘会触发 KSP 那个
  `different roots` 的坑）——让用户填一个「必须与另一个字段同盘」的字段，
  本身就是让手工维护一个能算出来的状态。
- **必须有「自检」**（一个接口 + 一个按钮）：首次使用要下 SDK/Gradle、首次编译十几
  分钟，用户必须能看见「缺什么、在装什么」。**别让它变成 App 连接页签那种
  「点开只有一句话」的状态**（那条待办见 P2）。
- **形态先做一次性调用**（我们后端 subprocess 调服务，跑完退出），别一上来就做常驻：
  常驻要管生命周期、端口、守护进程回收，等确实嫌慢（增量 ~15s/次）再说。

**App 连接的定位修正（2026-09-19）：校验不再必需它，调试也不是它更好**：

| | 谁主 | 理由 |
|---|---|---|
| 校验 | **JVM** | 语义是 App 的，且链路比设备那条短（无备份还原） |
| 调试 / AI 修复 | **JVM**（新结论） | **JVM 能给 HTML，设备调试 WS 给不了**——`build_steps` 的 `matched_html` 恒为空串（lessons §五十一 已记），模型在设备那条路上是失明的；JVM 服务是我们自己写的，可以直接交回抓到的 HTML 与命中节点 |
| 兜底复检 | **真机** | 三类只有它覆盖：① **登录墙**（loginUrl 491 / cookieJar 2004 / 判出 auth 1051——JVM 无 cookie，这批在用户手机上可能是好的）；② **WebView 依赖**（JVM 结构性跑不了）；③ **用户自己的网络出口**（IP / 代理 / DNS 与电脑可能不同） |

→ 所以**不删 App 连接**，把它从「必经环节」降级为「复检通道」，并**顺手把那个悬着的
设置页签做成它的配置页**（正好解掉 P2 里「要么实现要么撤承诺」那条）。

**WebView 依赖：机制、规模与结论（2026-09-19 调研，全库 3774 在用源）**：

**① 机制（读 `BackstageWebView.kt` 查清）**：WebView 只是**取数层**的替代品，不参与
规则求值——接口收口在 **一个类、一个方法**（`BackstageWebView(...).getStrResponse()`）。
缺省行为就一句话：**加载页面 → 执行 JS（默认 `document.documentElement.outerHTML`）
→ 把渲染后的 DOM 当作响应体返回**，顺带把浏览器 cookie 写回 `CookieStore`。
两个例外：`sourceRegex`/`overrideUrlRegex` 走嗅探模式（返回命中的**资源 URL**）；
`isRule=true` 时会往页面里注入 `java.*` / `source` / `cache` 三个 JS 接口。

**② 数量与真相（实测）**：

| | 数 | 说明 |
|---|---|---|
| 触碰 WebView 的源 | **324（8.6%）** | `webView` 选项 224 / `startBrowser` 95 / `sourceRegex` 17（有重叠） |
| 其中**自带页面 JS**（URL 选项 `"js"`） | **仅 3 条** | → **「要复刻 `java.*` 注入」的长尾几乎不存在**，替代只要「加载+取 DOM」 |
| 其中 health=**ok**（真正在用的） | **102**（5★ 18 条） | 其余：auth 121 / timeout 61 / gfw 12 / dead 10 / error 2 —— 那些本来就不是 WebView 能救的 |

**③ 反直觉的坑：JVM 服务照抄 App 代码，会比现在的本地校验**更差**。**
本地校验（Python）**从来没实现 webView 语义**——URL 选项里的 `webView` 被解析后
直接忽略，一律当普通 HTTP 请求。也就是说那 18 条 5★ **恰恰是「忽略 webView 之后
仍然跑得通」的证明**（站点对普通客户端本来就返回可用 HTML）。而 JVM 服务跑的是
App 的真代码，它会**忠实地**走 `BackstageWebView` → Robolectric 下是空实现 → 失败。

→ **所以 `strip_webview_option`（剥掉该选项再跑）不是优化项，是必需的补偿项**：
不做的话，JVM 服务上线第一天就会在这 102 条 ok 源上集体退步。
报告里要**显式标注**「该源带 webView 标记，本次按普通 HTTP 验证」——现状是静默忽略，
用户看到 5★ 不知道它是这么来的（与「把工具的能力边界说出来」是同一条原则）。

**④ 四条解法与取舍**：

| 解法 | 成本 | 何时用 |
|---|---|---|
| **(a) 剥掉选项**（默认开） | 0 | **先做这个**。证据表明大部分 webView 标记是冗余的 |
| **(a2) JS 壳检测 → 判 `unknown`（不是 fail）** | 小 | **与 (a) 同批做，且不可省**：剥掉选项后若页面是 JS 壳，规则会跑出空 → 那是**误判成「源坏了」**，正是本项目反复修的「把工具的欠缺说成源的问题」。判据：拿到页面但正文/列表为空 + 命中 JS 壳特征（`noscript`/"enable javascript"/脚本挂载点）→ unknown。**它保证不管有多少条，都不会被冤枉** |
| **(b) 浏览器桥**：shadow `BackstageWebView` → **驱动本机已装的 Edge/Chrome（CDP）** | **比原先估的低**：**不用下 Playwright 那 300MB Chromium**——用机器上已有的浏览器二进制（`--headless=new --remote-debugging-port=9222 --user-data-dir=<专用 profile>`），我们走 CDP：`Page.navigate` → `Runtime.evaluate('document.documentElement.outerHTML')`。因为只有 3 条源自带页面 JS，不需要注入 `java.*` 会话。⚠️ 新 Chrome 要求**非默认 user-data-dir** 才允许远程调试（安全加固），所以是专用 profile；好处是那个 profile 里可以放 cookie（对 121 条 auth 源有帮助） | **排期做，不再挂「等服务跑一遍看有多少条」这个条件**——理由见下 |
| **(b′) 把渲染抛给「网页前端」的浏览器** | **不可行（已否决）** | 卡浏览器**同源策略**：`fetch` 跨域读不到响应体；`<iframe>` 跨域**会渲染、JS 也会执行，但 `contentDocument` 读不到**——渲染发生了，结果取不回来。这是安全边界，不是工程问题。唯一绕法是浏览器扩展（要用户安装 + 按浏览器维护），门槛比 (b) 高 | 不做 |
| **(c) 真机兜底** | 已具备 | `startBrowserAwait` 那类（规则主动要浏览器：过挑战/拿 cookie）+ (b) 判定为真的 JS 壳 |

> ⚠️ **为什么 (b) 不能再等「数据」**（2026-09-19 订正，此前的「先不做」是错的）：
> ① **循环论证**——「还剩多少条真需要浏览器」这个数，**只有具备该能力之后才测得到**；
> 用带着缺口的工具去量缺口，量到的永远是「没被挡住的那部分」。那「几十条」是我拍的，
> 不是测的。
> ② **缺口会自我固化**——进不了验证的源要么被误判成「坏了」（用户可能误删可用源），
> 要么永远挂在「待验证」。这类源只会**累积**，没有任何机制让它自愈；而库是持续导入的
> （社区源越来越常带 webView 过反爬），方向只会更糟。本库 `created_at` 全是同一个月
> （一次批量导入），**趋势测不出来**，所以更不能拿快照当依据。
> ③ 成本已降到「复用本机 Edge + 替换一个类」，为「这一类源不再进盲区」付这点成本是值的。
> → 顺序：**(a) + (a2) 与服务化同批；(b) 排在服务化之后、作为独立一步**。

**⑤ 「是不是空需求」的答案**：**不是空需求**。已确认至少有 18 条 5★ 靠「忽略
webView」就能过（说明那批标记是冗余的），但**「真需要浏览器的还剩多少」这个数，
只有具备该能力之后才测得出来**（见上面 ④ 的订正：拿带缺口的工具量缺口是循环论证）。
所以结论不是「按今天的数决定做不做」，而是：**(a)+(a2) 立即做，(b) 排期做**——
前者保证不冤枉，后者保证这一类源不再进盲区。

**待设计**（与「四、Health 档位重设计」是同一件事，要一起做）：

- 路一的**「没结果」二义性**：流里只推**新命中**，所以「某源没出现」既可能是源坏了、
  也可能是它没这本书。缓解：多用几个词，某源全不命中才判坏。
- **结论怎么进 `checks`**：App 的三态（通过/失效/超时）与我们现有的
  `health`/`probe_depth`/`star_basis` 口径怎么映射、要不要记「结论来自 App」。
  来源阶梯（本地回放 < JVM < 真机）也落在这里。


## 二、真机复检通道（原「让 App 承担校验」的归宿）

**定位（2026-09-19）**：不再是校验主力——**主线是 JVM 服务**（见上）。真机只在
下面三类上不可替代，所以保留为**用户主动触发的「复检」**：

1. **登录墙**：库里 `loginUrl` 491 条 / `enabledCookieJar` 2004 条、校验判出 `auth` 1051 条。
   JVM 无 cookie，这批在电脑上一律「需验证」，而在用户手机上往往好用——这个偏差最伤信任。
2. **WebView 依赖**：JVM 结构性缺浏览器（见「JVM 校验服务」里的 (b)）。
3. **用户自己的网络出口**：IP / 代理 / DNS 与电脑可能不同。

**回推 vs 收割**：设备侧有两条可做的事，**优先做只读的那条**。
- **只读（推荐先做）**：`WS /searchBook` 搜 App 自己的库 + 读回 App 自带的校验结果
  （分组标签 / `respondTime` / `// Error:`），**一行 App 数据都不写**；参数与实测数据见
  lessons §五十一。
- **回推（要写 App 数据）**：`/getBookSources` 备份 → `/saveBookSources` 推我们的源 →
  跑 → 推回备份。REPLACE 按 `bookSourceUrl` 精确匹配，**唯一的真损失是「同 URL 两边版本
  不同」**（推送覆盖掉 App 侧的修改）。设备兼职日常使用就保留这套；纯测试机可省。

**怎么做一次复检**（脚本已删，步骤留在这里；协议细节与实测数据见 lessons §五十一）：

1. 设备与电脑同网段，App 开 Web 服务（HTTP 1122 / WS 1123）；**先 `GET /getBookSources`
   备份落盘**——回推路线靠它还原。
2. 只读复检：每个关键词开**一条新连接**（`Finished` 即 close）打 `/searchBook`，
   对账按 `origin` **去重数源**、两侧都过宽松归一（`strip` + 去尾斜杠）。
3. 要读回 App 自带的校验结果：让用户在 App 里点一次「校验」，再 `GET /getBookSources`
   读分组标签 / `respondTime` / `// Error:`——**读回后立刻映射入库，再让 organizer
   重建分组**（分组是它的地盘）。
4. 回推路线额外两步：`POST /saveBookSources` 推我们的源 → 跑完推回备份 →
   `GET /getBookSources` 与第 1 步对拍。
5. 从 git bash 调 `.bat` 时别给 `--tests` 的值加引号；`.bat` 必须全 CRLF（见 lessons §四十八）。

**判定**：真机结论的**证据等级最高**（真引擎 + 真环境 + 真 cookie），落 `checks` 时要
按来源阶梯标注（本地回放 < JVM < 真机），别和另外两条混成一个数。

## 三、本地回放：剩下的一件事

**只剩一件**：`bookUrl` 少了 bookList 作用域（旧称 §1-A）。

**证据**：实测 `book.sfacg.com`——现状把 `tag.a@href` 作用于**整页** → 解出 33 条
（全是导航栏），取第一条 = `https://www.sfacg.com`（漫画首页）；按 Legado 语义先在
`tag.form@tag.table.-2@tag.ul` 节点内取 → `https://book.sfacg.com/Novel/249775`（对的）。

**已修的两条**（别重复查）：`text.`/`children.` 简写的**判定半边**（一律 unknown）、
`_probe_toc` 接入 `tocUrl`（含 `verify.py` 把 tocUrl 当 URL 字符串的附带 bug）——
`git log` 与 lessons §四十四。

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
- **unknown 缺产品出口**：界面上只有解释文案，没有转化动作（如「连 App 验一次」）——
  与下面「App 连接」页签那条是同一件事的两面。
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



方向可行，但有个前提：**试跑的是表单里当前的规则**，未保存时 `fingerprint` 与库里
不一致，缓存写了也用不上（`is_cache_item_valid` 要比 fingerprint）。所以只对
**已保存的源**有意义。口径上没问题——项目已做「判定收拢到 `core.quality`」。
时效已有：`cache_ttl_ok`(14天) / `cache_ttl_other`(7天) / `cache_ttl_auth`(1天)，
**都已在设置里可改**
（键在 `core/settings_store.DEFAULTS["check"]`；这里不写行号——它每轮都漂）。

---


## 七、需先调研（**不要直接动手**）


> 顺着「取值类末段语义」那条（已修）发现的，先记在这里：
> `infer_type_static` 里 `declared == 2 → manga +1`、`declared == 0 → novel +1`
> （reclassify.py 打分段）——**被审对象给自己投票**：声明的类型正是要被推翻的
> 结论，却参与计分。权重低、方向上多数时候无害，但与 AGENTS #11「判定输入不能是
> 我们自己写进去的结论」有张力。调研换判据时应顺手拿掉，不必单独立项。

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

> ⚠️ **但这条思路现在搬不过来**：实测**管理库**（3861 条，2026-09-17 复测）里，
> **带 `downloadUrls` 的是 0 条**。（`data/candidates.json` 仍在，23MB——旧说法
> 说它已不存在，是错的；但类型判定的取样已统一到管理库，别再拿 JSON 当样本。）
> 所以两项都必须**先做一次调研**：摸清 3861 条里到底存在哪些**能定案**的结构信号，
> 再谈判据换不换。
> **在此之前不要动 `infer_type_static` 的出口**——没有信号就加出口，只会把
> 「猜域名」换成「猜别的东西」。

`reclassify --write` 在那之前仍只能在 `0` / `2` 之间翻转。
推迟的代价：`organizer.group_title` 与 `store._system_group_for` 都用
`BOOK_SOURCE_TYPE_NAMES` 生成类型标签，所以类型判定不补齐，App 里的类型分类就会
一直带着 ①④⑤ 的偏差。当时提供的是「人主动纠正单条源」的通道，不是批量修正。



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

---

## 已明确不做（决策记录）

**在 `skills/legado-source-lessons` §二十六**——那是决策记录，不是待办。放这里会和
「还没做」混成一片。本节只留一句：**一/五/六/二/(A) 那批 App 路线方案、逐源调试 WS
批量化、本地补 JS、XPath 引擎、星级收紧等，都已在那里记了理由。**

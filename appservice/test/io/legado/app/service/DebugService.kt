package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.Debug
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.AnalyzeRule.Companion.setCoroutineContext
import io.legado.app.model.analyzeRule.AnalyzeUrl
import io.legado.app.model.analyzeRule.RuleData
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.jsoup.nodes.Element
import kotlin.coroutines.coroutineContext
import java.io.BufferedWriter
import java.io.File

/**
 * 调试服务：在 JVM 里跑一次 App 的调试链，把事件流落成 NDJSON。
 *
 * **它替代的是「连 App 调试」的前半条**——设备 WS（`core/app_debug.py`）与它跑的是
 * 同一段 App 代码（`Debug.startDebug` → `WebBook.*`），区别只有一个：
 * **我们手里握着事件流，不经过网络、不需要设备**。所以 NDJSON 必须与设备 WS
 * 推的**逐事件同构**，`core/app_debug.py` 的 `_split_segments` / `build_steps`
 * 一行不改就能吃。事件流的形状照抄的是 App 自己的 WS 处理
 * （`BookSourceDebugWebSocket.kt` 里那段 `startDebug` + `events.collect`）。
 *
 * ## 两个「照抄 App」的决定，别随手改
 *
 * 1. **`isSourcePayload` 事件不落盘**（`SearchSource`/`InfoSource`/`TocSource`/
 *    `ContentSource`）。App 的 WS 就是这个过滤（`if (!event.kind.isSourcePayload)
 *    session.send(...)`），所以**设备 WS 里从来没有这几条**——那正是
 *    `matched_html` 恒空的原因。我们跟着丢掉，才谈得上「同构」；把原始 HTML
 *    塞进 NDJSON 会让 `values` 混进整页 HTML，还会与设备侧对不上。
 *    「看规则命中了什么」由**事件流跑完之后的命中回填**带回来（[collectMatched]）：
 *    按事件里的 URL 自己再抓一遍、用 App 的解析器跑该页的规则——它是侧车里的
 *    `matched_html`，不进 NDJSON。
 * 2. **不注入合成事件行**。超时/零事件**只**通过退出码 + stderr 表达，
 *    不往 NDJSON 里塞一行假的 Error——否则同一件事在两个通道里长得不一样，
 *    而「同构」是不变量；超时这件事由退出码承担。
 *
 * ## 退出码（调用方按它分派，**每一种的下一步动作都不同**）
 *
 * - `0` 正常：有事件且流自己关掉了（收到 `Completed`/`Error`）
 * - `2` 零事件：一条都没收到——**绝不当成功**（见 [ZERO_EVENT_HINT]）
 * - `3` 超时：`--timeout` 用尽；已收到的事件**仍然落盘**，是「部分结果」
 * - `4` 入参/输入错误，或进程内抛了异常
 * - `5` 流被截断：收到了事件，但**没等到终止事件**（App 侧异常/cancel/流中间被关）
 *
 * 「有事件」不等于「跑完了」——`0` 与 `5` 分开就是为了这件事：只看事件数的话，
 * 「收到两条就断了」会被当成成功。
 *
 * ## 参数（走 `appservice/args.properties`，由启动器转发；见 DebugServiceLauncher）
 *
 *     file=    源 JSON：数组取第一条，或单个对象
 *     key=     调试目标（`关键字` / `发现::URL` / `++URL` / `--URL` / 绝对 URL）
 *     out=     NDJSON 落盘路径
 *     timeout= 整次调试的墙钟上限（秒，默认 60）
 *     cookie=  手工注入的一条 cookie（可选；不给就按源 URL 从我们的浏览器 profile 读）
 *
 * 命令行形态是 `--file/--key/--out/--timeout/--cookie`——**与 ValidateService 同一套拼法**，
 * 因为拉起它的都是那个 `legado-gradle.bat` + `--tests <启动器>`。
 *
 * ## 产物有两个
 *
 * - `<out>`：NDJSON 事件流（与设备 WS 同构，只含非 payload 事件）
 * - `<out>.meta.json`：**侧车诊断**（退出码含义、事件数、payload 数、是否收到终止、
 *   是否超时、webView 警告、错误原文）+ **`matched_html`**（`{url: {段名: 命中 DOM}}`，
 *   形状的闸门在 Python 侧 `core/app_debug.matched_map`）。存在的理由：Gradle 默认**只在测试失败时**
 *   回显被测进程的 stdout/stderr，通过时全吞掉——调用方不能依赖 stderr 拿结论。
 */
object DebugService {

    //: 正常结束
    const val OK = 0
    //: 零事件——独立一个码，因为它是最容易被当成成功的那种失败
    const val ZERO_EVENT = 2
    //: 超时（可能有部分事件）
    const val TIMEOUT = 3
    //: 入参/输入错误，或进程内异常
    const val BAD_INPUT = 4
    //: 事件流被截断：有事件但没等到终止事件
    const val TRUNCATED = 5

    /** 整次调试的墙钟上限（秒）。比跑批的 30 宽：调试一条含正文段的链要渲染页面。 */
    const val DEFAULT_TIMEOUT_SEC = 60L

    /**
     * 命中回填的总预算（毫秒）。它在事件流跑完之后**另付**，所以要有自己的上限：
     * 调用方的等待是「调试预算 + 这一段」，而常驻那条链的读超时只留了 90s 余量
     * （`core/jvm_daemon.launcher_from_args` 的 `slack_sec`）。用尽就停，剩下的
     * URL 记进侧车（`matched_skipped`）——**不静默少记**。
     */
    const val MATCH_BUDGET_MS = 30_000L

    /** 单个页面取数的上限（毫秒）：一个 URL 卡住不该把整段回填拖到预算用尽。 */
    const val MATCH_FETCH_TIMEOUT_MS = 10_000L

    /**
     * 每段最多记几个命中节点。**与 `core.quality.MATCHED_NODES_LIMIT` 同一个数**：
     * 抽屉里那块 DOM 原来由本地投影算（也是取前 N 个节点的 outerHTML 拼起来），
     * 数不一样两边就没法对着看。逐词比对钉在
     * `tests/test_jvm_debug_contract.py::TestMatchedStepNameParity`。
     */
    const val MATCHED_NODES_LIMIT = 3

    /**
     * 零事件是这条链最典型的**静默失败**，两种成因：
     *
     * ① 源的 `bookSourceUrl` 与 App 内部用的不一致 → `Debug.log` 的
     *    `debugSource == sourceUrl` 不成立，一条都不 emit。**表单态源最容易出这个**：
     *    调试用的是编辑框里那份，而库/App 里那份的 URL 可能被规范化过。
     * ② Robolectric 主 looper 默认 `LooperMode.PAUSED`，scope 的 dispatcher 选错
     *    就收不到事件。
     *
     * 症状一样、成因不同，所以两个都写上——这比让调用方对着一个空文件猜强。
     */
    const val ZERO_EVENT_HINT =
        "本机引擎没有收到任何事件。两种成因：① 源的 bookSourceUrl 与 App 内部用的" +
            "不一致（调试用的是表单态那份，URL 被规范化过就对不上）；② 协程 scope 的 " +
            "dispatcher 不对（Robolectric 主 looper 默认不自动跑）。都不是「源坏了」。"

    private val webViewPattern =
        Regex("\"?webView\"?\\s*:\\s*(?:true|1|\"true\")", RegexOption.IGNORE_CASE)

    @JvmStatic
    fun main(args: Array<String>): Int {
        var file: String? = null
        var key = ""
        var outPath = ""
        var timeoutSec = DEFAULT_TIMEOUT_SEC
        var cookie = ""
        var i = 0
        while (i < args.size) {
            when (args[i]) {
                "--file" -> { file = args[++i] }
                "--key" -> { key = args[++i] }
                "--out" -> { outPath = args[++i] }
                "--timeout" -> { timeoutSec = args[++i].toLong() }
                "--cookie" -> { cookie = args[++i] }
                // 未知参数**显式拒绝**、不静默忽略：拼错的参数被吞掉会让人以为
                // 「跑了但没结果」。退出码 4 与「零事件」分开，动作不同（改参数 vs 查源）
                else -> {
                    System.err.println("[appservice] 未知参数: ${args[i]}")
                    return BAD_INPUT
                }
            }
            i++
        }
        if (file.isNullOrBlank() || key.isBlank() || outPath.isBlank()) {
            System.err.println("[appservice] 缺参数：file/key/out 都要给（timeout 可选）")
            return BAD_INPUT
        }
        if (timeoutSec <= 0) {
            System.err.println("[appservice] --timeout 必须为正数，收到 $timeoutSec")
            return BAD_INPUT
        }
        return runOnce(Config(file, key, outPath, timeoutSec, cookie))
    }

    /**
     * 一次调试的入参。**命令行与常驻 daemon 共用一份**——两份各写一遍的后果是
     * 「命令行跑出来的和界面上跑出来的不一样」（`core/jvm_debug` 存在的同一个理由）。
     */
    data class Config(
        val file: String,
        val key: String,
        val outPath: String,
        val timeoutSec: Long = DEFAULT_TIMEOUT_SEC,
        val cookie: String = "",
    )

    /**
     * 跑一次调试：读源 → 注入 cookie → 走 App 的调试链 → 落 NDJSON + 侧车 → 退出码。
     *
     * **一次性的 `main` 与常驻 daemon（S5-A 第二期）都走这一份**：常驻只是把「进程活多久」
     * 换了，跑的行为必须逐字段一样——那正是 D1 的验收判据（与「各起一次 JVM」对拍）。
     */
    fun runOnce(cfg: Config): Int {
        val file = cfg.file
        val key = cfg.key
        val outPath = cfg.outPath
        val timeoutSec = cfg.timeoutSec
        val cookie = cfg.cookie

        // 计数器是**每个请求**的：常驻进程里不清就会累积，而侧车里的
        // `shadow_webview_calls` 判据是「普通源必须是 0」——累积之后那条诊断会说反话。
        ShadowBackstageWebView.reset()

        // 源 JSON：数组取第一条（与跑批同一个导出形状），也允许单个对象（手工跑方便）
        val sourceJson = try {
            val text = File(file).readText(Charsets.UTF_8).trim()
            if (text.startsWith("[")) {
                val arr = kotlinx.serialization.json.Json.parseToJsonElement(text)
                    as kotlinx.serialization.json.JsonArray
                if (arr.isEmpty()) "" else arr.first().toString()
            } else {
                text
            }
        } catch (e: Exception) {
            System.err.println("[appservice] 读源文件失败: ${file} — ${e.message?.take(160)}")
            return BAD_INPUT
        }
        if (sourceJson.isEmpty()) {
            System.err.println("[appservice] 源文件里没有源: $file")
            return BAD_INPUT
        }
        val source: BookSource = try {
            GSON.fromJsonObject<BookSource>(sourceJson).getOrThrow()
        } catch (e: Exception) {
            System.err.println("[appservice] 源 JSON 解析失败: ${e.message?.take(160)}")
            return BAD_INPUT
        }

        // 带 webView 的源在这里**必须显式说出来**：取数那一步现在由 A2 的 shadow 桥委托给
        // 真浏览器，但它有**三条明确不支持的边界**（`ShadowBackstageWebView` 的类注释：
        // `isRule` 注入路径 / `sourceRegex` 嗅探 / 只给 html 的 loadDataWithBaseURL）——
        // 撞上边界时那几段会被读成「取不到」，看着像源坏了（AGENTS #4 的同一类错）。
        // 所以这里只警告不拦：搜索段这类不受 webView 影响的段仍有价值。
        // 同一个判据进侧车（`webview_unsupported`）；**那个字段目前没有程序化消费方**
        // （前端不读它），留着是给排障的人看的那份原始事实。
        val webviewSeen = webViewPattern.containsMatchIn(sourceJson)
        if (webviewSeen) {
            System.err.println(
                "[appservice] ⚠ 该源的 URL 规则带 webView：取值交给浏览器桥，" +
                    "但 isRule / sourceRegex / 只给 html 这三种形态**本机不支持**，" +
                    "撞上时那几段结果不可信（原因见侧车 webview_unsupported）。")
        }

        ValidateService.ensureStarted()

        // A3：调试前把该源域上的 cookie 注入 App 的 cookie 通道（手工 `--cookie` 优先，
        // 否则从我们的浏览器 profile 按源 URL 读）。**拿不到也照常跑**——只是登录墙的源
        // 会以「需登录」结束。两项都进侧车：「这次带没带登录态」是排障的第一个问题。
        val cookieInj = SourceCookies.inject(
            source.bookSourceUrl.orEmpty(), cookie, BrowserSession.get().first)

        System.err.println(
            "[appservice] 调试 source=${source.bookSourceName}(${source.bookSourceUrl}) " +
                "key=$key timeout=${timeoutSec}s out=$outPath " +
                "cookie=${if (cookieInj.len > 0) "${cookieInj.len} 字符" else "无"}" +
                (if (cookieInj.note.isNotEmpty()) "（${cookieInj.note}）" else ""))

        val writer: BufferedWriter = File(outPath).bufferedWriter(Charsets.UTF_8)
        // 计数器是**跨线程**的：收集在后台线程、收尾判定在主线程。
        // 用 Atomic* 而不是 var——这是实测踩出来的：见下面驱动主 looper 的那段。
        val count = java.util.concurrent.atomic.AtomicInteger(0)   // 落盘事件数（不含 payload）
        val dropped = java.util.concurrent.atomic.AtomicInteger(0) // payload 条数（只报数）
        val sawTerminal = java.util.concurrent.atomic.AtomicBoolean(false)
        val timedOut = java.util.concurrent.atomic.AtomicBoolean(false)
        val failure = java.util.concurrent.atomic.AtomicReference("")

        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val session = Debug.startDebug(scope, source, key)

        // ---------------------------------------------------------------- 为什么要驱动主 looper
        //
        // **实测（2026-09-20）**：不驱动的话，5 条源全都只收到 `⇒开始搜索关键字` +
        // `︾开始解析搜索页` 两条（那两条是 `startDebug` 同步打的），之后就一条都没有、
        // 直到超时——看着像「站点全挂了」，其实是我们的环境不对。
        //
        // 机制（认符号，别记行号）：`WebBook.searchBook` 的签名是
        // `executeContext: CoroutineContext = Dispatchers.Main`，而 `Coroutine.executeInternal`
        // 起协程用的是 `(scope.plus(executeContext)).launch{}`——**整条协程的 dispatcher
        // 就是 Main**（不只是回调）。Robolectric 主 looper 默认 `LooperMode.PAUSED`：
        // 投进去的任务不自动执行；而 `runBlocking` 又正堵在主线程（Robolectric 里测试
        // 线程**就是**主线程）→ 协程连启动都启动不了，死锁。
        //
        // 所以：**收集放后台线程，主线程负责 idle 主 looper**。这与 `Debug` 管线
        // 只能照抄、不能重实现（那是 App 的方言）是同一件事的两面——环境得我们补。
        val done = java.util.concurrent.atomic.AtomicBoolean(false)
        // 命中回填要抓哪些页面：事件里的 `≡获取成功:<URL>`，**按出现顺序、去重**
        // （详情页与目录页常常是同一个 URL，去重后只抓一次）。
        val urls = java.util.Collections.synchronizedSet(linkedSetOf<String>())
        val collector = Thread {
            runBlocking {
                try {
                    withTimeout(timeoutSec * 1000) {
                        session.events.collect { event ->
                            if (event.kind.isSourcePayload) {
                                dropped.incrementAndGet()
                                return@collect
                            }
                            if (event.kind.isTerminal) sawTerminal.set(true)
                            // 收集在 payload 过滤**之后**：那一类事件的 text 是整页 HTML，
                            // 页面正文里出现「≡获取成功」这四个字是完全可能的
                            gotUrlOf(event.message)?.let { urls.add(it) }
                            // 逐行 flush：崩了/超时也要留得下已经收到的那部分
                            synchronized(writer) {
                                writer.write(encodeLine(event))
                                // **显式 \n，不用 newLine()**：后者取平台行尾，Windows 上是
                                // CRLF——于是同一份事件流在两个平台上字节不同，fixture
                                // 对不上、对拍也要额外归一。NDJSON 就是 LF。
                                writer.write("\n")
                                writer.flush()
                            }
                            count.incrementAndGet()
                        }
                    }
                } catch (e: TimeoutCancellationException) {
                    timedOut.set(true)
                } catch (e: Throwable) {
                    failure.set("${e::class.simpleName}: ${e.message?.take(200)}")
                }
            }
            done.set(true)
        }
        collector.isDaemon = true
        collector.name = "a1-event-collector"
        collector.start()

        // 事件流的上限就是 `--timeout`，再加 5s 是它的收尾余量
        driveMainLooperUntil(done, System.currentTimeMillis() + timeoutSec * 1000 + 5_000)
        // 收集线程可能还在等 withTimeout 收尾（最多再等 2s），别把它抛在后面
        runCatching { collector.join(2_000) }

        try { writer.close() } catch (_: Exception) {}
        // 超时/异常后把 App 那边的活停掉：不收尾的话协程会挂在测试 JVM 里，
        // 拖慢 Gradle 退出（甚至让 fork 超时）
        if (timedOut.get() || !sawTerminal.get()) {
            runCatching { session.cancel() }
        }
        scope.cancel()
        // **请求之间要把 `Debug` 的静态态清干净**：`debugSource` 只在 `cancel` 与下一次
        // `replaceSession` 时才变，而 `Debug.log` 的判据正是 `debugSource == sourceUrl`
        // ——常驻时同一条源再跑，上一轮遗留的协程会把事件打进新一轮的事件流。
        // 一次性进程不需要（进程都没了），常驻需要（TODO §一点八「请求间清状态」）。
        // 无条件调用：`cancel` 内部按 session id 判重，重复/已取消都是 no-op。
        runCatching { session.cancel() }

        // ---------------------------------------------------------------- 命中回填（第三期）
        //
        // 事件流跑完之后**自己再抓一遍**：用 App 自己的客户端（cookie / UA / 代理都还是
        // 这一次的那一套）与 App 自己的解析器，把 URL 上「规则选中的 DOM」带回来。它是
        // 侧车里的 `matched_html`，**不进 NDJSON**——那份流要与设备 WS 逐事件同构。
        //
        // 放在这里而不是塞进事件流期间：`Debug.log` 只在 `debugSource == sourceUrl` 时
        // emit，我们另发的请求不带那个会话；而收尾的 `session.cancel()` 已经跑过，App
        // 那边的协程不会与这一段抢浏览器和 cookie 通道。
        //
        // 浏览器收掉之前做：URL 带 webView 选项时要用它，此时还热着（省一次 0.65s 的
        // 启动）；收尾那段的取舍见下面。
        val matchFails = mutableListOf<String>()
        val matchSkipped = java.util.concurrent.atomic.AtomicInteger(0)
        val matched = if (urls.isEmpty()) emptyMap()
        else runMatched(source, urls.toList(), matchSkipped, matchFails)

        // 浏览器进程要收掉：A2 起它可能被 shadow 拉起过。**常驻也收**（实测取舍见下）：
        // 留着能省 0.65s/次（实测 0.7s → 0.05s），但它会**一直占着浏览器 profile**
        // ——另一个 JVM（跑批、一次性调试）撞上占用就「自愈」换临时 profile，
        // **cookie 静默全丢**（实测：同一条源从 3 段变 1 段，还白等 18s 超时）。
        // 那种错长得像「源坏了」（AGENTS #4），拿 0.65s 换掉它是划算的。
        // 真要留着，正确做法是「谁要用谁先让 daemon 交出来」（还没做，见 TODO §一点八），
        // 而不是默认占着。
        runCatching { BrowserSession.close() }

        // 退出码：**「流关了但没有终止事件」也算失败**。原来只看 count/timedOut，
        // 于是「收到两条就断了」会返回 0——正好是这一批要消灭的那种静默截断。
        val n = count.get()
        val code = when {
            n == 0 -> ZERO_EVENT
            failure.get().isNotEmpty() -> BAD_INPUT      // 进程内异常：什么都没跑完
            timedOut.get() -> TIMEOUT
            !sawTerminal.get() -> TRUNCATED
            else -> OK
        }

        // 诊断落**侧车文件**，别只打 stderr：Gradle 默认**只在测试失败时**回显被测进程的
        // stdout/stderr，通过时全吞掉——调用方于是既看不到零事件的原因，也看不到
        // webView 警告。侧车让 A4 不依赖 Gradle 的日志开关就能拿到结构化结论。
        writeMeta(outPath + ".meta.json", mapOf(
            "code" to code,
            "code_text" to codeText(code),
            "events" to n,
            "dropped_payload" to dropped.get(),
            "saw_terminal" to sawTerminal.get(),
            "timed_out" to timedOut.get(),
            "webview_unsupported" to webviewSeen,
            // A3：这次带没带登录态（0 = 没带：该域没登录过，或浏览器不可用）
            "cookie_len" to cookieInj.len,
            "cookie_note" to cookieInj.note,
            "shadow_cookie_len" to ShadowBackstageWebView.lastCookieLen.get(),
            "shadow_cookie_note" to ShadowBackstageWebView.lastCookieNote.get(),
            // shadow 是否真的被走到（S5-A2 的落地标志）。普通源必须是 0：
            // 不是 0 就说明 shadow 挂宽了，会把「真实现的结果」换成我们的桩。
            "shadow_webview_calls" to ShadowBackstageWebView.calls.get(),
            "shadow_rendered" to ShadowBackstageWebView.rendered.get(),
            "shadow_last_url" to ShadowBackstageWebView.lastUrl.get(),
            "shadow_last_js_len" to ShadowBackstageWebView.lastJsLen.get(),
            "shadow_last_is_rule" to ShadowBackstageWebView.lastIsRule.get(),
            "shadow_last_render_ms" to ShadowBackstageWebView.lastRenderMs.get(),
            "shadow_last_reason" to ShadowBackstageWebView.lastReason.get(),
            "browser_cleanup" to BrowserBridge.lastCleanupNote,
            "timeout_sec" to timeoutSec,
            "key" to key,
            "source_name" to (source.bookSourceName ?: ""),
            "source_url" to (source.bookSourceUrl ?: ""),
            // 命中回填（形状闸门在 Python 侧：`core/app_debug.matched_map`）。
            // 四个计数是给排障的人看的：`urls` 是事件里出现过的 URL 数，`hits` 是真正
            // 记下来的（URL, 段名）对数——两个数差太远说明规则没命中或页面取不到，
            // 原因在 `matched_fail` 里。
            "matched_html" to matched,
            "matched_urls" to urls.size,
            "matched_hits" to matched.values.sumOf { it.size },
            "matched_skipped" to matchSkipped.get(),
            "matched_fail" to matchFails,
            "error" to failure.get(),
            "hint" to if (code == ZERO_EVENT) ZERO_EVENT_HINT else "",
        ))

        System.err.println(
            "[appservice] 调试结束：事件 $n 条（payload ${dropped.get()} 条按 App 的口径丢弃），" +
                "终止事件=${if (sawTerminal.get()) "有" else "无"}，超时=${timedOut.get()}，退出码=$code")
        System.err.println(
            "[appservice] 命中回填：${urls.size} 个 URL 里记下 ${matched.values.sumOf { it.size }} 段命中" +
                (if (matchSkipped.get() > 0) "（预算用尽，跳过 ${matchSkipped.get()} 个）" else "") +
                (if (matchFails.isNotEmpty()) "；没记成的：${matchFails.joinToString(" / ")}" else ""))
        return code
    }

    /**
     * 在本线程驱动主 looper，直到 [done] 置位或到了 [deadline]（毫秒时间戳）。
     *
     * 为什么必须驱动：见 [runOnce] 里收集事件那段（实测记录在那儿）。收集事件与命中
     * 回填都走它——两段的后台线程都会等 App 往 Main 上投的任务。
     */
    private fun driveMainLooperUntil(
        done: java.util.concurrent.atomic.AtomicBoolean,
        deadline: Long,
    ) {
        while (!done.get() && System.currentTimeMillis() < deadline) {
            // idle() 把主 looper 上已排队的任务**就地**跑掉；循环 + 小睡是因为
            // App 的协程会一批批往 Main 上投，idle 一次不够
            runCatching {
                org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
            }
            Thread.sleep(5)
        }
    }

    // ---------------------------------------------------------------- 命中回填

    /** 事件里的 `≡获取成功:<URL>`：App 每取到一页就打一条（列表 / 详情 / 目录 / 正文
     *  四处各一处）。URL 连 `,{...}` 选项都还在，正好原样交给 `AnalyzeUrl`（它认选项）。 */
    private val GOT_URL_RE = Regex("≡获取成功[:：](.+)$")

    private fun gotUrlOf(text: String): String? =
        GOT_URL_RE.find(text)?.groupValues?.get(1)?.trim()?.takeIf { it.isNotEmpty() }

    /**
     * 能记命中 DOM 的段名（= `core/app_debug.SEGMENT_NAMES` 的取值；**没有 `bookUrl`**：
     * 详情段是 `ruleBookInfo.*` 那一族字段规则，不是一条列表/正文规则，本批不覆盖）。
     *
     * 段名是 **Python 那边的词汇**——跨语言没法共享代码，逐词比对钉在
     * `tests/test_jvm_debug_contract.py::TestMatchedStepNameParity`（加一个名字就要加
     * [matchedRuleOf] 的分支，否则这个名字永远记不出东西来）。
     */
    private val MATCHED_STEP_NAMES = listOf("search", "explore", "toc", "content")

    /**
     * 段名 → 该段的取值规则。**这里只按「哪条规则命中」命名，不解析事件流的分段**
     * （分段语义只有 Python 那一份）。
     *
     * `search` 与 `explore` 共用 `bookList`：App 里搜索页与发现页走的是同一个解析器。
     * 本侧认不出手里这个 URL 是哪一种，所以两个名字都记；Python 按**每段自己的 url +
     * 段名**取，键里本来就带 url，串不了。
     */
    private fun matchedRuleOf(source: BookSource, step: String): String = when (step) {
        "search", "explore" -> source.ruleSearch?.bookList.orEmpty()
        "toc" -> source.getTocRule().chapterList.orEmpty()
        "content" -> source.getContentRule().content.orEmpty()
        else -> ""
    }

    /**
     * 跑命中回填：**后台线程 + 本线程驱动主 looper**（同收集事件那段），预算用尽就停。
     *
     * 失败一律吞成 `fails` 里的一行：它是**附加证据**，取不到只该让「命中源码」那块空着，
     * 不能把已经跑完的调试结果带走（同「补抓页面失败不动判定」那条纪律）。
     */
    private fun runMatched(
        source: BookSource,
        urls: List<String>,
        skipped: java.util.concurrent.atomic.AtomicInteger,
        fails: MutableList<String>,
    ): Map<String, Map<String, String>> {
        val done = java.util.concurrent.atomic.AtomicBoolean(false)
        val out = java.util.concurrent.atomic.AtomicReference<Map<String, Map<String, String>>>(emptyMap())
        val worker = Thread {
            runBlocking {
                try {
                    out.set(collectMatched(source, urls, skipped, fails))
                } catch (e: Throwable) {
                    fails.add("整段失败 — ${e::class.simpleName}: ${e.message?.take(120)}")
                }
            }
            done.set(true)
        }
        worker.isDaemon = true
        worker.name = "matched-html"
        worker.start()
        // 预算 + 收尾余量：worker 到点该自己停了，这里多给一点是让它的 finally 跑完
        driveMainLooperUntil(done, System.currentTimeMillis() + MATCH_BUDGET_MS + 5_000)
        runCatching { worker.join(2_000) }
        return out.get()
    }

    /**
     * 逐个 URL：取一次页面（**每个 URL 只取一次**，详情页与目录页常常同 URL），在上面跑
     * 该源每条取值规则，命中就记 `out[url][段名]`。返回 `{url: {段名: 命中 DOM}}`。
     */
    private suspend fun collectMatched(
        source: BookSource,
        urls: List<String>,
        skipped: java.util.concurrent.atomic.AtomicInteger,
        fails: MutableList<String>,
    ): Map<String, Map<String, String>> {
        val rules = MATCHED_STEP_NAMES.map { it to matchedRuleOf(source, it) }
            .filter { (_, rule) -> rule.isNotBlank() }
        if (rules.isEmpty()) return emptyMap()
        val out = LinkedHashMap<String, Map<String, String>>()
        val deadline = System.currentTimeMillis() + MATCH_BUDGET_MS
        for (url in urls) {
            val left = deadline - System.currentTimeMillis()
            if (left <= 0) {
                skipped.incrementAndGet()
                continue
            }
            try {
                val body = withTimeout(minOf(left, MATCH_FETCH_TIMEOUT_MS)) {
                    fetchForMatch(source, url)
                }
                if (body.isBlank()) {
                    noteFail(fails, url, "取到的页面是空的")
                    continue
                }
                val perStep = LinkedHashMap<String, String>()
                for ((step, rule) in rules) {
                    try {
                        hitOf(source, body, url, rule)?.let { perStep[step] = it }
                    } catch (e: Throwable) {
                        // 规则本身跑不动（不支持的选择器 / `@js:` 抛错…）：只影响这一段
                        noteFail(fails, url, "$step 段规则没跑成（${e::class.simpleName}: " +
                            "${e.message?.take(80)}）")
                    }
                }
                if (perStep.isNotEmpty()) out[url] = perStep
            } catch (e: Throwable) {
                noteFail(fails, url, "${e::class.simpleName}: ${e.message?.take(100)}")
            }
        }
        return out
    }

    /** 用 App 自己的客户端取这一页：cookie / UA / 代理都是这次调试的同一套。
     *  非 200 直接抛（带状态码）——由调用方记进 `matched_fail`。 */
    private suspend fun fetchForMatch(source: BookSource, url: String): String {
        val analyzeUrl = AnalyzeUrl(
            mUrl = url, baseUrl = source.bookSourceUrl.orEmpty(),
            source = source, ruleData = RuleData())
        val res = analyzeUrl.getStrResponseAwait()
        if (res.code() != 200) throw IllegalStateException("HTTP ${res.code()}")
        return res.body.orEmpty()
    }

    /** 上游 `getResultLast` 的 `when` 里那几个**只当取值动作**的末段（其余末段取属性）。 */
    internal val VALUE_ACTION_TAILS = listOf("text", "textNodes", "ownText", "html", "all")

    /**
     * 取值规则的**元素部分**：末段是取值动作时把它去掉（`#nr1@p@html` → `#nr1@p`）。
     *
     * 为什么必须去掉：`getElements` 把**每个** `@` 段都当选择器（上游
     * `AnalyzeByJSoup.getElements`），而取值规则的末段是**动作或属性名**——两边语义
     * 不同（上游 `getResultList` → `getResultLast`），所以这类规则用 `getElements`
     * 直接跑恒取空。去掉末段取出来的，正是 App 取值时选中的那些节点。
     *
     * **只认那五个动作词**，末段是属性名时返回 null：属性名与标签名同形（`title` /
     * `style`），猜不了（AGENTS #21 那一类）。返回 null = 这条规则不适用这个兜底。
     */
    internal fun elementsPartOf(rule: String): String? {
        // `##替换` 不是元素部分（上游 SourceRule.splitRegex 就是先切它的）
        val body = rule.substringBefore("##").trim()
        val idx = body.lastIndexOf('@')
        if (idx <= 0) return null
        if (body.substring(idx + 1).trim() !in VALUE_ACTION_TAILS) return null
        return body.substring(0, idx).trim().takeIf { it.isNotEmpty() }
    }

    /**
     * 在这份页面上跑一条取值规则，返回命中节点的 HTML（没命中返回 null）。
     *
     * 末段是取值动作的规则（[elementsPartOf]）**直接用元素部分**，不先跑原样那次：
     * 那次必然拿不到东西（末段被当成选择器），而带 `##替换` 的形态实测还会让 jsoup
     * 收到空选择器、抛 `SelectorParseException`——先跑一次等于给失败留个位置。
     */
    private suspend fun hitOf(source: BookSource, body: String, url: String, rule: String): String? =
        htmlOn(source, body, url, elementsPartOf(rule) ?: rule)

    /**
     * 按一条规则取元素，返回命中节点的 HTML（没命中返回 null）。
     *
     * 取前 [MATCHED_NODES_LIMIT] 个拼起来、不注入换行：与本地投影
     * （`core/rules/replayer.py`）同一口径——列表规则下只看第一条看不出「混进了
     * 导航栏」这类问题。JSON 规则下节点不是 Element，退化成 `toString()`。
     */
    private suspend fun htmlOn(source: BookSource, body: String, url: String, rule: String): String? {
        val analyzeRule = AnalyzeRule(RuleData(), source)
        analyzeRule.setContent(body, url)
        // `@js:` 里的 `java.ajax` 要协程上下文；不设它 Rhino 那边取不到
        analyzeRule.setCoroutineContext(coroutineContext)
        val html = analyzeRule.getElements(rule).take(MATCHED_NODES_LIMIT).joinToString("") { node ->
            (node as? Element)?.outerHtml() ?: node.toString()
        }
        return html.takeIf { it.isNotBlank() }
    }

    /** 失败原因留一行（最多 5 条）：侧车是给排障的人看的，不刷屏。 */
    private fun noteFail(fails: MutableList<String>, url: String, reason: String) {
        if (fails.size < 5) fails.add("$url — $reason")
    }

    private fun codeText(code: Int): String = when (code) {
        OK -> "正常"
        ZERO_EVENT -> "零事件（一条都没收到）"
        TIMEOUT -> "超时（已收到的是部分结果）"
        BAD_INPUT -> "入参/输入错误或进程内异常"
        TRUNCATED -> "事件流被截断（没等到终止事件）"
        else -> "未知"
    }

    /** 侧车诊断。写不出来也不能影响主流程——它只是给调用方的额外情报。 */
    private fun writeMeta(path: String, data: Map<String, Any?>) {
        runCatching {
            // 编码器只此一份（ServiceJson）：原来这里与跑批那边各写一个 when，
            // 两边对 null / List 的处理**不一样**——那种漂移不报错，只在下游按
            // 类型读它的时候才现形（跑批那边就是这么把 null 写成字符串 "null" 的）
            val obj = ServiceJson.toJsonObject(data)
            File(path).writeText(
                kotlinx.serialization.json.Json.encodeToString(
                    kotlinx.serialization.json.JsonObject.serializer(), obj),
                Charsets.UTF_8)
        }
    }

    /**
     * 一条事件 → 一行 JSON。
     *
     * 形状 `{"kind","elapsed_ms","text"}`：`text` 就是 `Event.message`，
     * **里面已经带了 `[mm:ss.SSS]` 前缀**——那是 `Debug.log` 自己加的
     * （`debugTimeFormat`），我们**不要再包一层**，否则 `_PREFIX_RE` 只剥掉一层、
     * 另一层会留在正文里，`_split_segments` 的段首匹配就全失效了。
     *
     * `kind` / `elapsed_ms` 目前 Python 侧用不到（`_event_text` 只读 `text`），
     * 留着是给第三期（用 `kind` 替代正则分段）和核对时间轴用的——**别因为
     * 「没人读」就把它们删掉**，它们不是装饰，是将来换判据时的现成材料。
     */
    private fun encodeLine(event: Debug.Event): String =
        kotlinx.serialization.json.Json.encodeToString(
            kotlinx.serialization.json.JsonObject.serializer(),
            kotlinx.serialization.json.JsonObject(
                mapOf(
                    "kind" to kotlinx.serialization.json.JsonPrimitive(event.kind.name),
                    "elapsed_ms" to kotlinx.serialization.json.JsonPrimitive(event.elapsedMillis),
                    "text" to kotlinx.serialization.json.JsonPrimitive(event.message),
                )
            ),
        )
}

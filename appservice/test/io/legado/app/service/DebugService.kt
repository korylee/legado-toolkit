package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.Debug
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
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
 *    「看规则命中了什么」是第三期的活（shadow/OkHttp 环形缓冲回填），不是这里。
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
 *   是否超时、webView 警告、错误原文）。存在的理由：Gradle 默认**只在测试失败时**
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

        val deadline = System.currentTimeMillis() + timeoutSec * 1000 + 5_000
        while (!done.get() && System.currentTimeMillis() < deadline) {
            // idle() 把主 looper 上已排队的任务就地跑掉（它会在**当前线程**执行）。
            // 循环 + 小睡：App 的协程会一批批往 Main 上投任务，idle 一次不够
            runCatching {
                org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
            }
            Thread.sleep(5)
        }
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
            "error" to failure.get(),
            "hint" to if (code == ZERO_EVENT) ZERO_EVENT_HINT else "",
        ))

        System.err.println(
            "[appservice] 调试结束：事件 $n 条（payload ${dropped.get()} 条按 App 的口径丢弃），" +
                "终止事件=${if (sawTerminal.get()) "有" else "无"}，超时=${timedOut.get()}，退出码=$code")
        return code
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

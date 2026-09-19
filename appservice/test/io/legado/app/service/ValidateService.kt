package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.data.entities.SearchBook
import io.legado.app.domain.gateway.BookExportSettingsGateway
import io.legado.app.domain.gateway.DownloadCacheSettingsGateway
import io.legado.app.domain.gateway.ImportBookSettingsGateway
import io.legado.app.domain.gateway.MangaSettingsGateway
import io.legado.app.domain.gateway.OtherSettingsGateway
import io.legado.app.domain.gateway.ReadSettingsGateway
import io.legado.app.domain.gateway.ThemeSettingsGateway
import io.legado.app.domain.model.settings.BookExportSettings
import io.legado.app.domain.model.settings.DownloadCacheSettings
import io.legado.app.domain.model.settings.ImportBookSettings
import io.legado.app.domain.model.settings.MangaSettings
import io.legado.app.domain.model.settings.OtherSettings
import io.legado.app.domain.model.settings.ReadSettings
import io.legado.app.domain.model.settings.ThemeSettings
import io.legado.app.model.webBook.WebBook
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.koin.core.context.startKoin
import org.koin.core.context.stopKoin
import org.koin.dsl.module
import org.robolectric.RuntimeEnvironment
import splitties.init.injectAsAppCtx
import java.io.File

/**
 * JVM 校验服务的核心（S1：搜索档；S3-1：目录段）。
 *
 * 输入：书源 JSON（单个文件 / 目录下批量 / stdin）。
 * 输出：每源一行 JSON 结论（NDJSON），供 Python 侧读回写 `checks`。
 *
 * **判定口径**（六态，与 S1 一致；段内失败不新造状态）：
 * - `ok`             : 该深度的每一段都真实跑通（哪怕剥掉 webView 选项后才跑通——
 *                      剥选项是默认行为，见 [stripWebView]；结论带
 *                      `webview_stripped=true` 说明它是这么过的）
 * - `empty_js_shell` : 请求成功但结果为空 + 页面疑似 JS 壳 → **unknown 语义**（工具跑不了，
 *                      不是源坏了——「剥掉 webView 后页面要 JS 渲染」属于这条）
 * - `no_result`      : 请求成功、结果为空、页面不是 JS 壳 → 源可能真失效（留给多词复核）
 * - `timeout`        : 该段超出**每源总预算**（各段共享 `--timeout`，不是每段一份）
 * - `error`          : 请求失败（网络/异常），带原因
 * - `invalid`        : 源 JSON 解析失败
 *
 * **`stage` 字段**：结论挂在哪一段（`search` / `toc` / `content`）。多段链路里
 * 「no_result 是搜索没结果还是目录空了」必须能分开，否则用户不知道该修哪段规则。
 * 失败时 `stage` 指**出错的那一段**，`ok` 时指**跑到的最深一段**。
 *
 * 并发：源之间用协程并发（`--concurrency`），单源内仍走 App 自己的限速。
 * **不落盘**：任何一段都不写 App 的数据库/缓存（`needSave=false` 那条纪律）。
 */
object ValidateService {

    /**
     * 设置网关的通用桩：这些网关的数据类**全是带默认值的纯数据类**（UA / 线程数 /
     * 界面偏好），构造它们不碰 Android，也不需要读用户配置——校验只需要「有个能用的值」。
     *
     * 接口形状一致（currentSettings / settings Flow / update），所以一个泛型基类
     * 就能覆盖全部：`class X : SomeGateway, StubSettings<SomeSettings>(SomeSettings())`。
     */
    private open class StubSettings<T>(val value: T) {
        val currentSettings: T get() = value
        val settings: Flow<T> get() = flowOf(value)
        suspend fun update(transform: (T) -> T) = Unit
    }

    /**
     * 各网关的桩。**一次列全，不要逐个撞**：App 里凡是 `GlobalContext.get().get<X>()`
     * 的地方，缺定义就整段抛 `NoDefinitionFoundException`——目录段实测栽在
     * `AppLog`（写日志要 `OtherSettingsGateway`）上，12 条里 7 条因此报错。
     * 这些网关都在解析链路（AnalyzeUrl / BookChapterList / BookContent / BookExtensions）
     * 可达范围内，预先桩掉比按需补便宜。
     */
    private class StubCacheGateway(ua: String)
        : DownloadCacheSettingsGateway, StubSettings<DownloadCacheSettings>(
            DownloadCacheSettings(userAgent = ua))

    private class StubOtherGateway
        : OtherSettingsGateway, StubSettings<OtherSettings>(OtherSettings())

    private class StubReadGateway
        : ReadSettingsGateway, StubSettings<ReadSettings>(ReadSettings())

    private class StubImportBookGateway
        : ImportBookSettingsGateway, StubSettings<ImportBookSettings>(ImportBookSettings())

    private class StubBookExportGateway
        : BookExportSettingsGateway, StubSettings<BookExportSettings>(BookExportSettings())

    private class StubMangaGateway
        : MangaSettingsGateway, StubSettings<MangaSettings>(MangaSettings())

    private class StubThemeGateway
        : ThemeSettingsGateway, StubSettings<ThemeSettings>(ThemeSettings())

    private var started = false

    /** 进程级初始化（幂等）：Koin + appCtx。由 [main] 在跑批前调用一次。 */
    fun ensureStarted(userAgent: String) {
        if (started) return
        RuntimeEnvironment.getApplication().injectAsAppCtx()
        // AppConst.<clinit> 会读 APK 签名（判断官方包）；Robolectric 应用默认没有，
        // NPE 藏在 ExceptionInInitializerError 里（实测 17k 源）。用 Robolectric 的
        // 标准 API 装一个带签名的包信息。
        RuntimeEnvironment.getApplication().let { ctx ->
            val info = android.content.pm.PackageInfo().apply {
                packageName = ctx.packageName
                signatures = arrayOf(android.content.pm.Signature("appservice-stub".toByteArray()))
            }
            org.robolectric.shadows.ShadowPackageManager().installPackage(info)
        }
        startKoin {
            modules(module {
                single<DownloadCacheSettingsGateway> { StubCacheGateway(userAgent) }
                single<OtherSettingsGateway> { StubOtherGateway() }
                single<ReadSettingsGateway> { StubReadGateway() }
                single<ImportBookSettingsGateway> { StubImportBookGateway() }
                single<BookExportSettingsGateway> { StubBookExportGateway() }
                single<MangaSettingsGateway> { StubMangaGateway() }
                single<ThemeSettingsGateway> { StubThemeGateway() }
            })
        }
        // OkHttp 的 PublicSuffixDatabase 静态初始化要 applicationContext
        // （实测 66书吧：Unable to load PublicSuffixDatabase.list），先显式初始化
        okhttp3.OkHttp.initialize(RuntimeEnvironment.getApplication())
        started = true
    }

    fun shutdown() {
        if (started) {
            stopKoin()
            started = false
        }
    }

    /** JS 壳页特征（与 TODO §2.1 的 (a2) 一致：拿到页面但规则跑空时的判别依据）。 */
    private val JS_SHELL_MARKERS = listOf(
        "enable javascript", "enable javascript and cookies", "请开启javascript",
        "请开启 javascript", "需要开启javascript", "<noscript",
        "window.location.href=\"/", "just a moment", "cf-browser-verification",
        "challenge-platform", "_cf_chl_", "ddos-guard", "checking your browser",
    )

    private val WEBVIEW_IN_OPTION = Regex(
        """("webView"\s*:\s*)(true|1)(?=\s*[,}])""", RegexOption.IGNORE_CASE
    )

    //: 探测深度。**只增不减、编号含义稳定**——结论里记的是实际跑到的深度，
    //: 含义一平移，历史结论整列都变意思（与 settings_store.PROBE_DEPTHS 同一条纪律）。
    const val DEPTH_SEARCH = "search"
    const val DEPTH_TOC = "toc"
    const val DEPTH_CONTENT = "content"

    //: 目录「完整」的**绝对下限**（按书源类型）。
    //:
    //: ⚠️ 与本地回放**不是同一把尺**：那边是**比例**（TOC_COMPLETE_THRESHOLD：
    //: 小说 ≥80% / 漫画 ≥60% 的参考章节数），因为它手里有 TEST_TITLES 参考表；
    //: JVM 拿到的是真实目录，没有「应有多少章」的参照，只能给绝对下限——
    //: 这里的 true 只说明「目录有条像样的条数」，不代表与真实章节数吻合。
    //: 比对两侧矛盾率时必须按这个差异解读。
    private val TOC_MIN_CHAPTERS = mapOf(0 to 10, 2 to 5)

    /**
     * 结构化剥除：把 `url,{json}` 拆开，从选项里删掉 webView 键，再拼回去。
     * 对齐 App 的 paramPattern 切法：**第一个 `,{` 之后**是选项 JSON。
     */
    fun stripWebView(urlRule: String): Pair<String, Boolean> {
        val idx = urlRule.indexOf(",{")
        if (idx < 0) return urlRule to false
        val head = urlRule.substring(0, idx)
        val jsonPart = urlRule.substring(idx + 1)
        if (!jsonPart.contains("webView", ignoreCase = true)) return urlRule to false
        return try {
            val map = linkedMapOf<String, kotlinx.serialization.json.JsonElement>()
            val parser = kotlinx.serialization.json.Json.parseToJsonElement(jsonPart)
            val obj = parser as? kotlinx.serialization.json.JsonObject
                ?: return urlRule to false
            for ((k, v) in obj) {
                if (k.equals("webView", ignoreCase = true) &&
                    (v is kotlinx.serialization.json.JsonPrimitive) &&
                    (v.content == "true" || v.content == "1")
                ) continue
                map[k] = v
            }
            val rebuilt = if (map.isEmpty()) head
            else head + "," + kotlinx.serialization.json.JsonObject(map).toString()
            rebuilt to true
        } catch (e: Exception) {
            // JSON 解析不了（可能带模板/函数）→ 正则剥布尔开关，保底
            val out = WEBVIEW_IN_OPTION.replace(jsonPart) { m: kotlin.text.MatchResult -> m.groupValues[1] + "false" }
            (head + "," + out) to true
        }
    }

    /** 对一个书源跑到指定深度，返回结论行。 */
    fun validateOne(
        sourceJson: String,
        keyword: String,
        timeoutSec: Long = 30,
        stripWebView: Boolean = true,
        depth: String = DEPTH_SEARCH,
    ): Map<String, Any?> {
        val source: BookSource = try {
            GSON.fromJsonObject<BookSource>(sourceJson).getOrThrow()
        } catch (e: Exception) {
            return linkedMapOf(
                "url" to "", "name" to "", "state" to "invalid",
                "reason" to "JSON 解析失败: ${e.message?.take(120)}",
            )
        }

        // (a) 剥 webView 选项（searchUrl 与 exploreUrl；S1 只跑搜索段）
        var stripped = false
        var effective: BookSource = source
        if (stripWebView) {
            var any = false
            fun stripField(get: () -> String?, set: (String) -> Unit) {
                val v = get() ?: return
                if (!v.contains("webView", true)) return
                val (nv, did) = stripWebView(v)
                if (did) { set(nv); any = true }
            }
            effective = source
            stripField({ effective.searchUrl }, { effective.searchUrl = it })
            stripField({ effective.exploreUrl }, { effective.exploreUrl = it })
            stripped = any
        }

        // 源没有搜索规则：「没搜索规则」是**源的能力事实**，不是请求失败——
        // 报成 error 会让归因报告把它算进「网络/异常」，而它的下一步动作完全不同
        // （不可搜，别重试）。判据同 App：searchUrl 为空即不搜（WebBook.kt:57）
        if (effective.searchUrl.isNullOrBlank()) {
            return linkedMapOf(
                "url" to source.bookSourceUrl,
                "name" to source.bookSourceName,
                "state" to "no_result",
                "stage" to DEPTH_SEARCH,
                "reason" to "源没有搜索规则（searchUrl 为空）——不可搜，不是网络失败",
                "cost_ms" to 0,
                "webview_stripped" to stripped,
            )
        }

        val startedAt = System.currentTimeMillis()
        // **每源总预算**，不是每段一份：目录+正文比搜索慢好几倍，按段各给一份的话
        // 一个慢源能把整场拖成 O(源数 × 段数 × timeout)（lessons §五十四）
        val deadline = startedAt + timeoutSec * 1000
        fun remaining(): Long = (deadline - System.currentTimeMillis()).coerceAtLeast(1L)

        return try {
            val books = runBlocking {
                withTimeout(remaining()) {
                    WebBook.searchBookAwait(effective, keyword)
                }
            }
            val cost = System.currentTimeMillis() - startedAt
            when {
                books.isNotEmpty() -> {
                    val row = linkedMapOf<String, Any?>(
                        "url" to source.bookSourceUrl,
                        "name" to source.bookSourceName,
                        "state" to "ok",
                        "stage" to DEPTH_SEARCH,
                        "hit" to books.size,
                        "sample" to books.take(3).map { it.name },
                        "cost_ms" to cost,
                        "webview_stripped" to stripped,
                    )
                    if (depth == DEPTH_TOC || depth == DEPTH_CONTENT) {
                        // 目录段失败**不吞**：state/stage 改写成该段的结论——多段链路里
                        // 「搜索好了但目录坏了」和「搜索就坏了」的下一步动作不同
                        try {
                            val toc = runBlocking {
                                withTimeout(remaining()) { runTocStage(effective, books[0]) }
                            }
                            row.putAll(toc)
                            row["stage"] = DEPTH_TOC
                            row["cost_ms"] = System.currentTimeMillis() - startedAt
                            if ((toc["toc_count"] as? Int ?: 0) == 0) {
                                row["state"] = "no_result"
                                row["reason"] = "搜索命中但目录页没有章节（章节数 0）"
                            }
                        } catch (e: kotlinx.coroutines.TimeoutCancellationException) {
                            row["state"] = "timeout"
                            row["stage"] = DEPTH_TOC
                            row["cost_ms"] = System.currentTimeMillis() - startedAt
                            row["reason"] = "目录段超时（每源总预算 ${timeoutSec}s）"
                        } catch (e: Throwable) {
                            val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
                            row["state"] = "error"
                            row["stage"] = DEPTH_TOC
                            row["cost_ms"] = System.currentTimeMillis() - startedAt
                            row["reason"] = "${e::class.simpleName}: ${e.message?.take(160)}"
                            row["root"] = "${root::class.simpleName}: ${root.message?.take(160)}"
                            row["root_stack"] = root.stackTrace.take(6).joinToString(" | ") { f ->
                                "${f.className.substringAfterLast('.')}.${f.methodName}:${f.lineNumber}"
                            }
                        }
                    }
                    row
                }
                else -> {
                    // 结果为空 → 先记下原因，JS 壳判定需要页面——searchBookAwait 不回页面，
                    // 这里用「请求成功但空」+ 源特征（带 webView 标记）给一个保守归因：
                    // 带 webView 标记的源剥掉后跑空，最可能是「页面要 JS 渲染」→ unknown 语义
                    val searchTxt = source.searchUrl ?: ""
                    val exploreTxt = source.exploreUrl ?: ""
                    val likelyShell = searchTxt.contains("webView", true) || exploreTxt.contains("webView", true)
                    linkedMapOf(
                        "url" to source.bookSourceUrl,
                        "name" to source.bookSourceName,
                        "state" to if (likelyShell) "empty_js_shell" else "no_result",
                        "stage" to DEPTH_SEARCH,
                        "reason" to if (likelyShell)
                            "剥掉 webView 选项后搜索为空；原源声明需要 webView（页面可能要 JS 渲染）→ 本机无法验证"
                        else "搜索成功但无结果（关键词：$keyword）",
                        "cost_ms" to cost,
                        "webview_stripped" to stripped,
                    )
                }
            }
        } catch (e: kotlinx.coroutines.TimeoutCancellationException) {
            linkedMapOf(
                "url" to source.bookSourceUrl, "name" to source.bookSourceName,
                "state" to "timeout",
                "stage" to DEPTH_SEARCH,
                "reason" to "搜索超时（每源总预算 ${timeoutSec}s）",
                "webview_stripped" to stripped,
            )
        } catch (e: Throwable) {
            // ExceptionInInitializerError 的根因在 cause 里；只报 message 会把
            // 「环境缺口」和「源的问题」混成一团（实测 17k 那条就是这么藏住的）
            val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
            linkedMapOf(
                "url" to source.bookSourceUrl, "name" to source.bookSourceName,
                "state" to "error",
                "stage" to DEPTH_SEARCH,
                "reason" to "${e::class.simpleName}: ${e.message?.take(160)}",
                "root" to "${root::class.simpleName}: ${root.message?.take(160)}",
                "root_stack" to root.stackTrace.take(6).joinToString(" | ") { f ->
                    "${f.className.substringAfterLast('.')}.${f.methodName}:${f.lineNumber}" },
                "webview_stripped" to stripped,
            )
        }
    }

    /**
     * 目录段：详情页 → 目录页。返回要并进结论的字段。
     *
     * 链路形态照抄 App 自己的调试链（`Debug.kt` 的 infoDebug → tocDebug）：
     * book 由搜索第一条结果来（`SearchBook.toBook()`），`book.tocUrl` 由详情段填
     * （详情规则为空时 App 回退 bookUrl，见 BookInfo.kt:163）——**不要自己拼 tocUrl**。
     *
     * **不落盘**：`getChapterListAwait` 不写 App 的数据库/缓存（needSave 那条纪律
     * 只约束正文段，这里连缓存都不碰）。
     */
    private suspend fun runTocStage(source: BookSource, firstBook: SearchBook): Map<String, Any?> {
        val book = firstBook.toBook()
        WebBook.getBookInfoAwait(source, book)
        val res = WebBook.getChapterListAwait(source, book)
        if (res.isFailure) throw (res.exceptionOrNull() ?: RuntimeException("目录解析失败"))
        // 卷标过滤与 Debug.kt:352 同口径：`isVolume && url.startsWith(title)` 是卷
        val all = res.getOrThrow()
        val chapters = all.filterNot { it.isVolume && it.url.startsWith(it.title) }
        val count = chapters.size
        val urlsOk = chapters.firstOrNull()?.url?.isNotBlank() == true &&
            chapters.lastOrNull()?.url?.isNotBlank() == true
        val min = TOC_MIN_CHAPTERS[source.bookSourceType] ?: 1
        // `toc_sample` + `toc_raw_count`：**让「只有 2 章」这种结论能自证真伪**——
        // 分不清「站点真的只有一章」和「我们的卷标过滤/规则解析把它吃掉了」时，
        // 只报一个数字等于把工具的问题说成源的问题
        return linkedMapOf(
            "book_url" to book.bookUrl,
            "toc_url" to book.tocUrl,
            "toc_count" to count,
            "toc_raw_count" to all.size,
            "toc_sample" to chapters.take(3).map { it.title.take(40) },
            "toc_complete" to (count >= min && urlsOk),
        )
    }

    /** 批量：并发跑一批，逐条回调（流式写文件用）。 */
    fun validateBatch(
        sourceJsons: List<String>,
        keyword: String,
        concurrency: Int = 8,
        timeoutSec: Long = 30,
        stripWebView: Boolean = true,
        depth: String = DEPTH_SEARCH,
        onResult: (Map<String, Any?>) -> Unit,
    ) {
        ensureStarted("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        val results = java.util.Collections.synchronizedList(mutableListOf<Map<String, Any?>>())
        runBlocking {
            coroutineScope {
                val sem = kotlinx.coroutines.sync.Semaphore(concurrency)
                sourceJsons.map { json ->
                    async(Dispatchers.IO) {
                        sem.withPermit {
                            val r = validateOne(json, keyword, timeoutSec, stripWebView, depth)
                            results.add(r)
                            onResult(r)
                        }
                    }
                }.awaitAll()
            }
        }
    }

    private suspend fun kotlinx.coroutines.sync.Semaphore.withPermit(block: suspend () -> Unit) {
        acquire()
        try { block() } finally { release() }
    }

    // ---------------------------------------------------------------- CLI

    @JvmStatic
    fun main(args: Array<String>) {
        var dir: String? = null
        var file: String? = null
        var keyword = "我"
        var concurrency = 8
        var timeoutSec = 30L
        var outPath = ""
        var limit = 0
        var noStrip = false
        var depth = DEPTH_SEARCH
        var i = 0
        while (i < args.size) {
            when (args[i]) {
                "--dir" -> { dir = args[++i] }
                "--file" -> { file = args[++i] }
                "--keyword" -> { keyword = args[++i] }
                "--concurrency" -> { concurrency = args[++i].toInt() }
                "--timeout" -> { timeoutSec = args[++i].toLong() }
                "--out" -> { outPath = args[++i] }
                "--limit" -> { limit = args[++i].toInt() }
                "--no-strip-webview" -> { noStrip = true }
                "--depth" -> { depth = args[++i] }
                else -> { System.err.println("未知参数: ${args[i]}"); return }
            }
            i++
        }
        // 深度白名单：**未实现的值显式拒绝**，不静默降级成搜索档——
        // 静默降级会让调用方以为跑到了目录/正文段，拿到一份"看起来正常"的浅结论
        if (depth !in listOf(DEPTH_SEARCH, DEPTH_TOC)) {
            System.err.println(
                "[appservice] --depth $depth 不可用（当前支持：$DEPTH_SEARCH / $DEPTH_TOC；" +
                    "$DEPTH_CONTENT 见 S3-2）")
            return
        }
        ensureStarted("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

        // 收源：--file 单文件（JSON 数组）、--dir 目录下 *.json（Legado 导出常见形态）
        val jsons = mutableListOf<String>()
        fun collect(f: File) {
            val text = f.readText(Charsets.UTF_8).trim()
            if (!text.startsWith("[")) return
            val arr = kotlinx.serialization.json.Json.parseToJsonElement(text)
                as kotlinx.serialization.json.JsonArray
            arr.forEach { el -> jsons.add(el.toString()) }
        }
        if (file != null) collect(File(file))
        if (dir != null) {
            File(dir).listFiles { f -> f.extension.equals("json", true) }
                ?.sortedBy { it.name }
                ?.forEach { collect(it) }
        }
        if (limit > 0) jsons.subList(0, minOf(limit, jsons.size)).toList().let { jsons.clear(); jsons.addAll(it) }
        System.err.println("[appservice] 待验源: ${jsons.size} 条；关键词=$keyword 并发=$concurrency 超时=${timeoutSec}s 深度=$depth 剥webView=${!noStrip}")

        val writer = if (outPath.isNotEmpty())
            File(outPath).bufferedWriter(Charsets.UTF_8) else null
        var done = 0
        try {
            validateBatch(jsons, keyword, concurrency, timeoutSec, !noStrip, depth) { r ->
                val line = kotlinx.serialization.json.Json.encodeToString(
                    kotlinx.serialization.json.JsonObject.serializer(),
                    kotlinx.serialization.json.JsonObject(r.mapValues { v ->
                        when (val x = v.value) {
                            is String -> kotlinx.serialization.json.JsonPrimitive(x)
                            is Number -> kotlinx.serialization.json.JsonPrimitive(x)
                            is Boolean -> kotlinx.serialization.json.JsonPrimitive(x)
                            is List<*> -> kotlinx.serialization.json.JsonArray(x.map {
                                kotlinx.serialization.json.JsonPrimitive(it.toString())
                            })
                            else -> kotlinx.serialization.json.JsonPrimitive(x.toString())
                        }
                    }),
                )
                synchronized(writer ?: return@validateBatch) {
                    writer?.write(line); writer?.newLine(); writer?.flush()
                }
                done++
                if (done % 50 == 0) System.err.println("[appservice] 进度 $done/${jsons.size}")
            }
        } finally {
            writer?.close()
        }
        System.err.println("[appservice] 完成 $done/${jsons.size}")
        shutdown()
    }
}

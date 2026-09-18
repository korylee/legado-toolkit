package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.domain.gateway.DownloadCacheSettingsGateway
import io.legado.app.domain.model.settings.DownloadCacheSettings
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
 * JVM 校验服务的核心（S1：搜索档闭环）。
 *
 * 输入：书源 JSON（单个文件 / 目录下批量 / stdin）。
 * 输出：每源一行 JSON 结论（NDJSON），供 Python 侧读回写 `checks`。
 *
 * **判定口径**（与 TODO §2.1 的 S1 范围一致）：
 * - `ok`          : 搜索真实返回结果（哪怕剥掉 webView 选项后才跑通——剥选项是默认行为，
 *                   见 [stripWebView]；报告里带 `webview_stripped=true` 说明它是这么过的）
 * - `empty`       : 请求成功但结果为空 + 页面疑似 JS 壳 → **unknown 语义**（工具跑不了，
 *                   不是源坏了——「剥掉 webView 后页面要 JS 渲染」属于这条）
 * - `no_result`   : 请求成功、结果为空、页面不是 JS 壳 → 源可能真失效（留给多词复核）
 * - `error`       : 请求失败（网络/超时/异常），带原因
 *
 * 并发：源之间用协程并发（`--concurrency`），单源内仍走 App 自己的限速。
 * 每源独立超时（`--timeout` 秒），防止个别站把整场拖死。
 */
object ValidateService {

    /** AnalyzeUrl 需要的设置网关（纯数据：UA / 线程数），不碰 Android。 */
    private class StubSettingsGateway(ua: String) : DownloadCacheSettingsGateway {
        override val currentSettings = DownloadCacheSettings(userAgent = ua)
        override val settings: Flow<DownloadCacheSettings> = flowOf(currentSettings)
        override suspend fun update(transform: (DownloadCacheSettings) -> DownloadCacheSettings) = Unit
    }

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
                single<DownloadCacheSettingsGateway> { StubSettingsGateway(userAgent) }
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

    /** 对一个书源跑搜索档，返回结论行。 */
    fun validateOne(
        sourceJson: String,
        keyword: String,
        timeoutSec: Long = 30,
        stripWebView: Boolean = true,
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

        val startedAt = System.currentTimeMillis()
        return try {
            val books = runBlocking {
                withTimeout(timeoutSec * 1000) {
                    WebBook.searchBookAwait(effective, keyword)
                }
            }
            val cost = System.currentTimeMillis() - startedAt
            when {
                books.isNotEmpty() -> linkedMapOf(
                    "url" to source.bookSourceUrl,
                    "name" to source.bookSourceName,
                    "state" to "ok",
                    "hit" to books.size,
                    "sample" to books.take(3).map { it.name },
                    "cost_ms" to cost,
                    "webview_stripped" to stripped,
                )
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
                "reason" to "搜索超时（${timeoutSec}s）",
                "webview_stripped" to stripped,
            )
        } catch (e: Throwable) {
            // ExceptionInInitializerError 的根因在 cause 里；只报 message 会把
            // 「环境缺口」和「源的问题」混成一团（实测 17k 那条就是这么藏住的）
            val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
            linkedMapOf(
                "url" to source.bookSourceUrl, "name" to source.bookSourceName,
                "state" to "error",
                "reason" to "${e::class.simpleName}: ${e.message?.take(160)}",
                "root" to "${root::class.simpleName}: ${root.message?.take(160)}",
                "root_stack" to root.stackTrace.take(6).joinToString(" | ") { f ->
                    "${f.className.substringAfterLast('.')}.${f.methodName}:${f.lineNumber}" },
                "webview_stripped" to stripped,
            )
        }
    }

    /** 批量：并发跑一批，逐条回调（流式写文件用）。 */
    fun validateBatch(
        sourceJsons: List<String>,
        keyword: String,
        concurrency: Int = 8,
        timeoutSec: Long = 30,
        stripWebView: Boolean = true,
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
                            val r = validateOne(json, keyword, timeoutSec, stripWebView)
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
                else -> { System.err.println("未知参数: ${args[i]}"); return }
            }
            i++
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
        System.err.println("[appservice] 待验源: ${jsons.size} 条；关键词=$keyword 并发=$concurrency 超时=${timeoutSec}s 剥webView=${!noStrip}")

        val writer = if (outPath.isNotEmpty())
            File(outPath).bufferedWriter(Charsets.UTF_8) else null
        var done = 0
        try {
            validateBatch(jsons, keyword, concurrency, timeoutSec, !noStrip) { r ->
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

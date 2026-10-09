package io.legado.app.service

import android.webkit.WebView
import io.legado.app.data.appDb
import io.legado.app.help.CacheManager
import io.legado.app.help.http.BackstageWebView
import io.legado.app.help.http.StrResponse
import io.legado.app.help.webView.WebJsExtensions
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.annotation.RealObject
import org.robolectric.util.ReflectionHelpers
import splitties.init.appCtx
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.Continuation

/**
 * `BackstageWebView.getStrResponse()` 的 shadow（S5-A2）：把「取数那一步」换成真浏览器。
 *
 * ## 为什么是 shadow 这个方法，而不是像 S3-4 那样「渲染后另喂解析器」
 *
 * S3-4 的校验通道可以「CDP 渲染 → 喂 `BookList.analyzeBookList`」，因为**校验只要终态**。
 * **调试通道不行**：要验的正是 App 自己那条不透明的管线（`Debug` → `WebBook` →
 * `AnalyzeUrl` → 这里），重实现它等于把 App 的调试语义抄一遍（TODO §一点八 的既定结论）。
 *
 * 而 `BackstageWebView` 本来就只是**替换取数那一步**（lessons §四十九），它干两件事：
 * ① 加载页面（loadUrl / loadDataWithBaseURL），② 在渲染结果上执行 `js` 选项
 * （无则默认 `document.documentElement.outerHTML`），**非空才收，空则重试**。
 * 所以正确接法就是 shadow 掉它、把这两步委托给 [BrowserBridge]，**App 管线一行不改**。
 *
 * ## 「渲染一次拿 outerHTML 交差」是错的
 *
 * 对 `xhr_mode` 类源（页面用 XHR 把图拉成 blob、DOM 里没有图片地址）那样做会得到空页，
 * 判「源取不到」而**真机是好的**——假阴性比延期糟，因为它长得像结论。所以第 ② 步
 * （执行 `js` 选项 + 空结果重试）不是可选项，它是这类源唯一能拿到数据的路径。
 *
 * ## 本批的边界（都要**显式报不支持**，不静默给错结果）
 *
 * - `isRule = true`（`AnalyzeRule.getWebJsResult` 那种，会注入 `java`/`source` 等绑定）
 *   → 暂不支持。我们注入不了 `WebJsExtensions` 的语义，硬跑会跑出**错的**值。
 * - `sourceRegex` 非空（SnifferWebClient 嗅探路径）→ 暂不支持。
 * - `html` + `url` 的 `loadDataWithBaseURL` 形态 → 随 `isRule` 一并挡掉（调用点就是它）。
 *
 * 不支持时**抛**异常而不是返回空串：异常会顺着 `AnalyzeUrl` → `WebBook` 变成事件流里的
 * 错误行（`state = -1`），在 NDJSON 里看得见；返回空串则会被读成「这个源取不到」。
 */
@Implements(BackstageWebView::class)
class ShadowBackstageWebView {

    companion object {
        /** 被调用次数。**普通源必须是 0**——不是 0 说明 shadow 挂宽了。 */
        val calls = AtomicInteger(0)
        val lastUrl = AtomicReference("")
        val lastJsLen = AtomicInteger(0)
        val lastIsRule = AtomicBoolean(false)
        /** 真正走了浏览器的次数（排掉「显式不支持」那几种）。 */
        val rendered = AtomicInteger(0)
        /** A3：这次渲染顺手注入了多少字符的 cookie（0 = 该域没 cookie，不是失败）。 */
        val lastCookieLen = AtomicInteger(0)
        /** 注入的说明（`no_cookie_for_domain` / `cdp_error:` / `store_error:`…）。 */
        val lastCookieNote = AtomicReference("")
        val lastRenderMs = AtomicLong(0)
        val lastReason = AtomicReference("")
        /** 本次调试实际撞上的 WebView 能力边界；只记录不支持分支，不把源声明当结论。 */
        val webviewUnsupported = CopyOnWriteArrayList<Map<String, Any?>>()
        val phase = AtomicReference("debug")

        private fun recordUnsupported(code: String, url: String) {
            webviewUnsupported += mapOf(
                "code" to code,
                "url" to url,
                "phase" to phase.get(),
            )
        }

        /** 走本机 JS 引擎跑完的 isRule 次数（不渲染页面）。对照实验与测试用它区分两条路。 */
        val ruleLocalRuns = AtomicInteger(0)

        /**
         * 规则 JS 是否**需要页面环境**（document / window 之类）。
         *
         * 扫的是规则原文：`getInjectionString` 前奏是 `LoadJsRunnable` 之后才拼上去的，
         * shadow 这里拿不到，所以前奏里的东西不会混进来。
         *
         * 判据**取保守**：拿不准就算需要。宁可报「本机未覆盖 + 连 App 取证」，也不要跑出
         * 一个错的値——类注释里那条「假阴性比延期糟」对这里同样成立。
         */
        val PAGE_ENV_MARKERS = listOf(
            "document", "window", "location", "navigator", "history", "screen",
            "localStorage", "sessionStorage", "XMLHttpRequest", "fetch(",
            "querySelector", "getElementsBy", "getElementById", "createElement",
        )

        internal fun needsPageEnvironment(js: String): Boolean =
            PAGE_ENV_MARKERS.any { js.contains(it) }
        /** L4 的材料：这一页**实际发过的接口请求**（XHR / Fetch，见 `BrowserBridge.networkRequests`）。
         *  写进侧车的 `network` 键；Python 侧只认形状（形状不对整块丢掉）。 */
        @Volatile
        var lastNetwork: List<Map<String, Any?>>? = null
        /** 流过来的 `Network.*` 事件条数（0 = 域没启用 / 事件没到，与「页面没发接口」分得开）。 */
        @Volatile
        var lastNetworkEvents: Int = 0
        @Volatile
        var lastNetworkTypes: String = ""
        @Volatile
        var lastNetworkDrops: String = ""

        /** 关掉就退回真实现——做对照实验用（不做对照就证明不了差异来自 shadow）。 */
        @Volatile
        var enabled = true

        /** 与 App 一致：`EvalJsRunnable` 空结果 → 1 秒后再来，最多 30 次。 */
        const val JS_RETRY_TIMES = 30
        const val JS_RETRY_INTERVAL_MS = 1000L

        /**
         * 相对地址按源 URL（`tag`）补全，语义就是 App 自己的 `NetworkUtils.getAbsoluteURL`
         * （别再写一份拼接）；tag 拿不到就原样返回——那种地址会被桥立刻报导航失败。
         */
        internal fun resolveAgainstTag(url: String, tag: String?): String =
            io.legado.app.utils.NetworkUtils.getAbsoluteURL(tag, url)

        fun reset() {
            calls.set(0); lastUrl.set(""); lastJsLen.set(0); lastIsRule.set(false)
            rendered.set(0); lastRenderMs.set(0L); lastReason.set("")
            ruleLocalRuns.set(0)
            lastNetwork = null; lastNetworkEvents = 0; lastNetworkTypes = ""; lastNetworkDrops = ""
            lastCookieLen.set(0); lastCookieNote.set("")
            webviewUnsupported.clear()
            phase.set("debug")
        }
    }

    @RealObject
    private lateinit var real: BackstageWebView

    /**
     * **注意签名**：`getStrResponse` 在 JVM 上是 `getStrResponse(Continuation)Object`
     * ——suspend 函数的续体是**最后一个参数**、返回类型擦成 `Object`。shadow 必须照着
     * 这个描述符写：名字对上但参数不对是**不会被调用**的，而且**不报错**。
     *
     * 返回值直接给结果：调用方（编译器生成的状态机）看到返回的不是 `COROUTINE_SUSPENDED`
     * 就当作「已同步完成」当场取用。
     */
    @Implementation
    fun getStrResponse(continuation: Continuation<*>): Any {
        calls.incrementAndGet()
        val url = ReflectionHelpers.getField<String?>(real, "url") ?: ""
        val html = ReflectionHelpers.getField<String?>(real, "html") ?: ""
        val js = ReflectionHelpers.getField<String?>(real, "javaScript") ?: ""
        val isRule = ReflectionHelpers.getField<Boolean>(real, "isRule") ?: false
        val sourceRegex = ReflectionHelpers.getField<String?>(real, "sourceRegex") ?: ""
        val overrideUrlRegex = ReflectionHelpers.getField<String?>(real, "overrideUrlRegex") ?: ""
        val timeoutMs = ReflectionHelpers.getField<Long?>(real, "timeout") ?: 60_000L
        lastUrl.set(url); lastJsLen.set(js.length); lastIsRule.set(isRule)

        if (!enabled) {
            throw IllegalStateException("shadow_disabled: 对照实验把 shadow 关掉了")
        }
        // ---- 本批边界：显式报不支持（理由见类注释）----
        if (isRule) {
            // isRule 不是「一条边界」，而是「在页面里跑 App 的 webJs 规则」。其中**不需要
            // 页面环境**的那部分（只用 java/source/cache 做数据变换）没有理由跑浏览器：
            // App 自带的 Rhino 就是同一个引擎、绑定同步、语义最接近 App（`BaseSource.evalJS`）。
            // 只有真要用 document/window 的才需要页面，仍走显式不支持。
            if (js.isNotBlank() && !needsPageEnvironment(js)) {
                return try {
                    runRuleWithoutPage(url, js)
                } catch (e: Throwable) {
                    recordUnsupported("unsupported_is_rule_local", url)
                    lastReason.set("unsupported_is_rule_local")
                    throw IllegalStateException(
                        "webview_shadow_unsupported: 这条 webJs 规则本机求值失败" +
                            "（${e.javaClass.simpleName}: ${e.message?.take(120)}）" +
                            "——不是源的问题", e)
                }
            }
            recordUnsupported("unsupported_is_rule", url)
            lastReason.set("unsupported_is_rule")
            throw IllegalStateException(
                "webview_shadow_unsupported: 这条规则要用页面环境（document/window 之类），" +
                    "本机调试暂不支持——不是源的问题")
        }
        // **嗅探（`sourceRegex` / `overrideUrlRegex`）不在这里挡**：上游 `SnifferWebClient`
        // 就是在加载过程中嗅探，命中把那个地址当响应体——所以它交给桥（`sniffHit`），与
        // 「html 形态 / 导航形态」都无关。早先那版在这里直接报不支持，等于把有真实调用的源
        // （`java.webViewGetOverrideUrl`）静默变成「取整页」。
        //
        // 上游 `load()` **先看 html**：html 有就分两条——url 空走 `loadData(html, …)`（没有 baseUrl），
        // url 有走 `loadDataWithBaseURL(url, html, …, url)`（按这个地址渲染这份 html）。
        // 两条都交给桥的 `content` 入口：前者 `setDocumentContent`，后者 `Fetch.fulfillRequest`
        // （帧地址与来源都是 url，且一个真实请求都不发）。这里**不再联网去取那个地址**——
        // 早先那样做会拿另一份材料当结果（`java.webView(script, source.key, "")` 这类调用点
        // 会拿到源站首页），而界面看起来像「源取不到」。
        if (html.isNotBlank()) {
            val (session, why) = BrowserSession.get()
            if (session == null) {
                lastReason.set("browser_unavailable")
                throw IllegalStateException("browser_unavailable: $why")
            }
            val t0 = System.currentTimeMillis()
            // 注入 JS 前的等待照上游：`LoadJsRunnable` 是 `onPageFinished` 之后 1000 + delayTime
            val delayTime = ReflectionHelpers.getField<Long?>(real, "delayTime") ?: 0L
            val r = BrowserBridge.renderContent(
                session, html, baseUrl = url.takeIf { it.isNotBlank() }, timeoutMs = timeoutMs,
                waitAfterLoadMs = 1000L + delayTime, js = js.takeIf { it.isNotBlank() },
                jsRetryTimes = JS_RETRY_TIMES, jsRetryIntervalMs = JS_RETRY_INTERVAL_MS,
                sourceRegex = sourceRegex.takeIf { it.isNotBlank() },
                overrideUrlRegex = overrideUrlRegex.takeIf { it.isNotBlank() })
            lastRenderMs.set(System.currentTimeMillis() - t0)
            if (!r.ok) {
                lastReason.set(r.reason.substringBefore(':'))
                throw IllegalStateException("webview_render_failed: ${r.reason}")
            }
            rendered.incrementAndGet()
            lastReason.set("")
            // 嗅探命中：上游 `StrResponse(url!!, requestUrl)`——**响应地址是页面地址**，
            // 响应体才是那个命中的地址（不是落地地址，也没发生导航）
            if (r.sniffed) return StrResponse(url, r.body)
            return StrResponse(if (url.isNotBlank()) r.url.ifBlank { url } else "", r.body)
        }
        if (url.isBlank()) {
            lastReason.set("no_url")
            throw IllegalStateException("webview_shadow_no_url: 既没有 url 也没有 html")
        }

        // ---- 取数交给真浏览器 ----
        val (session, why) = BrowserSession.get()
        if (session == null) {
            lastReason.set("browser_unavailable")
            throw IllegalStateException("browser_unavailable: $why")
        }
        // **相对地址在导航前补全**：App 路径里 `AnalyzeUrl` 会用 baseUrl 解析，但 `--`
        // 重跑那类 baseUrl 为空、相对地址原样进桥——CDP 拒绝相对地址的 navigate，桥
        // 只等 load 事件就白等满渲染预算（实测 2026-09-26：75.5s 才 fail）。`tag` 就是
        // 源 URL，在这里补全是**一个咽喉**：重跑、正文翻页、目录分段的所有相对地址
        // 都被覆盖。判据钉在 WebViewNavigationTest；桥侧「导航失败立刻报」是第二道防线。
        val tag = ReflectionHelpers.getField<String?>(real, "tag").orEmpty()
        val urlAbs = resolveAgainstTag(url, tag)
        val t0 = System.currentTimeMillis()
        val r = BrowserBridge.renderSerial(
            session, urlAbs, timeoutMs,
            js = js.takeIf { it.isNotBlank() },
            jsRetryTimes = JS_RETRY_TIMES,
            jsRetryIntervalMs = JS_RETRY_INTERVAL_MS,
            sourceRegex = sourceRegex.takeIf { it.isNotBlank() },
            overrideUrlRegex = overrideUrlRegex.takeIf { it.isNotBlank() })
        lastRenderMs.set(System.currentTimeMillis() - t0)
        if (!r.ok) {
            lastReason.set(r.reason.substringBefore(':'))
            throw IllegalStateException("webview_render_failed: ${r.reason}")
        }
        rendered.incrementAndGet()
        lastReason.set("")
        // 嗅探命中：响应体是命中的地址本身，响应地址仍是**页面地址**（上游同款）
        if (r.sniffed) return StrResponse(url, r.body)
        lastNetwork = r.network
        lastNetworkEvents = r.networkEvents
        lastNetworkTypes = r.networkTypes
        lastNetworkDrops = r.networkDrops
        // A3：**渲染完顺手把这一页的 cookie 收进 `CookieStore`**——上游
        // `BackstageWebView.setCookie()` 就是这一步（`onPageFinished` → 取 WebView 的
        // cookie → `CookieStore.setCookie(tag, cookie)`），区别只是我们的来源是 CDP
        // 浏览器（JVM 里 WebView 是桩）。不收回来的话，后续 HTTP 请求（目录/正文段）
        // 仍然匿名——登录墙的源就会被读成「源取不到」。
        // 取 cookie 用**落地地址**（登录页常是另一台主机），存的键仍按上游用 `tag`（源 URL）。
        val inj = SourceCookies.injectFromProfile(session, tag, r.url.ifBlank { urlAbs })
        lastCookieLen.set(inj.len)
        lastCookieNote.set(inj.note)
        // url 用**落地地址**：App 的 buildStrResponse 也是拿 WebView 跳转后的地址
        return StrResponse(r.url.ifBlank { url }, r.body)
    }

    /**
     * 在 App 自己的 JS 引擎里跑 webJs 规则（**不渲染页面**）。
     *
     * 绑定形状照 `BackstageWebView` 的 WebView 路径对齐，**不是**照 `BaseSource.evalJS`
     * 的默认形状——两条路看着像，实际差两处（照抄默认绑定会跑出不一样的値）：
     *
     * - `java`：WebView 路径里是 `WebJsExtensions`（`ajax` / `connect` / `get` / `log` …），
     *   而 `evalJS` 默认把它绑成 BaseSource 自己。所以必须显式覆盖。
     * - `result`：WebView 路径里进的是 `CacheManager["webview_result"]`，不是 JS 绑定
     *   （`getInjectionString` 只把 cache / source / java 起个别名）。
     *
     * `WebJsExtensions` 只在 JS 桥回调（`window.$JSBridgeResult`）那一处用真 WebView，
     * 传 Robolectric 的桩即可；用到那条桥的规则会在求值时报错，归入「本机未覆盖」。
     *
     * 返回值**不做 unescape**：WebView 路径要 `unescapeJson` 是因为 `evaluateJavascript`
     * 给的是 JSON 转义串，Rhino 这里直接返回真値，多剥一层反而错。
     */
    private fun runRuleWithoutPage(url: String, js: String): StrResponse {
        val tag = ReflectionHelpers.getField<String?>(real, "tag").orEmpty()
        val source = appDb.bookSourceDao.getBookSource(tag)
            ?: throw IllegalStateException("拿不到这条源（tag=$tag）")
        ReflectionHelpers.getField<String?>(real, "result")
            ?.let { CacheManager.put("webview_result", it) }
        val ext = WebJsExtensions(source, null, WebView(appCtx))
        val out = source.evalJS(js) { put("java", ext) }
        ruleLocalRuns.incrementAndGet()
        lastReason.set("")
        return StrResponse(url, out?.toString().orEmpty())
    }
}

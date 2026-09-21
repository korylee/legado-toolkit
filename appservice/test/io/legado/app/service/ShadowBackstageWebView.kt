package io.legado.app.service

import io.legado.app.help.http.BackstageWebView
import io.legado.app.help.http.StrResponse
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.annotation.RealObject
import org.robolectric.util.ReflectionHelpers
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
        /** L4 的材料：这一页**实际发过的接口请求**（XHR / Fetch，见 `BrowserBridge.networkRequests`）。
         *  写进侧车的 `network` 键；Python 侧只认形状（形状不对整块丢掉）。 */
        @Volatile
        var lastNetwork: List<Map<String, Any?>>? = null
        /** 流过来的 `Network.*` 事件条数（0 = 域没启用 / 事件没到，与「页面没发接口」分得开）。 */
        @Volatile
        var lastNetworkEvents: Int = 0
        @Volatile
        var lastNetworkTypes: String = ""

        /** 关掉就退回真实现——做对照实验用（不做对照就证明不了差异来自 shadow）。 */
        @Volatile
        var enabled = true

        /** 与 App 一致：`EvalJsRunnable` 空结果 → 1 秒后再来，最多 30 次。 */
        const val JS_RETRY_TIMES = 30
        const val JS_RETRY_INTERVAL_MS = 1000L

        fun reset() {
            calls.set(0); lastUrl.set(""); lastJsLen.set(0); lastIsRule.set(false)
            rendered.set(0); lastRenderMs.set(0L); lastReason.set("")
            lastNetwork = null; lastNetworkEvents = 0; lastNetworkTypes = ""
            lastCookieLen.set(0); lastCookieNote.set("")
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
        val timeoutMs = ReflectionHelpers.getField<Long?>(real, "timeout") ?: 60_000L
        lastUrl.set(url); lastJsLen.set(js.length); lastIsRule.set(isRule)

        if (!enabled) {
            throw IllegalStateException("shadow_disabled: 对照实验把 shadow 关掉了")
        }
        // ---- 本批边界：显式报不支持（理由见类注释）----
        if (isRule) {
            lastReason.set("unsupported_is_rule")
            throw IllegalStateException(
                "webview_shadow_unsupported: 这条规则走的是 isRule 注入路径" +
                    "（需要 java/source 等绑定），本机调试暂不支持——不是源的问题")
        }
        if (sourceRegex.isNotBlank()) {
            lastReason.set("unsupported_source_regex")
            throw IllegalStateException(
                "webview_shadow_unsupported: 源声明了 sourceRegex（嗅探路径），" +
                    "本机调试暂不支持——不是源的问题")
        }
        if (html.isNotBlank() && url.isBlank()) {
            lastReason.set("unsupported_html_only")
            throw IllegalStateException(
                "webview_shadow_unsupported: 只给了 html 没给 url（loadDataWithBaseURL），" +
                    "本机调试暂不支持——不是源的问题")
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
        val t0 = System.currentTimeMillis()
        val r = BrowserBridge.renderSerial(
            session, url, timeoutMs,
            js = js.takeIf { it.isNotBlank() },
            jsRetryTimes = JS_RETRY_TIMES,
            jsRetryIntervalMs = JS_RETRY_INTERVAL_MS)
        lastRenderMs.set(System.currentTimeMillis() - t0)
        if (!r.ok) {
            lastReason.set(r.reason.substringBefore(':'))
            throw IllegalStateException("webview_render_failed: ${r.reason}")
        }
        rendered.incrementAndGet()
        lastReason.set("")
        lastNetwork = r.network
        lastNetworkEvents = r.networkEvents
        lastNetworkTypes = r.networkTypes
        // A3：**渲染完顺手把这一页的 cookie 收进 `CookieStore`**——上游
        // `BackstageWebView.setCookie()` 就是这一步（`onPageFinished` → 取 WebView 的
        // cookie → `CookieStore.setCookie(tag, cookie)`），区别只是我们的来源是 CDP
        // 浏览器（JVM 里 WebView 是桩）。不收回来的话，后续 HTTP 请求（目录/正文段）
        // 仍然匿名——登录墙的源就会被读成「源取不到」。
        // 取 cookie 用**落地地址**（登录页常是另一台主机），存的键仍按上游用 `tag`（源 URL）。
        val tag = ReflectionHelpers.getField<String?>(real, "tag").orEmpty()
        val inj = SourceCookies.injectFromProfile(session, tag, r.url.ifBlank { url })
        lastCookieLen.set(inj.len)
        lastCookieNote.set(inj.note)
        // url 用**落地地址**：App 的 buildStrResponse 也是拿 WebView 跳转后的地址
        return StrResponse(r.url.ifBlank { url }, r.body)
    }
}

package io.legado.app.service

import io.legado.app.help.http.CookieStore

/**
 * A3：把「登录态」从浏览器 profile 搬进 App 的 cookie 通道。
 *
 * ## 上游自己在设备上就是这么做
 *
 * `BackstageWebView.setCookie()`：页面加载完成 → `CookieManager.getInstance().getCookie(url)`
 * （Android WebView 的 cookie）→ `CookieStore.setCookie(tag, cookie)`，其中 `tag` 是
 * **源 URL**（`AnalyzeUrl` 构造 `BackstageWebView` 时传的是 `source?.getKey()`）。
 * 我们的 JVM 里 WebView 是桩 → 那一步的来源换成 **CDP 浏览器**（[BrowserBridge.cookies]）。
 *
 * ## 为什么值得做
 *
 * App 的请求层**本来就认 `CookieStore`**：`BookSource.enabledCookieJar` 默认 `true` →
 * 请求带 `CookieJar` 头 → 网络拦截器 `CookieManager.loadRequest` → `CookieStore.getCookie
 * (domain)`（合并 内存/DB/会话 三层）。所以**只要把 cookie 放进去，后续所有请求自动带上**
 * ——登录墙的源于是可验、可调，而不是被读成「源坏了」。
 *
 * ## 边界（别越过）
 *
 * **不在 CDP 里代替用户登录**。拿的是「已经登录之后」的 cookie：用户在我们的固定 profile
 * 里登一次（预热），之后每次跑批/调试按 URL 读出来。App 的登录入口在 UI 层
 * （`ui/login/SourceLoginViewModel`，要用户填凭据），JVM 里没有那个界面，也不该有。
 */
object SourceCookies {

    /**
     * 登录墙特征词。**这份是跨语言的第二份，权威在 `core/models.py` 的 `LOGIN_MARKERS`**
     * ——加词/改词必须两边一起改，`tests/test_jvm_debug_contract.py` 里有一条测试逐词比对，
     * 漂了会红。为什么不能只留一份：判定要在 JVM 里当场做（那是产生结论的地方），
     * 而 Python 侧那份是本地回放用的，两边跨语言没法共享代码。
     */
    val LOGIN_MARKERS = listOf(
        "请登录", "需要登录", "登录后", "未登录",
        "please log in", "please login", "login required", "sign in to continue",
    )

    /** 注入结果。[len] = 注进去的 cookie 串长度（**0 不是失败**：可能只是这个域没登录过）。 */
    data class Injected(val len: Int, val note: String)

    /**
     * 统一入口：**手工给的优先**（`--cookie` 是用户明确指定的，不该被 profile 里的旧值
     * 盖过），否则从 profile 按 [tag] 读。[session] 为 null（浏览器不可用）时只报原因。
     */
    fun inject(tag: String, manualCookie: String, session: BrowserBridge.Session?): Injected {
        if (tag.isBlank()) return Injected(0, "no_tag")
        if (manualCookie.isNotBlank()) return injectManual(tag, manualCookie)
        if (session == null) return Injected(0, "browser_unavailable")
        return injectFromProfile(session, tag, tag)
    }

    /**
     * 按 [tag]（源 URL）把 [url] 的 cookie 读出来注入。[url] 与 [tag] 可以是不同的地址
     * ——渲染落地地址常常是登录页所在的另一台主机，而我们要的是「源域那份登录态」。
     */
    fun injectFromProfile(session: BrowserBridge.Session, tag: String, url: String): Injected {
        if (tag.isBlank()) return Injected(0, "no_tag")
        val cookieUrl = url.ifBlank { tag }
        val (cookie, why) = BrowserBridge.cookies(session, cookieUrl)
        if (cookie.isBlank()) {
            // 「这个域没有 cookie」与「读失败」都要说出来：前者正常（没登录过），
            // 后者是我们的问题——别把它读成源的结论（AGENTS #4）
            return Injected(0, why.ifBlank { "no_cookie_for_domain" })
        }
        return try {
            CookieStore.replaceCookie(tag, cookie)
            Injected(cookie.length, "")
        } catch (e: Throwable) {
            Injected(0, "store_error: ${e::class.simpleName}: ${e.message?.take(80)}")
        }
    }

    /** 手工注入（args `cookie=` / `--cookie`）：给「不渲染、又不方便预热」的源一条明路。 */
    fun injectManual(tag: String, cookie: String): Injected {
        if (tag.isBlank()) return Injected(0, "no_tag")
        return try {
            CookieStore.replaceCookie(tag, cookie)
            Injected(cookie.length, "manual")
        } catch (e: Throwable) {
            Injected(0, "store_error: ${e::class.simpleName}: ${e.message?.take(80)}")
        }
    }

    /** 命中的登录特征词（没有则空串）。大小写不敏感，与 `login_marker_of` 同语义。 */
    fun loginMarkerOf(text: String?): String {
        val low = (text ?: "").lowercase()
        return LOGIN_MARKERS.firstOrNull { low.contains(it) } ?: ""
    }
}

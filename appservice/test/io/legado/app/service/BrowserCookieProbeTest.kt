package io.legado.app.service

import io.legado.app.help.http.CookieStore
import io.legado.app.probe.WindowsPathAssetManagerShadow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * 探针：**能不能从我们那条 CDP 浏览器里把 cookie 读出来，再喂进 App 的 `CookieStore`**。
 *
 * 这决定 A3（cookie 注入）走哪条路。上游自己在设备上做的就是这件事——
 * `BackstageWebView.setCookie()`：每次页面加载完成 → `CookieManager.getInstance()
 * .getCookie(url)`（Android WebView 的 cookie）→ `CookieStore.setCookie(tag, cookie)`。
 * 而**我们的 JVM 里 WebView 是桩**，那个来源只能是 CDP 浏览器。
 *
 * 要实证两件事，缺一件这条路就不成立：
 * ① CDP 能不能给出该 URL 的 cookie（`Network.getCookies` / `getAllCookies` /
 *    `Storage.getCookies` 三条都试，看哪条在 page 目标上真的可用）；
 * ② 读出来的 cookie 串能不能喂进 `CookieStore` 并**原样读回**——即 Room（`appDb`）
 *    与 `CacheManager` 在 Robolectric 里到底能不能用。
 *
 * 会真联网、会真起浏览器。跑法（Gradle 会吞 stdout，用 `--info` 看 `PROBE:` 行）：
 *     cmd /c appservice/legado-gradle.bat :app:testAppDebugUnitTest \
 *         --tests io.legado.app.service.BrowserCookieProbeTest --rerun --info
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class BrowserCookieProbeTest {

    /** 探针访问的页面：会给自己种 cookie 的站点（百度种 BAIDUID）。 */
    private val target = "https://www.baidu.com"

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private fun httpGet(url: String): String? = runCatching {
        http.newCall(Request.Builder().url(url).build()).execute().use { r: Response ->
            if (r.isSuccessful) r.body?.string() else null
        }
    }.getOrNull()

    /**
     * 取一个**已经存在的 page 目标**的 WS 地址。
     *
     * **不用 `/json/new`**：`BrowserBridge.render` 的注释里记着实测——`/json/new`
     * 偶发拿不到页签，所以渲染走的是「复用启动时那个 about:blank 页签」。探针第一版
     * 正是撞在这上面（`/json/new` 拿不到页签，且 `PUT` 少 body 也会直接抛）。
     */
    private fun pageWs(port: Int): String? {
        val body = httpGet("http://127.0.0.1:$port/json/list") ?: return null
        val arr = JSONArray(body)
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            if (o.optString("type") == "page") {
                val ws = o.optString("webSocketDebuggerUrl")
                if (ws.isNotEmpty()) return ws
            }
        }
        return null
    }

    /** 一条 CDP 会话：自己连 WS 发命令（探针不动生产代码）。 */
    private class Cdp {
        private val inbox = LinkedBlockingQueue<String>()
        private var id = 0
        var ws: WebSocket? = null

        fun onMessage(text: String) { inbox.offer(text) }

        fun send(method: String, params: JSONObject? = null): Int {
            val n = ++id
            val o = JSONObject().put("id", n).put("method", method)
            params?.let { o.put("params", it) }
            ws!!.send(o.toString())
            return n
        }

        /** 等 `id` 的应答（忽略事件）。 */
        fun await(wantId: Int, timeoutMs: Long = 15_000): JSONObject? {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline) {
                val text = inbox.poll(500, TimeUnit.MILLISECONDS) ?: continue
                val o = runCatching { JSONObject(text) }.getOrNull() ?: continue
                if (o.optInt("id", -1) == wantId) return o
            }
            return null
        }

        /** 等某个事件（不看 id），用于等页面加载完成。 */
        fun awaitEvent(names: Set<String>, timeoutMs: Long): Boolean {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline) {
                val text = inbox.poll(500, TimeUnit.MILLISECONDS) ?: continue
                val o = runCatching { JSONObject(text) }.getOrNull() ?: continue
                val m = o.optString("method")
                if (m.isNotEmpty() && m in names) return true
            }
            return false
        }
    }

    private fun newCdp(wsUrl: String): Cdp {
        val cdp = Cdp()
        val ws = http.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) { cdp.onMessage(text) }
                override fun onFailure(webSocket: WebSocket, t: Throwable, r: Response?) {
                    cdp.onMessage("""{"__error":"${t::class.simpleName}: ${t.message}"}""")
                }
            })
        cdp.ws = ws
        return cdp
    }

    @Test
    fun cdp_cookies_can_be_injected_into_cookie_store() {
        ValidateService.ensureStarted()      // appCtx + Koin：CookieStore 要用 appDb
        val exe = BrowserBridge.findBrowser()
        println("PROBE: browser=${exe ?: "没找到"}")
        if (exe == null) return

        // 与 BrowserSession 同一个默认 profile：它攒下的 cookie 正是 A3 想复用的东西
        val profile = File(System.getProperty("java.io.tmpdir"), "legado-appservice-profile")
        val (session, why) = BrowserBridge.launch(profile)
        if (session == null) {
            println("PROBE: 启动失败 → $why")
            return
        }
        try {
            val wsUrl = pageWs(session.port)
            if (wsUrl == null) {
                println("PROBE: /json/list 里没有可用的 page 目标")
                return
            }
            val cdp = newCdp(wsUrl)
            println("PROBE: 连上页签 = …${wsUrl.takeLast(20)}")

            // ① **不导航**（也没开 Network 域）直接查该 URL 的 cookie：查的是 profile 的
            //    cookie 库，不是页面。这条决定批次设计——成立就不必为每条源渲染一遍，
            //    只按 URL 问一次即可。
            val pre = cdp.await(cdp.send("Network.getCookies",
                JSONObject().put("urls", JSONArray().put(target))), 15_000)
            val preCookies = pre?.optJSONObject("result")?.optJSONArray("cookies")
            println("PROBE: 导航前 Network.getCookies → ${preCookies?.length() ?: 0} 条"
                + (pre?.optJSONObject("error")?.let { " error=${it.optString("message")}" } ?: "")
                + "（>0 = 不用导航也能读 profile 的 cookie）")

            cdp.await(cdp.send("Page.enable"))
            cdp.await(cdp.send("Network.enable"))
            cdp.send("Page.navigate", JSONObject().put("url", target))
            val loaded = cdp.awaitEvent(
                setOf("Page.loadEventFired", "Page.domContentEventFired"), 20_000)
            println("PROBE: 页面加载事件 = $loaded（$target）")
            Thread.sleep(1500)      // 给页面脚本种 cookie 留一点时间

            // 三条查法都试：哪条在 page 目标上真能用，是这一步要回答的
            val all = mutableListOf<JSONObject>()
            for ((method, params) in listOf(
                "Network.getCookies" to JSONObject().put("urls", JSONArray().put(target)),
                "Network.getAllCookies" to null,
                "Storage.getCookies" to null,
            )) {
                val r = cdp.await(cdp.send(method, params), 20_000)
                val arr = r?.optJSONObject("result")?.optJSONArray("cookies")
                val err = r?.optJSONObject("error")?.optString("message")
                println("PROBE: $method → ${arr?.length() ?: 0} 条"
                    + (if (err != null) " error=$err" else ""))
                for (i in 0 until (arr?.length() ?: 0)) all += arr!!.getJSONObject(i)
            }
            val cookies = all.distinctBy { it.optString("name") + "|" + it.optString("domain") }
            println("PROBE: 合计 ${cookies.size} 条（去重）")
            cookies.take(8).forEach {
                println("PROBE:   ${it.optString("name")}=${it.optString("value").take(12)}"
                    + "  domain=${it.optString("domain")}")
            }
            if (cookies.isEmpty()) {
                println("PROBE: **CDP 一条都没读到** —— 这条路要在这里停住")
                return
            }

            // ② 喂进 App 的 CookieStore（上游 BackstageWebView.setCookie 就是这么做的）
            val cookieHeader = cookies.joinToString("; ") {
                it.optString("name") + "=" + it.optString("value")
            }
            println("PROBE: 待注入 cookie 串长度 = ${cookieHeader.length}")
            try {
                CookieStore.setCookie(target, cookieHeader)
                val back = CookieStore.getCookie(target)
                println("PROBE: CookieStore 读回长度 = ${back?.length ?: -1}"
                    + if (back.orEmpty().isNotEmpty()) "（**注入→读回成立**）"
                    else "（**读回为空：这条路要换注入点**）")
                // ② 上游 `BackstageWebView.setCookie` 用的是 setCookie，而我们要用
                //    replaceCookie（多一步 merge）——确认它在 JVM 里也成立
                CookieStore.replaceCookie(target, "probe_cookie=1")
                val merged = CookieStore.getCookie(target).orEmpty()
                println("PROBE: replaceCookie 后读回 = ${merged.length}，含 probe_cookie=1 = "
                    + merged.contains("probe_cookie=1"))
            } catch (e: Throwable) {
                val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
                println("PROBE: **CookieStore 注入失败**：${e::class.simpleName}: ${e.message?.take(160)}")
                println("PROBE:   根因：${root::class.simpleName}: ${root.message?.take(160)}")
            }
        } finally {
            session.close()     // 不删 profile：cookie 就在里面，那正是要复用的东西
        }
    }
}

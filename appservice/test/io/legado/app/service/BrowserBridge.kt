package io.legado.app.service

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.net.ServerSocket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * 浏览器桥（S3-4）：把「页面渲染」这一步交给本机已装的 Edge / Chrome。
 *
 * **为什么这样接**：App 的 `BackstageWebView` 只替换**取数那一步**——加载页面、
 * 执行 JS、把渲染后的 DOM 当响应体返回（lessons §四十九）。而 App 的解析层
 * （`BookList.analyzeBookList` / `BookChapterList` / `BookContent.analyzeContent`）
 * 都收 `body: String?`，所以**不需要 shadow WebView**：我们自己拿渲染后的 HTML，
 * 再喂给同一套解析器即可。同为「零入侵」，且少一层易碎的影子实现。
 *
 * 协议：CDP over WebSocket。流程最短的一条是
 * `PUT /json/new` 开页签 → 连它的 webSocketDebuggerUrl → `Page.enable` →
 * `Page.navigate` → 等 load → `Runtime.evaluate('document.documentElement.outerHTML')`。
 *
 * **专用 user-data-dir**：新版 Chromium 在默认 profile 上禁用远程调试（安全加固），
 * 必须给非默认目录；好处是那个 profile 里能放 cookie（对登录墙后的源有帮助）。
 */
object BrowserBridge {

    /** 浏览器二进制候选（本机实测 Edge 存在；Chrome 作兜底）。 */
    private val CANDIDATES = listOf(
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    )

    //: **两个客户端，别混用**。WebSocket 不能有读超时（`readTimeout(0)` = 无限），
    //: 而 CDP 的 HTTP 端点（/json/version、/json/new）必须**有**超时——共用一个
    //: 客户端的实测后果：端点不响应时永久阻塞，而 `ValidateService.browser()` 正
    //: 持有锁 → **整批源一起卡死**（jstack 定位：BrowserBridge.httpGet → launch）。
    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .build()

    private val wsClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // WebSocket 不能有读超时
        .build()

    data class Result(val ok: Boolean, val html: String = "", val reason: String = "")

    fun findBrowser(): String? = CANDIDATES.firstOrNull { File(it).isFile }

    private fun freePort(): Int = ServerSocket(0).use { it.localPort }

    /** 常驻的浏览器进程 + 调试端口。批量跑时复用，别每个源起一个。
     *  @param tempProfile 非 null 时（自愈路径的 `-run<时间戳>` 专属 profile），
     *      [close] 会把它删掉。固定 profile **永远传 null**——它攒的 cookie
     *      是给登录墙后的源用的，删了每次都是冷启动。 */
    class Session(val process: Process, val port: Int, val profileDir: File,
                  private val exhaust: Exhaust,
                  private val tempProfile: Boolean = false) {
        /** 浏览器自己的输出尾部（启动失败时贴进原因里，省得靠猜）。 */
        fun tail(): String = exhaust.tail()

        fun close() {
            runCatching { process.destroy() }
            runCatching {
                if (!process.waitFor(5, TimeUnit.SECONDS)) process.destroyForcibly()
            }
            runCatching { exhaust.stopped = true }
            // 等进程真退出了才能删：Windows 上文件被占着删不动
            if (tempProfile) runCatching { profileDir.deleteRecursively() }
        }
    }

    /**
     * **必须把子进程的输出读掉**：`redirectErrorStream(true)` 只是把 stderr 并进
     * stdout，管道缓冲（几 KB）写满后**子进程会阻塞在写日志上**——表现是
     * 「进程活着，但调试端口一直不出来」，手工跑（重定向到 /dev/null）却一切正常。
     * 保留最后 40 行，失败时贴进原因里。
     */
    class Exhaust(private val stream: java.io.InputStream) {
        @Volatile var stopped = false
        private val lines = ArrayDeque<String>()

        fun start(): Exhaust {
            val th = Thread {
                runCatching {
                    stream.bufferedReader().useLines { seq ->
                        for (line in seq) {
                            if (stopped) break
                            synchronized(lines) {
                                lines.addLast(line.take(200))
                                while (lines.size > 40) lines.removeFirst()
                            }
                        }
                    }
                }
            }
            th.isDaemon = true
            th.start()
            return this
        }

        fun tail(): String = synchronized(lines) { lines.joinToString(" | ").take(600) }
    }

    /**
     * 启动浏览器（headless + 调试端口）。失败时第二个返回值说明缺什么。
     * @param tempProfile 自愈路径的专属 profile 才置 true——close 时删目录。
     */
    fun launch(profileDir: File, timeoutMs: Long = 20000,
               tempProfile: Boolean = false): Pair<Session?, String> {
        val exe = findBrowser() ?: return null to "browser_unavailable: 没找到 Edge/Chrome"
        profileDir.mkdirs()
        val port = freePort()
        val cmd = listOf(
            exe,
            "--headless=new",
            "--remote-debugging-port=$port",
            "--user-data-dir=${profileDir.absolutePath}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-extensions",
            "--window-size=1280,900",
            "about:blank",
        )
        val proc = ProcessBuilder(cmd).redirectErrorStream(true).start()
        val exhaust = Exhaust(proc.inputStream).start()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (httpGet("http://127.0.0.1:$port/json/version") != null) {
                return Session(proc, port, profileDir, exhaust, tempProfile = tempProfile) to ""
            }
            if (!proc.isAlive) {
                return null to ("browser_unavailable: 浏览器进程已退出（${exhaust.tail()}）")
            }
            Thread.sleep(300)
        }
        runCatching { proc.destroyForcibly() }
        return null to ("browser_unavailable: 调试端口 $port 在 ${timeoutMs}ms 内没起来" +
            "（${exhaust.tail()}）")
    }

    private fun httpGet(url: String): String? = runCatching {
        http.newCall(Request.Builder().url(url).build()).execute().use { r: Response ->
            if (r.isSuccessful) r.body?.string() else null
        }
    }.getOrNull()

    private fun httpPut(url: String): String? = runCatching {
        val body = okhttp3.RequestBody.create(null, ByteArray(0))
        http.newCall(Request.Builder().url(url).put(body).build()).execute().use { r: Response ->
            if (r.isSuccessful) r.body?.string() else null
        }
    }.getOrNull()

    /**
     * 渲染一个 URL，返回 `document.documentElement.outerHTML`。
     *
     * 浏览器不可用 / 超时都**显式返回原因**（`browser_unavailable` / `render_timeout`），
     * 调用方据此判 unknown——不静默退回「空壳」结论（那会把工具的欠缺说成源的问题）。
     */
    fun render(session: Session, url: String, timeoutMs: Long = 30000,
               waitAfterLoadMs: Long = 800): Result {
        // **复用启动时那个 about:blank 页签**（/json/list 的第一个 page），
        // 不每次 `/json/new`：少一个端点就少一类失败（实测 /json/new 偶发拿不到页签），
        // 而且只有一个页签时，渲染天然是串行的——不必再操心多页签互相干扰。
        // 页签级互斥由 [renderLock] 保证。
        val page = pageTarget(session) ?: return Result(false, reason = "cdp_error: 没有可用的页签")
        val wsUrl = page.optString("webSocketDebuggerUrl")
        if (wsUrl.isEmpty()) return Result(false, reason = "cdp_error: 页签没有调试地址")

        val inbox = LinkedBlockingQueue<String>()
        val ws: WebSocket = wsClient.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) { inbox.offer(text) }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    inbox.offer("""{"__error":"${t::class.simpleName}: ${t.message}"}""")
                }
            })
        try {
            if (!send(ws, 1, "Page.enable", null, inbox, timeoutMs)) {
                return Result(false, reason = "cdp_error: Page.enable 无响应")
            }
            val params = JSONObject().put("url", url)
            ws.send(JSONObject().put("id", 2).put("method", "Page.navigate")
                .put("params", params).toString())
            val loaded = awaitEvent(inbox, setOf("Page.loadEventFired", "Page.domContentEventFired"),
                                    timeoutMs)
            if (!loaded) {
                return Result(false, reason = "render_timeout: ${timeoutMs}ms 内没有 load 事件")
            }
            if (waitAfterLoadMs > 0) Thread.sleep(waitAfterLoadMs)
            // 等文档真的 complete 再取 HTML（SPA 常有二次渲染）
            val end = System.currentTimeMillis() + 5000
            while (System.currentTimeMillis() < end) {
                if (eval(ws, 10, "document.readyState", inbox, 5000) == "complete") break
                Thread.sleep(200)
            }
            val html = eval(ws, 11, "document.documentElement.outerHTML", inbox, timeoutMs)
            return if (html.isNullOrEmpty()) Result(false, reason = "render_empty: 渲染后 DOM 为空")
            else Result(true, html = html)
        } finally {
            runCatching { ws.close(1000, "done") }
        }
    }

    //: 渲染是**串行**的：只有一个页签，两个源同时 Page.navigate 会互相把页面顶掉。
    private val renderLock = Any()

    /** 顺便把整段渲染也锁起来（页签是共享资源）。 */
    fun renderSerial(session: Session, url: String, timeoutMs: Long = 30000,
                     waitAfterLoadMs: Long = 800): Result =
        synchronized(renderLock) { render(session, url, timeoutMs, waitAfterLoadMs) }

    /** 取文档里第一个 `type == "page"` 的页签。 */
    private fun pageTarget(session: Session): JSONObject? {
        val list = httpGet("http://127.0.0.1:${session.port}/json/list") ?: return null
        val arr = runCatching { org.json.JSONArray(list) }.getOrNull() ?: return null
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("type") == "page") return o
        }
        return null
    }

    private fun send(ws: WebSocket, id: Int, method: String, params: JSONObject?,
                     inbox: LinkedBlockingQueue<String>, timeoutMs: Long): Boolean {
        val obj = JSONObject().put("id", id).put("method", method)
        if (params != null) obj.put("params", params)
        ws.send(obj.toString())
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val msg = inbox.poll(200, TimeUnit.MILLISECONDS) ?: continue
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (o.optInt("id", -1) == id) return !o.has("error")
        }
        return false
    }

    private fun awaitEvent(inbox: LinkedBlockingQueue<String>, names: Set<String>,
                           timeoutMs: Long): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val msg = inbox.poll(200, TimeUnit.MILLISECONDS) ?: continue
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (names.contains(o.optString("method"))) return true
        }
        return false
    }

    /** `Runtime.evaluate` 一个表达式，取回字符串结果。 */
    private fun eval(ws: WebSocket, id: Int, expr: String,
                     inbox: LinkedBlockingQueue<String>, timeoutMs: Long): String? {
        val params = JSONObject().put("expression", expr).put("returnByValue", true)
        val obj = JSONObject().put("id", id).put("method", "Runtime.evaluate")
            .put("params", params)
        ws.send(obj.toString())
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val msg = inbox.poll(200, TimeUnit.MILLISECONDS) ?: continue
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (o.optInt("id", -1) != id) continue
            val result = o.optJSONObject("result") ?: return null
            val value = result.opt("result")
            return if (value is JSONObject && value.has("value")) value.get("value") as? String
            else null
        }
        return null
    }
}

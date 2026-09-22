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

    /**
     * 渲染结果。`body` 是**取到的正文**——默认是渲染后的
     * `document.documentElement.outerHTML`；给了 `js` 参数时则是**那个 JS 的求值结果**
     * （S5-A2 起）。**原字段名是 `html`，改名是因为它不该被当 HTML 读**：名字说 html、
     * 里面装的却是 JS 返回值时，调用方会照着 HTML 去解析它（本仓库被这类误导性命名
     * 坑过多次，见 lessons §十九）。
     */
    data class Result(val ok: Boolean, val body: String = "", val reason: String = "",
                      /** 落地地址（重定向后）。取不到时回退请求地址。 */
                      val url: String = "",
                      /** 这一页**实际发过的接口请求**（XHR / Fetch，有上界）。见 [collectNetwork]。
                       *  **必须是普通 Map / List**：侧车编码器（`ServiceJson.element`）只递归
                       *  List / Map，`org.json.JSONArray` 会落到 `toString()` 那支——于是侧车里
                       *  存的是一个**字符串**，Python 侧的形状闸门只会说「形状不对」。 */
                      val network: List<Map<String, Any?>>? = null,
                      /** 流过来的 `Network.*` 事件条数（诊断：0 = 域没启用或事件没到）。 */
                      val networkEvents: Int = 0,
                      /** 请求按类型的计数（`XHR=0,Document=1,…`）：让「没抓到接口」自解释。 */
                      val networkTypes: String = "")

    fun findBrowser(): String? = CANDIDATES.firstOrNull { File(it).isFile }

    /** 上一次清理的结果（给侧车诊断用）。**"没清理" 与 "清理失败" 必须分得开**——
     *  前者说明代码路径没走到，后者说明命令没杀掉，两件事的下一步完全不同。 */
    @Volatile
    var lastCleanupNote: String = "not_called"

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
            // **先走协议级关闭**：Chromium 的浏览器端点上有 `Browser.close`，它会
            // 优雅退出——连 profile 锁一起清干净（A3 复用固定 profile 攒 cookie 的前提）。
            // 免 shell、免引号、免进程树语义。
            val graceful = closeBrowser(this)
            // 兜底：协议没关掉（浏览器卡死 / 端点在但不响应）才动刀子。
            // `destroy()` 不杀树（实测一次渲染 15 个进程只剩 10 个），所以借
            // taskkill /T /F——它是**普通 exe**，不经过 shell 引号那一层。
            if (!graceful) killTree(process)
            lastCleanupNote = if (graceful) "graceful" else "killtree_fallback"
            runCatching { exhaust.stopped = true }
            // 等进程真退出了才能删：Windows 上文件被占着删不动
            if (tempProfile) runCatching { profileDir.deleteRecursively() }
        }
    }

    /**
     * 用 CDP 的 `Browser.close` 优雅关掉这个浏览器会话。
     *
     * 返回是否**确认已退出**，判据是「调试端口不再应答」——`Browser.close` 通常不回
     * 响应就断开连接，所以**别等应答**，等端口消失才可靠。
     */
    private fun closeBrowser(session: Session): Boolean {
        val info = httpGet("http://127.0.0.1:${session.port}/json/version") ?: return false
        val wsUrl = runCatching { JSONObject(info).optString("webSocketDebuggerUrl") }
            .getOrNull()?.takeIf { it.isNotBlank() } ?: return false
        val inbox = LinkedBlockingQueue<String>()
        val ws: WebSocket = wsClient.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) { inbox.offer(text) }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    inbox.offer("""{"__error":"${t::class.simpleName}"}""")
                }
            })
        return try {
            ws.send(JSONObject().put("id", 1).put("method", "Browser.close").toString())
            val deadline = System.currentTimeMillis() + 8000
            var gone = false
            while (System.currentTimeMillis() < deadline && !gone) {
                gone = httpGet("http://127.0.0.1:${session.port}/json/version") == null
                if (!gone) Thread.sleep(200)
            }
            gone
        } catch (e: Throwable) {
            false
        } finally {
            runCatching { ws.close(1000, "closing") }
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
               tempProfile: Boolean = false, proxy: String = ""): Pair<Session?, String> {
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
        ) + proxyArg(proxy) + listOf(
            "about:blank",
        )
        val proc = ProcessBuilder(cmd).redirectErrorStream(true).start()
        val exhaust = Exhaust(proc.inputStream).start()
        val deadline = System.currentTimeMillis() + timeoutMs
        var starterExited = false
        while (System.currentTimeMillis() < deadline) {
            if (httpGet("http://127.0.0.1:$port/json/version") != null) {
                return Session(proc, port, profileDir, exhaust, tempProfile = tempProfile) to ""
            }
            // **`!proc.isAlive` 只记下来，不作为判定**（2026-09-20 实测踩坑）：
            // Chromium 启动后会把活**移交给另一个进程**，我们 ProcessBuilder 起的那个
            // 随即退出——但浏览器是好的、调试端口也在。原实现看到进程没了就当场判
            // 「浏览器进程已退出」并 return，于是：① 明明是好的却报不可用（A2 从
            // 「能渲染」变成「一直 browser_unavailable」就是这个）；② **失败路径直接
            // return，把一棵浏览器树漏在后台**（实测一次漏 16 个进程、还占着调试端口）。
            // 真正的判据只有一个：**端口有没有起来**。
            if (!proc.isAlive) starterExited = true
            Thread.sleep(300)
        }
        // 没起来才是真失败——**必须把自己起过的东西收掉**，不能留泄漏
        killTree(proc)
        return null to ("browser_unavailable: 调试端口 $port 在 ${timeoutMs}ms 内没起来" +
            (if (starterExited) "（启动器进程已退出，浏览器没接住）" else "") +
            "（${exhaust.tail()}）")
    }

    /** 杀一棵进程树（Windows 上 `destroy()` 不杀树，实测留 10 个孤儿）。 */
    private fun killTree(proc: Process) {
        val pid = runCatching {
            org.robolectric.util.ReflectionHelpers.callInstanceMethod<Long>(proc, "pid")
        }.getOrNull()
        if (pid != null) {
            runCatching {
                ProcessBuilder("taskkill", "/PID", pid.toString(), "/T", "/F")
                    .redirectErrorStream(true).start().waitFor(5, TimeUnit.SECONDS)
            }
        }
        runCatching { proc.destroyForcibly() }
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
     * 渲染一个 URL，取回正文。
     *
     * 默认取 `document.documentElement.outerHTML`；**传了 `js` 就取那个 JS 的求值结果**
     * ——那正是 App 的 `BackstageWebView` 干的事（加载页面 → 在渲染结果上执行 `js` 选项），
     * S5-A2 的 shadow 要的就是它。
     *
     * `jsRetryTimes > 0` 时按 **App 的重试纪律**处理空结果：求值返回空 → 等
     * `jsRetryIntervalMs` 再来一次，直到非空或用完次数。**不是「渲染一次拿 outerHTML
     * 交差」**——对 `xhr_mode` 类源（页面用 XHR 拉图、DOM 里没有地址）那会得到空页，
     * 判成「源取不到」而真机是好的（假阴性）。App 那边是 1 秒 × 最多 30 次。
     *
     * 浏览器不可用 / 超时 / 求值为空都**显式返回原因**（`browser_unavailable` /
     * `render_timeout` / `js_empty`），调用方据此判 unknown——不静默退回「空壳」结论。
     */
    fun render(session: Session, url: String, timeoutMs: Long = 30000,
               waitAfterLoadMs: Long = 800,
               js: String? = null,
               jsRetryTimes: Int = 0,
               jsRetryIntervalMs: Long = 1000,
               challengeWaitMs: Long = CHALLENGE_WAIT_MS): Result {
        // **复用启动时那个 about:blank 页签**（/json/list 的第一个 page），
        // 不每次 `/json/new`：少一个端点就少一类失败（实测 /json/new 偶发拿不到页签），
        // 而且只有一个页签时，渲染天然是串行的——不必再操心多页签互相干扰。
        // 页签级互斥由 [renderLock] 保证。
        val page = pageTarget(session) ?: return Result(false, reason = "cdp_error: 没有可用的页签")
        val wsUrl = page.optString("webSocketDebuggerUrl")
        if (wsUrl.isEmpty()) return Result(false, reason = "cdp_error: 页签没有调试地址")

        // **把流过的每条 CDP 消息顺手抄一份**：三个 helper（send / awaitEvent / eval）各自
        // 都只认自己那条应答、其余一律丢掉，而网络事件（`Network.*`）正是「其余」那一类。
        // 抄在这里是**零侵入**：不去改那三个 helper 的匹配语义（它们的行为一个字没动）。
        val tapped = java.util.Collections.synchronizedList(mutableListOf<String>())
        val tapQueue = object : LinkedBlockingQueue<String>() {
            override fun offer(e: String): Boolean {
                tapped.add(e)
                return super.offer(e)
            }
        }
        val ws: WebSocket = wsClient.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) {
                    tapQueue.offer(text)
                }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    tapQueue.offer("""{"__error":"${t::class.simpleName}: ${t.message}"}""")
                }
            })
        try {
            if (!send(ws, 1, "Page.enable", null, tapQueue, timeoutMs)) {
                return Result(false, reason = "cdp_error: Page.enable 无响应")
            }
            // 网络域：默认**开着**（上界很小，见 NET_* 常量）。为什么不设开关：这个开关要
            // 一路穿过 App 的 `AnalyzeUrl` → `BackstageWebView`，那几层不是我们的代码；
            // 而抓包本身很便宜（只留 XHR / Fetch、条数与字节都有上界）。
            send(ws, 3, "Network.enable", null, tapQueue, timeoutMs)
            val params = JSONObject().put("url", url)
            ws.send(JSONObject().put("id", 2).put("method", "Page.navigate")
                .put("params", params).toString())
            val loaded = awaitEvent(tapQueue, setOf("Page.loadEventFired", "Page.domContentEventFired"),
                                    timeoutMs)
            if (!loaded) {
                return Result(false, reason = "render_timeout: ${timeoutMs}ms 内没有 load 事件")
            }
            if (waitAfterLoadMs > 0) Thread.sleep(waitAfterLoadMs)
            // 等文档真的 complete 再取 HTML（SPA 常有二次渲染）
            val end = System.currentTimeMillis() + 5000
            while (System.currentTimeMillis() < end) {
                if (eval(ws, 10, "document.readyState", tapQueue, 5000) == "complete") break
                Thread.sleep(200)
            }
            // 求值表达式：给了 js 就用它，否则仍是整页 outerHTML（原有行为）
            val expr = js?.takeIf { it.isNotBlank() } ?: "document.documentElement.outerHTML"
            var body = eval(ws, 11, expr, tapQueue, timeoutMs)
            // App 的重试纪律：**空结果要重试**，而不是收工（理由见函数注释）
            var tries = 0
            while (body.isNullOrEmpty() && tries < jsRetryTimes) {
                tries++
                Thread.sleep(jsRetryIntervalMs)
                body = eval(ws, 11 + tries, expr, tapQueue, timeoutMs)
            }
            // **反爬拦截页要等它自己再来一次导航**（Cloudflare 那类 JS 挑战）：挑战页的
            // `load` 与 `document.readyState == complete` **都成立**，照上面那条路会稳定取到
            // 「挑战页那一刻」的 DOM——实测（2026-09-21）18read.net 连取两次都是「请稍候…」、
            // www.banxia.cc 两次都是 Attention Required。解完挑战的页面会自己 reload，
            // 所以：识别到挑战页就等下一次 `Page.loadEventFired`，再取一次；有上界，等不到
            // 就把手里这份**如实交回去**。
            // 判它是什么的权力**不在这一侧**（权威是 `core/quality.interstitial_marker`），
            // 这里只决定「要不要再等一等」：先问 App 那一句（`CF_CHALLENGE_PROBE`），
            // 再退回那张通用词表。两侧逐词/逐表达式比对钉在
            // `tests/test_jvm_debug_contract.py::TestChallengeMarkerParity`。
            // 正常页零代价：多一次求值（几毫秒），不进循环。
            var waitedMs = 0L
            var seq = 20
            // 判据并上：先问 App 那一句（页面里的真实状态），再退回词表（通用）
            fun stillChallenge(): Boolean {
                val probe = runCatching { eval(ws, seq++, CF_CHALLENGE_PROBE, tapQueue, 3000) }
                    .getOrNull().orEmpty()
                return isChallenge(body.orEmpty(), probe)
            }
            while (js == null && waitedMs < challengeWaitMs && stillChallenge()) {
                val t0 = System.currentTimeMillis()
                val again = awaitEvent(tapQueue, setOf("Page.loadEventFired"),
                                       challengeWaitMs - waitedMs)
                waitedMs += System.currentTimeMillis() - t0
                if (!again) break                    // 它不再加载了：别再耗着
                // 挑战通过后的那次加载同样可能有二次渲染，等 complete 再取
                val end2 = System.currentTimeMillis() + 5000
                while (System.currentTimeMillis() < end2) {
                    if (eval(ws, seq, "document.readyState", tapQueue, 5000) == "complete") break
                    Thread.sleep(200)
                }
                seq++
                body = eval(ws, seq, expr, tapQueue, timeoutMs)
                seq++
            }
            // 落地地址：App 的 buildStrResponse 用的是 WebView 跳转后的地址（`res.url`），
            // 事件流里的 `≡获取成功:<URL>` 就是它——不取这个的话，重定向的站点会报
            // 请求前的地址。取不到就退回请求地址（只是少一点信息，不判失败）。
            // **id 取 900**：20 起那些 id 归挑战等待循环用，别撞上（CDP 的应答按 id 过滤）
            val finalUrl = runCatching { eval(ws, 900, "location.href", tapQueue, 5000) }
                .getOrNull()?.takeIf { it.isNotBlank() } ?: url
            if (isBrowserError(finalUrl, body.orEmpty())) {
                // **交出去的是失败，不是页面**：错误页当站点用比「没拿到」更糟
                // （下游会拿它的 DOM 去配规则，而它一个字的站点内容都没有）
                return Result(false, reason = "browser_error: 浏览器自己报的错页（" +
                    finalUrl.take(60) + "），不是站点", url = finalUrl)
            }
            return when {
                !body.isNullOrEmpty() -> Result(true, body = body, url = finalUrl,
                    network = collectNetwork(ws, tapped.toList(), tapQueue, 5000),
                    networkEvents = networkEventCount(tapped.toList()),
                    networkTypes = networkTypeCount(tapped.toList()))
                jsRetryTimes > 0 -> Result(false, reason = "js_empty: 求值 $tries 次仍为空",
                                           url = finalUrl)
                else -> Result(false, reason = "render_empty: 渲染后 DOM 为空", url = finalUrl)
            }
        } finally {
            runCatching { ws.close(1000, "done") }
        }
    }

    //: 渲染是**串行**的：只有一个页签，两个源同时 Page.navigate 会互相把页面顶掉。
    private val renderLock = Any()

    /** 挑战页最多等多久（毫秒）。实测 Cloudflare 的 JS 挑战在真浏览器里几秒内过掉。 */
    private const val CHALLENGE_WAIT_MS = 8000L

    /**
     * **App 自己那一句**：上游判 Cloudflare 挑战用的就是这个表达式
     * （`ui/browser/WebViewRouteScreen.kt` 的 `onPageFinished` 里
     * `evaluateJavascript("!!window._cf_chl_opt")`，为真即「还在挑战页」，挑战解完它自己翻回假）。
     *
     * 为什么照抄而不是自己造：它是**页面里的真实状态**，比拿 HTML 匹配字符串精确——
     * 不会误伤正文里出现的词（实测「请稍候」在正常漫画页里出现 60 次）。两个判据是**并上**，
     * 不是替换：这句只管 Cloudflare，非 CF 的验证码墙仍靠词表。
     * 表达式由契约测试钉住（`tests/test_jvm_debug_contract.py::TestChallengeMarkerParity`）。
     */
    private const val CF_CHALLENGE_PROBE = "!!window._cf_chl_opt"

    /**
     * 浏览器走不走代理：`--proxy-server=<值>`（空 = 不加这个参数，行为与以前一字不差）。
     *
     * **为什么浏览器这一侧必须单独做**：App 的 OkHttp 与 App 的 WebView 是两条取数栈——
     * 前者靠源 header 里的 `proxy` 键（`AnalyzeUrl`），后者是**真浏览器**，只认启动参数。
     * 只做前者的话，L2–L4 的「换材料」在代理环境里仍然出不去，而且看不出原因（十-3）。
     */
    internal fun proxyArg(proxy: String): List<String> =
        proxy.trim().takeIf { it.isNotEmpty() }?.let { listOf("--proxy-server=$it") } ?: emptyList()

    /**
     * 反爬拦截页的特征词——**只用来决定「要不要再等一等」**，不下任何结论。
     *
     * 权威那一份是 `core/models.ANTI_BOT_MARKERS`（Python 侧判「这份材料是不是站点」），
     * 这里取的是其中的子集：**必须逐词出现在那一份里**，契约测试
     * `tests/test_jvm_debug_contract.py::TestChallengeMarkerParity` 按源码字面量比。
     *
     * **别往这里加通用词**（「请稍候」这类）：实测正常漫画页正文里它出现 60 次，
     * 按它判会让每个正常页都白等 8 秒（`core/models.py` 里「不要放裸 cloudflare」是同一类错）。
     */
    private val CHALLENGE_MARKERS = listOf(
        "验证码", "人机验证", "安全验证", "滑动验证",
        "__cf_chl", "captcha", "verify you are human",
    )

    /**
     * 挑战还在不在——**两个判据并上**：App 那句 JS 求值（精确、只管 CF）+ 我们的词表（通用）。
     *
     * `cfProbe` 是 `CF_CHALLENGE_PROBE` 的求值结果（`"true"` / `"false"` / 空 = 没求到）。
     */
    internal fun isChallenge(html: String, cfProbe: String = ""): Boolean {
        if (cfProbe.trim().equals("true", ignoreCase = true)) return true
        val low = html.lowercase()
        return CHALLENGE_MARKERS.any { low.contains(it.lowercase()) }
    }

    // ---------------------------------------------------------------- 网络抓包（L4 的材料）

    /** 最多记几条接口请求（这一页的 XHR / Fetch；超出的丢掉并记数）。 */
    private const val NET_LIMIT = 20
    /** 单条响应体的上限（字符）。整页 HTML 接口（有的站搜索页本身就是文档）也够用。 */
    private const val NET_BODY_LIMIT = 256 * 1024
    /** 只记「文本类」响应——图片 / 字体那些抓回来也没法当规则材料。 */
    private val NET_MIME_OK = listOf("json", "text/", "javascript", "xml", "html")

    /**
     * 从抓到的 CDP 消息里**挑出这一页实际发过的接口请求**：XHR / Fetch，配成
     * `{url, method, body?, status, mime, requestId}`。
     *
     * **为什么是「观察」而不是「读 JS 猜」**：2026-09-21 那一轮实测说明，需要这条路线的站，
     * 其接口地址往往在外部 bundle 里拼、带签名或 POST body——而且**页面 HTML 我们常常根本拿不到**
     * （403 / Cloudflare）。而浏览器真发出去的那条请求，地址、方法、body 都是现成的。
     *
     * 纯函数（只吃字符串列表），所以它有自己的单测；**取响应体**那一步要 CDP 往返，
     * 在 [collectNetwork] 里做。
     */
    /**
     * 抓到的请求按类型计数（`Document` / `XHR` / `Image`…）。
     *
     * **为什么要报这个**：`network` 空有两种意思——「这一页真没发接口」与「抓包没生效」，
     * 光看条数分不开（与 `dropped_payload` 同一条纪律：把看不见的事实报出来）。
     */
    internal fun networkTypeCount(messages: List<String>): String {
        val counter = LinkedHashMap<String, Int>()
        for (msg in messages) {
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (o.optString("method") != "Network.requestWillBeSent") continue
            val type = o.optJSONObject("params")?.optString("type").orEmpty().ifEmpty { "Other" }
            counter[type] = (counter[type] ?: 0) + 1
        }
        return counter.entries.sortedByDescending { it.value }
            .joinToString(",") { "${it.key}=${it.value}" }
    }

    internal fun networkEventCount(messages: List<String>): Int {
        var n = 0
        for (msg in messages) {
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (o.optString("method").startsWith("Network.")) n++
        }
        return n
    }

    internal fun networkRequests(messages: List<String>, limit: Int = NET_LIMIT): List<Map<String, Any?>> {
        val sent = LinkedHashMap<String, MutableMap<String, Any?>>()
        for (msg in messages) {
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            val params = o.optJSONObject("params") ?: continue
            when (o.optString("method")) {
                "Network.requestWillBeSent" -> {
                    val req = params.optJSONObject("request") ?: continue
                    val id = params.optString("requestId")
                    if (id.isEmpty() || sent.containsKey(id)) continue
                    // **只留 XHR / Fetch**：文档 / 图片 / 脚本不是「接口」，留着只会把上限挤掉
                    val type = params.optString("type")
                    if (type != "XHR" && type != "Fetch") continue
                    if (sent.size >= limit) continue
                    val item = mutableMapOf<String, Any?>(
                        "requestId" to id,
                        "url" to req.optString("url"),
                        "method" to req.optString("method", "GET"),
                        "post_data" to req.optString("postData", ""),
                        "status" to 0,
                        "mime" to "",
                    )
                    sent[id] = item
                }
                "Network.responseReceived" -> {
                    val id = params.optString("requestId")
                    val item = sent[id] ?: continue
                    val resp = params.optJSONObject("response") ?: continue
                    item["status"] = resp.optInt("status", 0)
                    item["mime"] = resp.optString("mimeType", "")
                }
            }
        }
        return sent.values.map { it as Map<String, Any?> }
    }

    /**
     * 取回上面挑出来的请求的响应体，附在 `body` 字段上（只取文本类，有上界）。
     *
     * `Network.getResponseBody` 必须在响应还在内存里时问（页面导航 / 清缓存之后就没了），
     * 所以这一步紧跟在渲染之后做。
     */
    private fun collectNetwork(ws: WebSocket, messages: List<String>,
                               inbox: LinkedBlockingQueue<String>,
                               timeoutMs: Long): List<Map<String, Any?>> {
        val out = mutableListOf<Map<String, Any?>>()
        var id = 700
        for (item in networkRequests(messages)) {
            val mime = item["mime"] as? String ?: ""
            val status = (item["status"] as? Int) ?: 0
            if (status !in 200..399) continue
            if (NET_MIME_OK.none { mime.lowercase().contains(it) }) continue
            val rid = item["requestId"] as? String ?: continue
            val got = runCatching {
                evalJson(ws, id++, "Network.getResponseBody",
                         JSONObject().put("requestId", rid), inbox, timeoutMs)
            }.getOrNull() ?: continue
            val raw = got.optString("body", "")
            val encoded = got.optBoolean("base64Encoded", false)
            val text = if (encoded) runCatching {
                String(java.util.Base64.getDecoder().decode(raw), Charsets.UTF_8)
            }.getOrDefault("") else raw
            if (text.isEmpty()) continue
            val row = LinkedHashMap<String, Any?>()
            for ((k, v) in item) if (k != "requestId") row[k] = v
            row["body"] = text.take(NET_BODY_LIMIT)
            row["truncated"] = text.length > NET_BODY_LIMIT
            out.add(row)
        }
        return out
    }

    /** 发一条 CDP 命令并取回它的 `result` 对象（抓包用：`Network.getResponseBody`）。 */
    private fun evalJson(ws: WebSocket, id: Int, method: String, params: JSONObject,
                         inbox: LinkedBlockingQueue<String>, timeoutMs: Long): JSONObject? {
        ws.send(JSONObject().put("id", id).put("method", method).put("params", params).toString())
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val msg = inbox.poll(200, TimeUnit.MILLISECONDS) ?: continue
            val o = runCatching { JSONObject(msg) }.getOrNull() ?: continue
            if (o.optInt("id", -1) != id) continue
            return o.optJSONObject("result")
        }
        return null
    }

    /**
     * 交回来的**不是站点、而是浏览器自己那张错误页**吗（连不上 / 被重置 / DNS 失败）。
     *
     * 实测（2026-09-22）：`www.banxia.cc` 有一回返回的是 Edge 的「无法访问此页面」
     * （**317642 字节**，标题就是域名，一个反爬词都没有）——只按词表判会把它当成站点，
     * 于是下游拿它的 DOM 去配规则（选得中、选中的是错误页）。判据是**结构性的**，两条：
     * 落地地址变成 `chrome-error://…`（Chromium 错误页的地址），或 DOM 里出现
     * `main-frame-error`（错误页自己的容器 id）。**都不依赖语言**（错误页文案会本地化）。
     */
    internal fun isBrowserError(finalUrl: String, html: String): Boolean {
        if (finalUrl.trim().startsWith("chrome-error")) return true
        return html.contains("main-frame-error")
    }

    /** 顺便把整段渲染也锁起来（页签是共享资源）。 */
    fun renderSerial(session: Session, url: String, timeoutMs: Long = 30000,
                     waitAfterLoadMs: Long = 800,
                     js: String? = null,
                     jsRetryTimes: Int = 0,
                     jsRetryIntervalMs: Long = 1000): Result =
        synchronized(renderLock) {
            render(session, url, timeoutMs, waitAfterLoadMs, js, jsRetryTimes, jsRetryIntervalMs)
        }

    /**
     * 读浏览器 profile 里**这个 URL 适用的 cookie**（CDP `Network.getCookies`）。
     *
     * 为什么走 CDP 而不是在页面里读 `document.cookie`：**HttpOnly 的 cookie 只有 CDP 看得到**，
     * 而登录态大多正是 HttpOnly（实测 8 条里 BAIDUID 这类都在）。
     *
     * 实测（2026-09-20，`BrowserCookieProbeTest`）：**不需要先导航、也不需要开 `Network`
     * 域**，直接问就是 profile 的 cookie 库——于是「预热登录一次、之后按 URL 读」成立，
     * 批量校验不必为每条源渲染一遍。
     *
     * 返回 `(cookie 串, 原因)`：**空串 = 这个域没有 cookie**，与「读失败」分开报
     * （后者带 `cdp_error:` 前缀）。调用方据此决定注入还是跳过——不静默。
     *
     * **不进 [renderLock]**：这是只读查询，CDP 支持多客户端；把它排在渲染后面只会让批量
     * 的每条源都去等渲染。
     */
    fun cookies(session: Session, url: String, timeoutMs: Long = 5000): Pair<String, String> {
        if (url.isBlank()) return "" to "no_url"
        val page = pageTarget(session) ?: return "" to "cdp_error: 没有可用的页签"
        val wsUrl = page.optString("webSocketDebuggerUrl")
        if (wsUrl.isEmpty()) return "" to "cdp_error: 页签没有调试地址"
        val inbox = LinkedBlockingQueue<String>()
        val ws: WebSocket = wsClient.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) { inbox.offer(text) }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    inbox.offer("""{"__error":"${t::class.simpleName}: ${t.message}"}""")
                }
            })
        return try {
            val params = JSONObject().put("urls", org.json.JSONArray().put(url))
            ws.send(JSONObject().put("id", 1).put("method", "Network.getCookies")
                .put("params", params).toString())
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline) {
                val text = inbox.poll(300, TimeUnit.MILLISECONDS) ?: continue
                val o = runCatching { JSONObject(text) }.getOrNull() ?: continue
                if (o.optInt("id", -1) != 1) continue
                o.optJSONObject("error")?.let { return "" to "cdp_error: ${it.optString("message")}" }
                val arr = o.optJSONObject("result")?.optJSONArray("cookies")
                    ?: return "" to "cdp_error: 应答里没有 cookies"
                val parts = (0 until arr.length()).mapNotNull { i ->
                    val c = arr.optJSONObject(i) ?: return@mapNotNull null
                    val name = c.optString("name")
                    if (name.isEmpty()) null else "$name=${c.optString("value")}"
                }
                return parts.joinToString("; ") to ""
            }
            "" to "cdp_error: ${timeoutMs}ms 内没有应答"
        } finally {
            runCatching { ws.close(1000, "done") }
        }
    }

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

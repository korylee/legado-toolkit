package io.legado.app.service

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.InetAddress
import java.net.ServerSocket
import java.net.SocketTimeoutException

/**
 * 常驻引擎服务：**起一次**（JVM + Robolectric + App + Koin），之后每次调试或校验只付站点的时间。
 * 为什么值得存在、实测数字与取舍见 lessons §六十五 / §六十六 / §六十七（别在这儿抄）。
 *
 * ## 与旧的两个 daemon（DebugServiceDaemon / ValidateServiceDaemon，2026-10-18 合并）
 *
 * 两者本来同构（协议、串行、版本键、idle 退出），合并后按 `op` 分派；**生命周期语义取
 * 更强一侧**（validate 侧是踩坑演化的结果：prepare 的 busy 分类、stop_idle 的身份核验、
 * owner marker）。Python 客户端只有一份（`core.jvm_daemon`），scripts 那条开发链也走它。
 *
 * ## 协议（与一次性那条**同形**，别另造一套）
 *
 * 请求一行 JSON：
 *   `{"id":1,"op":"debug","file":"…","key":"…","out":"…","timeout":60,"cookie":"…"}`
 *   `{"id":2,"op":"validate","file":"…","keyword":"…","out":"…","concurrency":8,…}`
 *   `op` 缺省 = `debug`（scripts 那条链的老请求不带 op）。
 * 应答一行 JSON：`{"id":1,"code":0,"cost_ms":1234,"error":""}`。
 *
 * **产物一字不变**：NDJSON 与侧车（debug）与 results.jsonl（validate）仍落到各自的
 * `out` 路径，Python 侧解析一行不改（`launcher=` 缝就是为这件事）——daemon 只换
 * 「怎么拉起」，不换「跑了什么」。
 *
 * ## 串行是硬约束，不是保守
 *
 * 两个 op 同时跑会互相污染事件流与浏览器 profile；Robolectric 的主 looper 只有一条，
 * `idle()` 必须由正在跑的那个线程驱动。所以这里是**单线程 accept → 跑 → 应答 → 再 accept**。
 *
 * ## 请求间清什么、留什么（别一刀切）
 *
 * - **清**：debug 请求开头的 `ShadowBackstageWebView.reset()`——常驻会让计数跨请求
 *   累积，侧车里的「普通源 shadow_calls 必须是 0」判据会被累积值说反。
 * - **留**：cookie 与浏览器 profile（登录态预热的收益就靠它）。
 * - **浏览器进程照旧每请求收掉**（debug 的决策点在 `DebugService.runOnce` 收尾那段）。
 * - validate 的 `main` 每次收尾自带 `shutdown()`（stopKoin，幂等重启）——保留现状。
 */
object AppServiceDaemon {

    /** 空闲多久退出（秒）。默认 30 分钟：够覆盖一轮改规则的编辑循环，又不至于长期占着。 */
    const val DEFAULT_IDLE_SEC = 1800L

    /**
     * 起服务并**阻塞**在这里，直到空闲超时或被外部停掉。返回退出码（0 = 正常空闲退出）。
     *
     * 只监听 `127.0.0.1`：这不是对外服务，且它握着用户的 cookie 与真实网络出口。
     *
     * `sig` 是调用方给的**版本键**（App 仓库 + appservice 的指纹，由客户端算）：
     * 只用于 `op=ping` 时原样回吐——进程里跑的是不是最新编译的那份类，**只有调用方
     * 比得出来**（改完 Kotlin 不重启，进程里就还是旧类，而那看起来像「规则没生效」）。
     */
    fun serve(port: Int, idleSec: Long = DEFAULT_IDLE_SEC, sig: String = ""): Int {
        if (port <= 0) {
            System.err.println("[appservice] daemon 缺端口（LEGADO_DAEMON_PORT）")
            return DebugService.BAD_INPUT
        }
        val server = ServerSocket(port, 4, InetAddress.getByName("127.0.0.1"))
        server.soTimeout = (idleSec.coerceAtLeast(1L) * 1000L).toInt()
        println("[appservice] daemon 就绪 port=$port idle=${idleSec}s sig=$sig")
        var served = 0
        try {
            while (true) {
                val sock = try {
                    server.accept()
                } catch (e: SocketTimeoutException) {
                    println("[appservice] daemon 空闲 ${idleSec}s，退出（本次共服务 $served 次）")
                    break
                }
                served++
                val keepGoing = try {
                    handle(sock, sig)
                } catch (e: Throwable) {
                    // **一次请求出错不许带走常驻进程**：常驻挂了之后每次调试都会失败，
                    // 而那看起来像「JVM 坏了」（客户端的回落路径才接得住）。
                    System.err.println("[appservice] daemon 处理请求异常: ${e.stackTraceStr()}")
                    true
                }
                if (!keepGoing) {
                    println("[appservice] daemon 收到停止指令，退出（本次共服务 $served 次）")
                    break
                }
            }
        } finally {
            runCatching { server.close() }
            // 收尾一份就够：ValidateService.shutdown 含 BrowserSession.close 与 stopKoin，
            // 而调试与校验的环境都是 ensureStarted 建的（幂等）
            runCatching { ValidateService.shutdown() }
        }
        return DebugService.OK
    }

    /** 一条连接 = 一个请求。读完一行、分派一次（或应答探针）、回一行。返回 false = 该退出。 */
    private fun handle(sock: java.net.Socket, sig: String): Boolean {
        val reader = BufferedReader(InputStreamReader(sock.getInputStream(), Charsets.UTF_8))
        val line = reader.readLine()
        if (line.isNullOrBlank()) return true
        val out = PrintWriter(sock.getOutputStream().writer(Charsets.UTF_8), false)
        val req = runCatching { Json.parseToJsonElement(line).let { it as JsonObject } }
            .getOrElse {
                out.println(respond(0, DebugService.BAD_INPUT, 0, "请求不是 JSON 对象: ${it.message}"))
                out.flush()
                return true
            }
        val id = req.num("id", 0L)
        // **健康探针**（D1）：不回跑一次、只报自己是谁。客户端的「版本对不上就换进程」
        // 与「daemon 还活着吗」两件事都靠它——从进程外杀 pid 有误杀风险，从端口问身份没有。
        if (req.str("op") == "ping") {
            out.println(respond(id, DebugService.OK, 0, "", mapOf(
                "kind" to "engine",
                "ops" to listOf("debug", "validate"),
                "pid" to ProcessHandle.current().pid(),
                "sig" to sig,
            )))
            out.flush()
            return true
        }
        // **优雅停止**（D1）：客户端不能只 kill 进程——`TerminateProcess` 不执行 `finally`，
        // 「正跑着」的那一刻被 kill，Chromium 会活下来**继续占着 profile**：之后任何一次
        // 抓页都「自愈」换临时 profile、**cookie 静默全丢**（代价与实测见
        // `core.jvm_daemon` 的停机路径，那里是决策点）。走这一条就干干净净。
        if (req.str("op") == "stop") {
            out.println(respond(id, DebugService.OK, 0, ""))
            out.flush()
            return false
        }
        return when (req.str("op").ifBlank { "debug" }) {
            "debug" -> handleDebug(id, req, out)
            "validate" -> handleValidate(id, req, out)
            else -> {
                out.println(respond(id, DebugService.BAD_INPUT, 0,
                    "未知 op：" + req.str("op") + "（支持 debug / validate）"))
                out.flush()
                true
            }
        }
    }

    /** 调试 op：参数与 `DebugService.Config` 一一对应，产物 = NDJSON + 侧车。 */
    private fun handleDebug(id: Long, req: JsonObject, out: PrintWriter): Boolean {
        val cfg = DebugService.Config(
            file = req.str("file"),
            key = req.str("key"),
            outPath = req.str("out"),
            timeoutSec = req.num("timeout", DebugService.DEFAULT_TIMEOUT_SEC),
            cookie = req.str("cookie"),
        )
        // 入参校验与 `main` 同一份判断（少了它，缺 file 会走到 IOException 上，
        // 而那个报错长得像「JVM 挂了」而不是「参数不对」）
        if (cfg.file.isBlank() || cfg.key.isBlank() || cfg.outPath.isBlank() || cfg.timeoutSec <= 0) {
            out.println(respond(id, DebugService.BAD_INPUT, 0,
                "缺参数：file/key/out 都要给，timeout 必须为正"))
            out.flush()
            return true
        }
        val t0 = System.currentTimeMillis()
        var err = ""
        val code = try {
            DebugService.runOnce(cfg)
        } catch (e: Throwable) {
            err = "${e::class.simpleName}: ${e.message?.take(200)}"
            System.err.println("[appservice] daemon 跑调试时抛异常: ${e.stackTraceStr()}")
            DebugService.BAD_INPUT
        }
        val cost = System.currentTimeMillis() - t0
        out.println(respond(id, code, cost, err))
        out.flush()
        println("[appservice] daemon 调试完成：code=$code ${cost}ms")
        return true
    }

    /** 校验 op：参数原样进 `ValidateService.main`，产物 = results.jsonl（main 收尾自带 shutdown）。 */
    private fun handleValidate(id: Long, req: JsonObject, out: PrintWriter): Boolean {
        val args = mutableListOf<String>()
        req.str("file").takeIf { it.isNotBlank() }?.let { args += listOf("--file", it) }
        req.str("dir").takeIf { it.isNotBlank() }?.let { args += listOf("--dir", it) }
        req.str("keyword").takeIf { it.isNotBlank() }?.let { args += listOf("--keyword", it) }
        req.str("out").takeIf { it.isNotBlank() }?.let { args += listOf("--out", it) }
        req.str("concurrency").takeIf { it.isNotBlank() }?.let { args += listOf("--concurrency", it) }
        req.str("timeout").takeIf { it.isNotBlank() }?.let { args += listOf("--timeout", it) }
        req.str("limit").takeIf { it.isNotBlank() }?.let { args += listOf("--limit", it) }
        req.str("depth").takeIf { it.isNotBlank() }?.let { args += listOf("--depth", it) }
        req.str("cookie").takeIf { it.isNotBlank() }?.let { args += listOf("--cookie", it) }
        req.str("profile").takeIf { it.isNotBlank() }?.let {
            System.setProperty("legado.browser.profile", it)
        }
        if (req.bool("no_strip_webview")) args += "--no-strip-webview"

        if (req.str("file").isBlank() || req.str("out").isBlank() ||
            (req.num("timeout", 30L) <= 0L)) {
            out.println(respond(id, DebugService.BAD_INPUT, 0,
                "缺参数：file/out 都要给，timeout 必须为正"))
            out.flush()
            return true
        }
        val ownerJob = req.str("owner_job_id").ifBlank { "unknown" }
        val ownerChunk = req.str("owner_chunk").ifBlank { "unknown" }
        val ownerSource = req.str("owner_source_name").ifBlank { "unknown" }
        println("[appservice] daemon 开始校验 job=$ownerJob chunk=$ownerChunk source=$ownerSource")
        val started = System.currentTimeMillis()
        var error = ""
        val code = try {
            // main 的参数解析、深度白名单、真实校验和 results.jsonl 写入全部复用。
            ValidateService.main(args.toTypedArray())
            DebugService.OK
        } catch (e: Throwable) {
            error = "${e::class.simpleName}: ${e.message?.take(240)}"
            System.err.println("[appservice] daemon 执行校验异常: ${e.stackTraceStr()}")
            DebugService.BAD_INPUT
        }
        val cost = System.currentTimeMillis() - started
        out.println(respond(id, code, cost, error))
        out.flush()
        println("[appservice] daemon 校验完成 job=$ownerJob chunk=$ownerChunk code=$code ${cost}ms")
        return true
    }

    private fun respond(id: Long, code: Int, costMs: Long, error: String,
                        extra: Map<String, Any?> = emptyMap()): String =
        Json.encodeToString(
            JsonObject.serializer(),
            // 编码器只此一份（ServiceJson）：各写一个 when 才把 null 写成字符串 "null"（AGENTS #22）
            ServiceJson.toJsonObject(mapOf(
                "id" to id, "code" to code, "cost_ms" to costMs, "error" to error) + extra))

    private fun JsonObject.str(key: String): String =
        runCatching { this[key]?.jsonPrimitive?.contentOrNull ?: "" }.getOrDefault("")

    private fun JsonObject.num(key: String, default: Long): Long =
        runCatching { this[key]?.jsonPrimitive?.longOrNull ?: default }.getOrDefault(default)

    private fun JsonObject.bool(key: String): Boolean =
        runCatching { this[key]?.jsonPrimitive?.contentOrNull == "true" }.getOrDefault(false)

    private fun Throwable.stackTraceStr(): String =
        (this::class.qualifiedName ?: "Throwable") + ": " + (message ?: "") + "\n" +
            stackTrace.take(6).joinToString("\n") { "    at $it" }
}

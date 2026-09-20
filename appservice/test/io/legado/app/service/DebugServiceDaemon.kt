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
 * 常驻调试服务：**起一次**（JVM + Robolectric + App + Koin），之后每次调试只付站点的时间。
 * 为什么值得存在、实测数字与取舍见 lessons §六十五 / §六十六 / §六十七（别在这儿抄）。
 *
 * ## 协议（与一次性那条**同形**，别另造一套）
 *
 * 请求一行 JSON：`{"id":1,"file":"…","key":"…","out":"…","timeout":60,"cookie":"…"}`
 * 应答一行 JSON：`{"id":1,"code":0,"cost_ms":1234,"error":""}`
 *
 * **产物一字不变**：NDJSON 与侧车仍旧落到 `out` / `out.meta.json`，Python 侧
 * `core.jvm_debug` 的解析**一行不改**（`launcher=` 缝就是为这件事）——daemon 只换
 * 「怎么拉起」，不换「跑了什么」。
 *
 * ## 串行是硬约束，不是保守
 *
 * `Debug.log` 只在 `debugSource == sourceUrl` 时 emit（且 `activeSession` 只有一个），
 * 两个请求同时跑会互相污染事件流；且 Robolectric 的主 looper 只有一条，`idle()` 必须由
 * 正在跑的那个线程驱动。所以这里是**单线程 accept → 跑 → 应答 → 再 accept**。
 *
 * ## 请求间清什么、留什么（别一刀切）
 *
 * - **清**：`Debug` 的静态态（`runOnce` 收尾的 `session.cancel()`）、shadow 计数器
 *   （`runOnce` 开头的 `reset()`）——常驻会让它们跨请求累积，而侧车里的诊断判据
 *   （「普通源 shadow_calls 必须是 0」）会被累积值说反。
 * - **留**：cookie 与浏览器 profile（A3 的收益就靠它）。
 * - **浏览器进程照旧每请求收掉**：留着能省 0.65s/次，但会一直占着 profile（取舍与实测
 *   在 `DebugService.runOnce` 收尾那段——**那里是决策点**，理由别在这儿再抄一份）。
 */
object DebugServiceDaemon {

    /** 空闲多久退出（秒）。默认 30 分钟：够覆盖一轮改规则的编辑循环，又不至于长期占着。 */
    const val DEFAULT_IDLE_SEC = 1800L

    /**
     * 起服务并**阻塞**在这里，直到空闲超时或被外部杀掉。返回退出码（0 = 正常空闲退出）。
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
                    // 而那看起来像「JVM 坏了」（客户端的降级路径才接得住）。
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
            // 收尾：浏览器**每请求结束就已经关了**（`runOnce` 收尾），这里再兜一次是防
            // 「请求正跑着时收到 stop」那一瞬间的残留会话
            runCatching { BrowserSession.close() }
        }
        return DebugService.OK
    }

    /** 一条连接 = 一个请求。读完一行、跑一次（或应答探针）、回一行。返回 false = 该退出。 */
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
        // **健康探针**（D1）：不回跑一次调试、只报自己是谁。客户端的「版本对不上就换进程」
        // 与「daemon 还活着吗」两件事都靠它——从进程外杀 pid 有误杀风险，从端口问身份没有。
        if (req.str("op") == "ping") {
            out.println(respond(id, DebugService.OK, 0, "", mapOf(
                "pid" to ProcessHandle.current().pid(), "sig" to sig)))
            out.flush()
            return true
        }
        // **优雅停止**（D1）：客户端不能只 kill 进程——`TerminateProcess` 不执行 `finally`，
        // 于是「正跑着」的那一刻被 kill，那个 Chromium 会活下来**继续占着 profile**：
        // 之后任何一次抓页都「自愈」换临时 profile、**cookie 静默全丢**（代价与实测见
        // `core.jvm_daemon._stop_port`，那里是决策点）。走这一条就干干净净。
        if (req.str("op") == "stop") {
            out.println(respond(id, DebugService.OK, 0, ""))
            out.flush()
            return false
        }
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
        println("[appservice] daemon 请求完成：code=$code ${cost}ms")
        return true
    }

    private fun respond(id: Long, code: Int, costMs: Long, error: String,
                        extra: Map<String, Any?> = emptyMap()): String =
        Json.encodeToString(
            JsonObject.serializer(),
            // 编码器只此一份（ServiceJson）：跑批那边就是各写一个 when 才把 null
            // 写成字符串 "null" 的（AGENTS #22）
            ServiceJson.toJsonObject(mapOf(
                "id" to id, "code" to code, "cost_ms" to costMs, "error" to error) + extra))

    private fun JsonObject.str(key: String): String =
        runCatching { this[key]?.jsonPrimitive?.contentOrNull ?: "" }.getOrDefault("")

    private fun JsonObject.num(key: String, default: Long): Long =
        runCatching { this[key]?.jsonPrimitive?.longOrNull ?: default }.getOrDefault(default)

    private fun Throwable.stackTraceStr(): String =
        (this::class.qualifiedName ?: "Throwable") + ": " + (message ?: "") + "\n" +
            stackTrace.take(6).joinToString("\n") { "    at $it" }
}

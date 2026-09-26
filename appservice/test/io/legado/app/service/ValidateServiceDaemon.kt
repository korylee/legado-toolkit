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
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * ValidateService 的常驻入口。
 *
 * 协议仍然把每次校验的输入送进 ValidateService.main，结果仍由 --out 写成原来的
 * results.jsonl；daemon 只负责摊平测试 JVM/Gradle 的启动成本。请求串行执行，避免
 * 多个请求同时使用 Robolectric 和浏览器 profile。
 */
object ValidateServiceDaemon {

    const val DEFAULT_IDLE_SEC = 1800L
    private const val OK = 0
    private const val BAD_INPUT = 4

    fun serve(port: Int, idleSec: Long = DEFAULT_IDLE_SEC, sig: String = ""): Int {
        ServiceJson.writeRuntimeSnapshot("validate_daemon")
        if (port <= 0) {
            System.err.println("[appservice] Validate daemon 缺端口")
            return BAD_INPUT
        }
        val server = ServerSocket(port, 4, InetAddress.getByName("127.0.0.1"))
        server.soTimeout = (idleSec.coerceAtLeast(1L) * 1000L).toInt()
        println("[appservice] Validate daemon 就绪 port=$port idle=${idleSec}s sig=$sig")
        var served = 0
        try {
            while (true) {
                val sock = try {
                    server.accept()
                } catch (e: SocketTimeoutException) {
                    println("[appservice] Validate daemon 空闲 ${idleSec}s，退出（本次共服务 $served 次）")
                    break
                }
                served++
                val keepGoing = try {
                    handle(sock, sig)
                } catch (e: Throwable) {
                    System.err.println("[appservice] Validate daemon 处理请求异常: ${e.stackTraceStr()}")
                    true
                }
                if (!keepGoing) {
                    println("[appservice] Validate daemon 收到停止指令，退出（本次共服务 $served 次）")
                    break
                }
            }
        } finally {
            runCatching { server.close() }
            runCatching { ValidateService.shutdown() }
        }
        return OK
    }

    private fun handle(sock: Socket, sig: String): Boolean {
        sock.use { connection ->
            val reader = BufferedReader(InputStreamReader(connection.getInputStream(), Charsets.UTF_8))
            val writer = PrintWriter(connection.getOutputStream().writer(Charsets.UTF_8), false)
            val line = reader.readLine()
            if (line.isNullOrBlank()) return true
            val req = runCatching {
                Json.parseToJsonElement(line) as JsonObject
            }.getOrElse {
                writer.println(respond(0, BAD_INPUT, 0, "请求不是 JSON 对象: ${it.message}"))
                writer.flush()
                return true
            }
            val id = req.num("id", 0L)
            if (req.str("op") == "ping") {
                writer.println(respond(id, OK, 0, "", mapOf(
                    "kind" to "validate",
                    "pid" to ProcessHandle.current().pid(),
                    "sig" to sig,
                )))
                writer.flush()
                return true
            }
            if (req.str("op") == "stop") {
                writer.println(respond(id, OK, 0, ""))
                writer.flush()
                return false
            }

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
                writer.println(respond(id, BAD_INPUT, 0,
                    "缺参数：file/out 都要给，timeout 必须为正"))
                writer.flush()
                return true
            }
            val started = System.currentTimeMillis()
            var error = ""
            val code = try {
                // main 的参数解析、深度白名单、真实校验和 results.jsonl 写入全部复用。
                ValidateService.main(args.toTypedArray())
                OK
            } catch (e: Throwable) {
                error = "${e::class.simpleName}: ${e.message?.take(240)}"
                System.err.println("[appservice] Validate daemon 执行异常: ${e.stackTraceStr()}")
                BAD_INPUT
            }
            val cost = System.currentTimeMillis() - started
            writer.println(respond(id, code, cost, error))
            writer.flush()
            println("[appservice] Validate daemon 请求完成：code=$code ${cost}ms")
            return true
        }
    }

    private fun respond(id: Long, code: Int, costMs: Long, error: String,
                        extra: Map<String, Any?> = emptyMap()): String =
        Json.encodeToString(
            JsonObject.serializer(),
            ServiceJson.toJsonObject(mapOf(
                "id" to id, "code" to code, "cost_ms" to costMs, "error" to error,
            ) + extra),
        )

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

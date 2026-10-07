package io.legado.app.service

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.InterruptedIOException
import java.net.ConnectException
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLException
import javax.script.ScriptException

/**
 * `classifyRoot` 的码表钉（不联网、不需要 Robolectric）。
 *
 * 异常串全是从真库抄的（`tests/test_jvm_health.py` 的 REAL_REASONS）——
 * `Socket closed` 与 `Connection reset` 同是 SocketException，只差一个词，
 * 一个该落 other（本地关掉的连接）一个该落 reset（被墙特征）。
 * 词表两侧逐词一致由 Python 侧的词表契约测试钉住；这条钉的是**分类本身**。
 */
class ValidateServiceClassifyRootTest {

    private fun kind(e: Throwable) = ValidateService.classifyRoot(e)

    @Test fun dns_by_type() =
        assertEquals("dns", kind(UnknownHostException("api.example.com")))

    @Test fun timeout_by_type_and_by_message() {
        assertEquals("timeout", kind(SocketTimeoutException("Connect timed out")))
        assertEquals("timeout", kind(InterruptedIOException("timeout")))
    }

    @Test fun reset_by_message_when_the_type_is_generic() =
        assertEquals("reset", kind(SocketException("Connection reset")))

    @Test fun socket_closed_is_not_reset() =
        assertEquals("other", kind(SocketException("Socket closed")))

    @Test fun connect_refused_is_connect() =
        assertEquals("connect", kind(ConnectException("Connection refused: getsockopt")))

    @Test fun ssl_by_class_name() =
        assertEquals("tls", kind(SSLException("Unable to parse TLS packet header")))

    @Test fun rule_by_type_and_by_message() {
        assertEquals("rule", kind(ScriptException(
            "org.mozilla.javascript.EcmaError: TypeError: Cannot read property \"1\" from null")))
        assertEquals("rule", kind(IllegalArgumentException("json string can not be null or empty")))
    }

    @Test fun self_for_engine_side_gaps() {
        assertEquals("self", kind(RuntimeException(ExceptionInInitializerError())))
        assertEquals("self", kind(OutOfMemoryError("Java heap space")))
    }

    @Test fun unclassifiable_is_other() =
        assertEquals("other", kind(IllegalStateException("weird state")))
    @Test fun webview_false_is_left_unchanged() {
        val rule = "https://example.com,{\"webView\":false,\"method\":\"GET\"}"
        assertEquals(rule to false, ValidateService.stripWebView(rule))
    }

    @Test fun webview_true_is_removed_and_reported() {
        assertEquals("https://example.com" to true,
            ValidateService.stripWebView("https://example.com,{\"webView\":true}"))
    }
}

package io.legado.app.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 引擎取回的整页 HTML 收集器（`DebugService.EngineHtmlCollector`）。
 *
 * **不联网、不跑 Robolectric**：它就是一段纯逻辑，没有理由省。
 *
 * 为什么要钉它：payload 事件里**带着整页 body**，而这条链以前按「与设备 WS 同构」
 * 把它整个丢掉——于是抽屉那份「整页源码」只能靠我们另抓一遍（另一条 HTTP 栈、
 * 没有登录态）。这份 HTML 是 App 手上那一份，收它的规则一旦写错，用户看到的
 * 会是**另一页**（比如详情页配上了目录页的 URL），而界面上完全看不出来。
 */
class EngineHtmlCollectorTest {

    private val 获取 = "≡获取成功:"
    private val 前缀 = "[00:01.234] "

    private fun feed(vararg items: Pair<String, Boolean>): Map<String, String> {
        val c = DebugService.EngineHtmlCollector(limit = 1000)
        for ((text, payload) in items) c.accept(text, payload)
        return c.result()
    }

    @Test
    fun payload_is_paired_with_the_url_right_before_it() {
        val out = feed(
            "⇒开始搜索关键字:我" to false,
            "︾开始解析搜索页" to false,
            前缀 + 获取 + "https://a.com/s?q=x" to false,
            "<html>搜索页</html>" to true,
            前缀 + 获取 + "https://a.com/book/1" to false,
            "<html>详情页</html>" to true,
        )
        assertEquals(setOf("https://a.com/s?q=x", "https://a.com/book/1"), out.keys)
        assertEquals("<html>搜索页</html>", out["https://a.com/s?q=x"])
        assertEquals("<html>详情页</html>", out["https://a.com/book/1"])
    }

    @Test
    fun the_same_url_keeps_the_first_copy() {
        // 详情页与目录页**常常同 URL**：后到的那条是同一页，别覆盖（也别把自己撑大）
        val out = feed(
            获取 + "https://a.com/book/1" to false, "第一份" to true,
            获取 + "https://a.com/book/1" to false, "第二份" to true,
        )
        assertEquals("第一份", out["https://a.com/book/1"])
    }

    @Test
    fun payload_without_a_url_is_dropped() {
        // 流里的第一条 payload 可能前面没有 `≡获取成功`（比如 key 直接给详情页）——
        // 宁可不收，也不要把它塞到上一轮的 URL 上
        assertTrue(feed("<html>来路不明</html>" to true).isEmpty())
    }

    @Test
    fun engine_html_updates_the_url_for_later_payloads_only() {
        // URL 行只影响**它之后**的 payload：配错方向就会把页面挂到下一页上
        val out = feed(
            获取 + "https://a.com/1" to false, "第一页" to true,
            获取 + "https://a.com/2" to false, "第二页" to true,
        )
        assertEquals("第一页", out["https://a.com/1"])
        assertEquals("第二页", out["https://a.com/2"])
    }

    @Test
    fun oversized_pages_are_truncated_with_a_note() {
        val c = DebugService.EngineHtmlCollector(limit = 20)
        c.accept(获取 + "https://a.com/big", false)
        c.accept("x".repeat(100), true)
        val html = c.result()["https://a.com/big"].orEmpty()
        assertTrue("要先留下前 20 个字符：$html", html.startsWith("x".repeat(20)))
        assertTrue("截断必须写明（否则会被当成整页）：$html", html.contains("已截断"))
        assertTrue("要写清原长：$html", html.contains("100"))
    }
}

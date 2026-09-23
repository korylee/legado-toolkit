package io.legado.app.service

import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 网络抓包（`BrowserBridge.networkRequests`）：从 CDP 事件流里挑出**这一页实际发过的接口请求**。
 *
 * 不联网、不起浏览器；**要 Robolectric**——它解析 CDP 消息用的是 `org.json`，而纯单测里
 * 那个类是 Android 的桩（`Method … not mocked`）。仓库里碰 JSON 的单测都走这一条
 * （`MatchedHtmlRuleTest` 那几个）；纯逻辑那几条（挑战判据、错误页）仍然不跑 Robolectric。
 *
 * 为什么值得钉：这是 L4 唯一的材料来源（2026-09-21 那一轮的结论：需要这条路的站，页面 HTML
 * 往往根本拿不到——403 / Cloudflare——而**浏览器真发出去的请求**地址、方法、body 都是现成的）。
 * 挑错形状的后果不是报错，是下游拿到半截（比如只有 URL 没有 body，于是 POST 接口被当成 GET
 * 用），然后按它去配规则。
 */
@RunWith(RobolectricTestRunner::class)
// 与仓库里其它 Robolectric 单测同一套配置：**把 application 换成空的那个**——
// App 自己的 `onCreate` 要初始化一堆东西（路径 / 资产），单测里没有那个环境。
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class BrowserBridgeNetworkTest {

    private fun request(id: String, url: String, type: String, method: String = "GET",
                        postData: String = ""): String =
        """{"method":"Network.requestWillBeSent","params":{"requestId":"$id","type":"$type",""" +
            """"request":{"url":"$url","method":"$method","postData":"$postData"}}}"""

    private fun response(id: String, status: Int, mime: String): String =
        """{"method":"Network.responseReceived","params":{"requestId":"$id",""" +
            """"response":{"status":$status,"mimeType":"$mime"}}}"""

    @Test
    fun only_xhr_and_fetch_are_kept() {
        val msgs = listOf(
            request("1", "https://a.com/api/list", "XHR"),
            response("1", 200, "application/json"),
            request("2", "https://a.com/style.css", "Stylesheet"),
            request("3", "https://a.com/img.jpg", "Image"),
            request("4", "https://a.com/page", "Document"),
            request("5", "https://a.com/f", "Fetch"),
        )
        val got = BrowserBridge.networkRequests(msgs)
        assertEquals(listOf("https://a.com/api/list", "https://a.com/f"),
                     got.map { it["url"] })
    }

    @Test
    fun method_and_body_are_carried_through() {
        // POST 接口**必须连着 body 一起拿**：光有地址的接口，下游会当 GET 用
        val got = BrowserBridge.networkRequests(listOf(
            request("1", "https://a.com/api/search", "XHR", "POST", "q=%E6%88%91")))
        assertEquals("POST", got[0]["method"])
        assertEquals("q=%E6%88%91", got[0]["post_data"])
    }

    @Test
    fun status_and_mime_come_from_the_response_event() {
        val got = BrowserBridge.networkRequests(listOf(
            request("1", "https://a.com/api/list", "XHR"),
            response("1", 200, "application/json")))
        assertEquals(200, got[0]["status"])
        assertEquals("application/json", got[0]["mime"])
    }

    @Test
    fun requests_without_a_response_are_still_reported_once() {
        val got = BrowserBridge.networkRequests(listOf(
            request("1", "https://a.com/api/list", "XHR"),
            request("1", "https://a.com/api/list", "XHR")))   // 同一 requestId 只算一条
        assertEquals(1, got.size)
        assertEquals(0, got[0]["status"])
    }

    @Test
    fun the_type_count_explains_an_empty_capture() {
        // 「一条都没抓到」有两种意思：这一页真没发接口，或抓包没生效。类型分布把它们分开
        // （与 `dropped_payload` 同一条纪律：看不见的事实要报出来）
        val msgs = listOf(request("1", "https://a.com/x", "XHR"),
                          request("2", "https://a.com/i.jpg", "Image"),
                          request("3", "https://a.com/i2.jpg", "Image"),
                          request("4", "https://a.com/p", "Document"))
        // 按集合比：同分的先后取决于稳定排序（插入序），那是实现细节；
        // 这里要钉的是**格式与计数**（`类型=条数`，逗号分隔）
        assertEquals(setOf("Image=2", "XHR=1", "Document=1"),
            BrowserBridge.networkTypeCount(msgs).split(",").toSet())
        // 注意 JUnit 三参数版是 (message, expected, actual)，message 放最后会静默换位
        assertEquals("没有请求事件就是空串", "", BrowserBridge.networkTypeCount(listOf("{}")))
    }

    @Test
    fun response_filter_reasons_distinguish_status_and_mime() {
        assertEquals("status", BrowserBridge.networkDropReason(0, "application/json"))
        assertEquals("status", BrowserBridge.networkDropReason(403, "application/json"))
        assertEquals("mime", BrowserBridge.networkDropReason(200, "image/png"))
        assertEquals(null, BrowserBridge.networkDropReason(200, "application/json"))
    }

    @Test
    fun the_event_count_counts_every_network_message() {
        val msgs = listOf(request("1", "https://a.com/x", "XHR"),
                          response("1", 200, "application/json"),
                          """{"method":"Network.loadingFinished","params":{}}""",
                          """{"method":"Page.loadEventFired","params":{}}""")
        assertEquals(3, BrowserBridge.networkEventCount(msgs))
    }

    @Test
    fun the_limit_is_honoured_and_garbage_is_ignored() {
        val msgs = (1..30).map { request(it.toString(), "https://a.com/$it", "XHR") } +
            listOf("不是 JSON", "{}")
        assertEquals(20, BrowserBridge.networkRequests(msgs).size)
        assertTrue("垃圾消息不该炸", BrowserBridge.networkRequests(listOf("不是 JSON")).isEmpty())
    }
}

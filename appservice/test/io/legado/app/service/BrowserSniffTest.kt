package io.legado.app.service

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.AfterClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.net.InetSocketAddress

/**
 * S2 桥级验证：`sourceRegex` / `overrideUrlRegex` 的嗅探（上游 `SnifferWebClient`）。
 *
 * 上游语义（`BackstageWebView.SnifferWebClient`）：两条正则匹配的是**加载过程中的 URL**，
 * 命中就把**那个 URL 本身**当响应体返回（`StrResponse(url!!, requestUrl)`），不是读响应体；
 * 而且两个回调管的范围不同——`overrideUrlRegex` 只管**导航**（`shouldOverrideUrlLoading`）、
 * `sourceRegex` 管**任何资源**（`onLoadResource`）。
 *
 * 这里用本地 HTTP 服务器当夹具：不联网、不碰设备，四种形态一次覆盖，外加两条**防护钉子**：
 * ① 没命中必须回落到页面结果（不许把整页换成别的）；
 * ② 渲染我们自己给的 HTML 时，那次合成导航**不许**参与 override 判据（上游 `loadDataWithBaseURL`
 *    根本不触发 `shouldOverrideUrlLoading`，判进去就是假命中）。
 *
 * 跑法：启动器**需要完整的 JVM 环境**（`LEGADO_REPO` 等，裸跑只会报
 * 「LEGADO_REPO is not set」），所以要走项目里注入环境那条路（`core.jvm_direct` 的
 * `jvm_env.process_environment`），按类名跑：
 *     :app:testAppDebugUnitTest --tests io.legado.app.service.BrowserSniffTest --rerun
 *
 * 两个实测坑：① 纯 JUnit 下 `org.json` 是 **not mocked**，必须挂 Robolectric；
 * ② 会真起浏览器，所以别在 daemon 正跑同一 profile 时并发跑。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class BrowserSniffTest {

    companion object {
        private lateinit var server: HttpServer
        private var port = 0
        private lateinit var profile: File

        private fun reply(ex: HttpExchange, body: String, type: String = "text/html; charset=utf-8") {
            val bytes = body.toByteArray(Charsets.UTF_8)
            ex.responseHeaders.add("Content-Type", type)
            ex.sendResponseHeaders(200, bytes.size.toLong())
            ex.responseBody.use { it.write(bytes) }
        }

        @BeforeClass
        @JvmStatic
        fun up() {
            server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
            server.createContext("/src") {
                reply(it, "<html><body><div id='mark'>PAGE-SRC</div>"
                    + "<img src='/want.json?x=1'></body></html>")
            }
            server.createContext("/want.json") { reply(it, "{}", "application/json") }
            server.createContext("/ovr") {
                reply(it, "<html><body><div id='mark'>PAGE-OVR</div>"
                    + "<script>location.href='/jump/target'</script></body></html>")
            }
            server.createContext("/jump") { reply(it, "<html><body>JUMPED</body></html>") }
            server.createContext("/plain") {
                reply(it, "<html><body><div id='mark'>PAGE-PLAIN</div></body></html>")
            }
            server.start()
            port = server.address.port
            // 与 BrowserSession 同一个默认 profile：已预热，省一次冷启动
            profile = File(System.getProperty("java.io.tmpdir"), "legado-appservice-profile")
        }

        @AfterClass
        @JvmStatic
        fun down() {
            server.stop(0)
        }
    }

    private fun url(path: String) = "http://127.0.0.1:$port$path"

    private fun withSession(block: (BrowserBridge.Session) -> Unit) {
        val (session, why) = BrowserBridge.launch(profile)
        if (session == null) {
            println("SNIFF: 启动浏览器失败 → $why")
            return
        }
        try {
            block(session)
        } finally {
            session.close()
        }
    }

    @Test
    fun source_regex_returns_the_matched_resource_url() {
        withSession { session ->
            val r = BrowserBridge.render(session, url("/src"), sourceRegex = ".*want\\.json.*")
            println("SNIFF: sourceRegex → ok=${r.ok} sniffed=${r.sniffed} body=${r.body.take(70)} reason=${r.reason}")
            assertTrue(r.ok)
            assertTrue("命中资源必须标成嗅探", r.sniffed)
            assertEquals(url("/want.json?x=1"), r.body)
        }
    }

    @Test
    fun override_url_regex_returns_the_navigated_url() {
        withSession { session ->
            val r = BrowserBridge.render(session, url("/ovr"), overrideUrlRegex = ".*jump.*")
            println("SNIFF: overrideUrlRegex → ok=${r.ok} sniffed=${r.sniffed} body=${r.body.take(70)}")
            assertTrue(r.ok)
            assertTrue(r.sniffed)
            assertEquals(url("/jump/target"), r.body)
        }
    }

    @Test
    fun no_match_falls_back_to_the_page() {
        withSession { session ->
            val r = BrowserBridge.render(session, url("/plain"), sourceRegex = ".*never-match-xyz.*")
            println("SNIFF: 未命中 → ok=${r.ok} sniffed=${r.sniffed} 长度=${r.body.length}")
            assertTrue(r.ok)
            assertFalse("没命中就不能标成嗅探", r.sniffed)
            assertTrue("必须回落到整页", r.body.contains("PAGE-PLAIN"))
        }
    }

    @Test
    fun invalid_regex_fails_explicitly() {
        withSession { session ->
            val r = BrowserBridge.render(session, url("/plain"), sourceRegex = "([unclosed")
            println("SNIFF: 无效正则 → ok=${r.ok} reason=${r.reason.take(80)}")
            assertFalse(r.ok)
            assertTrue("无效正则要显式报原因，不能静默跳过", r.reason.startsWith("sniff_regex_invalid"))
        }
    }

    @Test
    fun sniff_works_while_rendering_our_own_html() {
        withSession { session ->
            // 上游 `java.webViewGetSource(html, url, js, sourceRegex, …)` 就是这种形态
            val html = "<html><body><img src='/want.json?x=1'></body></html>"
            val r = BrowserBridge.renderContent(session, html, baseUrl = url("/base"),
                                               sourceRegex = ".*want\\.json.*")
            println("SNIFF: html 形态 → ok=${r.ok} sniffed=${r.sniffed} body=${r.body.take(70)}")
            assertTrue(r.ok)
            assertTrue(r.sniffed)
            assertEquals(url("/want.json?x=1"), r.body)
        }
    }

    @Test
    fun synthetic_navigation_of_our_own_html_never_matches_override() {
        withSession { session ->
            // `overrideUrlRegex = ".*"` 会匹配一切：若把渲染 html 时那次合成导航判进来，
            // 这里就会假命中。上游 `loadDataWithBaseURL` 不触发 `shouldOverrideUrlLoading`。
            val html = "<html><body><div id='mark'>CONTENT-MODE</div></body></html>"
            val r = BrowserBridge.renderContent(session, html, baseUrl = url("/base"),
                                               overrideUrlRegex = ".*")
            println("SNIFF: html + override 假命中防护 → ok=${r.ok} sniffed=${r.sniffed}")
            assertTrue(r.ok)
            assertFalse("渲染自有 html 时的合成导航不许参与 override 判据", r.sniffed)
        }
    }
}

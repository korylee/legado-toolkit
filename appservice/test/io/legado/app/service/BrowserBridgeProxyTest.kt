package io.legado.app.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 浏览器侧的代理参数（`BrowserBridge.proxyArg`）：App 的 OkHttp 与 App 的 WebView 是**两条
 * 取数栈**——前者认源 header 里的 `proxy` 键，后者是真浏览器、只认启动参数。只做前者的话，
 * L2–L4 的「换材料」在代理环境里仍然出不去，而用户只会觉得「代理配错了」。
 *
 * 不联网、不跑 Robolectric：纯参数拼装（同 `EngineHtmlCollectorTest`）。
 */
class BrowserBridgeProxyTest {

    @Test
    fun a_proxy_becomes_a_launch_argument() {
        assertEquals(listOf("--proxy-server=http://127.0.0.1:7890"),
            BrowserBridge.proxyArg("http://127.0.0.1:7890"))
    }

    @Test
    fun no_proxy_means_no_argument_at_all() {
        // **空 = 一个字都不加**：写一个空的 --proxy-server 会让浏览器把它当代理地址
        assertTrue(BrowserBridge.proxyArg("").isEmpty())
        assertTrue(BrowserBridge.proxyArg("   ").isEmpty())
    }
}

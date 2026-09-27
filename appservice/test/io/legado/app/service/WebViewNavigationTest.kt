package io.legado.app.service

import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * webView 导航的两道防线（TODO jvm-webview-nav）：
 *
 * ① **桥读导航应答**：CDP 对相对地址/无效地址的 `Page.navigate` 会回命令级 `error`
 *    （实测 2026-09-26：`--` 重跑的相对地址 + webView，原实现发完不读、等满 60s
 *    渲染预算才说 render_timeout，75.5s 才 fail）。判据 [BrowserBridge.navigationFailure]
 *    纯函数化——与 `networkRequests` 同一测法：喂 CDP 消息、不起浏览器。
 * ② **shadow 导航前补全相对地址**：`AnalyzeUrl` 的 baseUrl 在 `--` 重跑那类场景是空的，
 *    相对地址原样进桥；[ShadowBackstageWebView.resolveAgainstTag] 按 `tag`（源 URL）
 *    补全，一个咽喉覆盖重跑 / 正文翻页 / 目录分段。
 *
 * 要 Robolectric：两边都碰 `org.json` / `NetworkUtils`（纯单测里是 Android 桩），
 * 与 `BrowserBridgeNetworkTest` 同一套配置。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class WebViewNavigationTest {

    // ---------------------------------------------------------------- ① 桥侧判据

    @Test
    fun cdp_command_error_becomes_the_reason() {
        val nav = JSONObject(
            """{"id":2,"error":{"code":-32602,"message":"Cannot navigate to invalid URL"}}""")
        assertEquals("Cannot navigate to invalid URL",
                     BrowserBridge.navigationFailure(nav))
    }

    @Test
    fun accepted_navigation_returns_null() {
        assertNull(BrowserBridge.navigationFailure(
            JSONObject("""{"id":2,"result":{"frameId":"F"}}""")))
        // 应答里没有 error 键就是受理——别把 result.errorText 判进来（ERR_ABORTED
        // 会误杀「马上被 JS 重定向」的正常页，判据注释里写了）
        assertNull(BrowserBridge.navigationFailure(
            JSONObject("""{"id":2,"result":{"frameId":"F","errorText":"ERR_ABORTED"}}""")))
    }

    @Test
    fun missing_response_is_a_failure_not_a_hang() {
        assertEquals("Page.navigate 无响应", BrowserBridge.navigationFailure(null))
    }

    // ---------------------------------------------------------------- ② shadow 解析

    @Test
    fun relative_url_is_resolved_against_the_tag() {
        assertEquals("https://www.koudaimh.com/manhua/a/74.html",
                     ShadowBackstageWebView.resolveAgainstTag(
                         "/manhua/a/74.html", "https://www.koudaimh.com"))
    }

    @Test
    fun absolute_url_passes_through() {
        val url = "https://www.koudaimh.com/manhua/a/74.html"
        assertEquals(url, ShadowBackstageWebView.resolveAgainstTag(url, "https://x.com"))
    }

    @Test
    fun blank_tag_leaves_the_url_alone() {
        // tag 拿不到时原样返回——那种地址由桥侧「导航失败立刻报」接住，不静默
        assertEquals("/manhua/a/74.html",
                     ShadowBackstageWebView.resolveAgainstTag("/manhua/a/74.html", ""))
        assertEquals("/manhua/a/74.html",
                     ShadowBackstageWebView.resolveAgainstTag("/manhua/a/74.html", null))
    }

    @Test
    fun fragment_only_urls_do_not_lose_the_base() {
        // 站点常见的锚点/查询形态：补全后基地必须还在
        val got = ShadowBackstageWebView.resolveAgainstTag(
            "?page=2", "https://www.koudaimh.com/list/")
        assertTrue(got, got.startsWith("https://www.koudaimh.com"))
    }
}

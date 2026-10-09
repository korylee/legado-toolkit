package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.help.source.SourceVerificationHelp
import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * `ShadowSourceVerification` 的两件事，分开钉：
 *
 * ① **入口拦得住、且立刻失败**——这条是这次故障的正面判据：`useBrowser=false`（图片验证码）
 *   走真实现会 `LockSupport.parkNanos` 无限等，走 shadow 必须**毫秒级抛**。
 *   `@Test(timeout=...)` 是判据的一部分：拦不住就会挂到超时。
 * ② **渲染结果的策略**（纯函数，不起浏览器）：哪些页面算"不能用"，理由怎么说。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class, ShadowSourceVerification::class])
class ShadowSourceVerificationTest {

    /** 实测样本（同 `BrowserBridgeChallengeTest`，2026-09-21 引擎取回的那份）。 */
    private val cdnChallenge = """
        <html lang="en-US" dir="ltr"><head><title>请稍候…</title></head>
        <body><h1>18read.net</h1><h2 class="YNaX0">正在进行安全验证</h2>
        <script>window._cf_chl_opt = {cRay: 'abc'};</script>
        </body></html>
    """.trimIndent()

    @Before
    fun reset() {
        ShadowSourceVerification.reset()
    }

    @Test(timeout = 20_000)
    fun captchaPathFailsFastInsteadOfParking() {
        val started = System.currentTimeMillis()
        var thrown: Throwable? = null
        try {
            SourceVerificationHelp.getVerificationResult(
                BookSource(bookSourceUrl = "https://spike.test/"),
                "https://spike.test/captcha.png", "验证码", false, true, null)
        } catch (e: Throwable) {
            thrown = e
        }
        val costMs = System.currentTimeMillis() - started

        assertTrue("图片验证码那条路必须抛，不许 park（真实现是 LockSupport.parkNanos 无限等）",
            thrown is BrowserRequiredException)
        assertEquals(ShadowSourceVerification.CAPTCHA_REASON, thrown?.message)
        assertEquals(1, ShadowSourceVerification.calls.get())
        assertEquals(ShadowSourceVerification.MODE_CAPTCHA,
            ShadowSourceVerification.lastMode.get())
        assertTrue("必须立刻返回（park 只能等测试超时），实耗 ${costMs}ms", costMs < 5_000)
    }

    @Test
    fun renderingPolicyTurnsEveryBadPageIntoAReason() {
        // 渲染本身失败：原样带出渲染器的原因（别把具体原因换成一句笼统话）
        assertEquals("Page.navigate 无响应", ShadowSourceVerification.renderFailure(
            false, "", "Page.navigate 无响应", "https://a/"))
        // 失败但没原因：给一个兜底，也不许是空串
        assertEquals("浏览器渲染失败", ShadowSourceVerification.renderFailure(
            false, "", "", "https://a/"))
        assertEquals("浏览器渲染出空页面", ShadowSourceVerification.renderFailure(
            true, "   ", "", "https://a/"))
        // 挑战页**必须算失败**：拿它当正文，源那边只会得到 no_result（看着像源坏了）
        assertTrue(ShadowSourceVerification.renderFailure(
            true, cdnChallenge, "", "https://a/")!!.contains("挑战"))
        assertTrue(ShadowSourceVerification.renderFailure(
            true, "<html><body>main-frame-error</body></html>", "",
            "chrome-error://chromewebdata/")!!.contains("打不开"))
        // 正常页放行
        assertNull(ShadowSourceVerification.renderFailure(
            true, "<html><body>正常页</body></html>", "", "https://a/"))
    }
}

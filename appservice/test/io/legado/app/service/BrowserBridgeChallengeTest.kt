package io.legado.app.service

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 挑战页识别（`BrowserBridge.isChallenge`）：**它只决定「要不要再等一次导航」**。
 *
 * 为什么不联网、不跑 Robolectric：这是一段纯判断（拿一份 HTML 看有没有那几个词），
 * 没有理由省（同 `EngineHtmlCollectorTest`）。
 *
 * 为什么值得钉：判错的代价是**每个正常页白等 8 秒**（假阳性），或者挑战页永远取在
 * 「请稍候…」那一刻（假阴性，下游会按拦截页判层、写规则——2026-09-21 实测的正是这一支）。
 * 那两组反例（`请稍候` / `正在加载图片，请稍候`）来自小爱漫画的**正常**章节页：
 * 正文里 `请稍候` 出现 60 次，所以它**不能**进特征词表。
 */
class BrowserBridgeChallengeTest {

    /** 实测样本（2026-09-21，引擎取回来的那两份，`data/out/interstitial_*.html`）。 */
    private val cdnChallenge = """
        <html lang="en-US" dir="ltr"><head><title>请稍候…</title></head>
        <body><h1>18read.net</h1><h2 class="YNaX0">正在进行安全验证</h2>
        <script>window._cf_chl_opt = {cRay: 'abc'};</script>
        <script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
        </body></html>
    """.trimIndent()

    private val blocked = """
        <html><head><title>Attention Required! | Cloudflare</title></head>
        <body><div class="cf-error-details cf-wrapper">Please enable cookies.</div>
        <!-- /.captcha-container --></body></html>
    """.trimIndent()

    /** 正常页：这两份都**不该**被判成挑战页。 */
    private val normalPage =
        "<html><body><div id='chapter-images'><div class='chapter-message'>正在加载图片，请稍候</div>" +
            "<div class='chapter-message'>正在加载图片，请稍候</div><img><img></div></body></html>"

    @Test
    fun cdn_challenge_page_is_recognized() {
        assertTrue("Cloudflare 的 JS 挑战页要认出来（否则永远取在「请稍候…」那一刻）",
            BrowserBridge.isChallenge(cdnChallenge))
    }

    @Test
    fun cloudflare_block_page_is_recognized() {
        assertTrue("拦截页同样要认出来（这类等也过不去，交回去由下游判）",
            BrowserBridge.isChallenge(blocked))
    }

    @Test
    fun a_normal_page_that_says_please_wait_is_not_a_challenge() {
        assertFalse("`请稍候` 是通用词：正常漫画页里出现 60 次，拿它判会让每页白等 8 秒",
            BrowserBridge.isChallenge(normalPage))
        assertFalse(BrowserBridge.isChallenge(""))
        assertFalse(BrowserBridge.isChallenge("<html><body>甲</body></html>"))
    }
}

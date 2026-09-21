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

    @Test
    fun the_apps_own_probe_wins_when_the_html_looks_clean() {
        // Cloudflare 挑战期：页面里可能一个特征词都没有，但 App 那句 `!!window._cf_chl_opt`
        // 是页面里的**真实状态**——它说还在过，就得接着等
        assertTrue("App 那句为真时必须等（哪怕 HTML 里没有词）",
            BrowserBridge.isChallenge(normalPage, "true"))
        assertTrue(BrowserBridge.isChallenge("", "true"))
    }

    @Test
    fun a_false_probe_falls_back_to_the_word_list() {
        assertFalse(BrowserBridge.isChallenge(normalPage, "false"))
        assertFalse(BrowserBridge.isChallenge(normalPage, ""))
        // 非 CF 的验证码墙：App 那句为假，但词表命中 → 仍要等
        assertTrue(BrowserBridge.isChallenge(cdnChallenge, "false"))
    }

    /** 浏览器自己的错误页（实测 2026-09-22：banxia.cc 返回 Edge 的「无法访问此页面」，317KB）。 */
    private val browserErrorPage = """
        <html dir="ltr" lang="zh"><head><title>www.banxia.cc</title></head>
        <body id="t"><div id="main-frame-error" class="neterror">
        <span class="error-code">ERR_CONNECTION_CLOSED</span></div></body></html>
    """.trimIndent()

    @Test
    fun a_browser_error_page_is_not_a_site() {
        assertTrue("错误页的 DOM 要认出来（它一个字的站点内容都没有）",
            BrowserBridge.isBrowserError("https://www.banxia.cc/", browserErrorPage))
    }

    @Test
    fun the_landing_url_gives_it_away_even_without_the_marker() {
        assertTrue("落地地址是 chrome-error:// 就是错误页（不依赖错误页文案的语言）",
            BrowserBridge.isBrowserError("chrome-error://chromewebdata/", normalPage))
    }

    @Test
    fun normal_pages_and_challenge_pages_are_not_browser_errors() {
        assertFalse(BrowserBridge.isBrowserError("https://a.com/", normalPage))
        assertFalse(BrowserBridge.isBrowserError("https://a.com/", cdnChallenge))
    }
}

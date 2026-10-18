package io.legado.app.service

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 登录墙识别的语义（A3）。**词表本身**的跨语言一致性由 Python 侧那条契约测试守
 * （`tests/test_jvm_debug_contract.py` 逐词比对 `core/models.py: LOGIN_MARKERS`），
 * 这里只钉匹配语义——两侧都靠 `lowercase` + `contains`，别一边改成别的。
 *
 * 不需要 Robolectric：纯字符串匹配。
 */
class SourceCookiesTest {

    @Test
    fun login_marker_hits_are_case_insensitive() {
        assertEquals("请登录", SourceCookies.loginMarkerOf("<div>请登录后继续</div>"))
        assertEquals("login required", SourceCookies.loginMarkerOf("LOGIN REQUIRED"))
        assertEquals("please log in", SourceCookies.loginMarkerOf("Please Log In to continue"))
    }

    @Test
    fun normal_page_has_no_marker() {
        assertEquals("", SourceCookies.loginMarkerOf("<html><body>目录 共 120 章</body></html>"))
        assertEquals("", SourceCookies.loginMarkerOf(""))
        assertEquals("", SourceCookies.loginMarkerOf(null))
    }

    @Test
    fun anti_bot_marker_hits_and_clean_pages_miss() {
        // 词表两侧一致性在 Python 契约测试（TestAntiBotMarkerParity）；这里钉匹配语义
        assertEquals("cf-challenge", SourceCookies.antiBotMarkerOf("/cdn-cgi/l/chk_jschl?__cf_chl_tk=x"))
        assertEquals("验证码", SourceCookies.antiBotMarkerOf("请输入验证码后继续"))
        assertEquals("captcha", SourceCookies.antiBotMarkerOf("CAPTCHA required"))
        // 正常页与 Cloudflare 邮箱保护脚本都不许命中（裸 cloudflare 不在表里）
        assertEquals("", SourceCookies.antiBotMarkerOf("<html><body>目录 共 120 章</body></html>"))
        assertEquals("", SourceCookies.antiBotMarkerOf(
            "src=\"/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js\""))
        assertEquals("", SourceCookies.antiBotMarkerOf(null))
    }
}

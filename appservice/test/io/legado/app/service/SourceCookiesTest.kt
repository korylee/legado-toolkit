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
}

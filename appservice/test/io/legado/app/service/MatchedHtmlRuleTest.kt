package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.RuleData
import org.jsoup.nodes.Element
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 命中回填的取值口径（第三期）：**末段是取值动作的规则，`getElements` 直接跑恒取空**。
 *
 * 为什么要有这条：`matched_html` 就是拿 `getElements` 算的，而正文规则末段是
 * `@html` / `@text` 这类动作的**占了七成以上**（实测在 lessons §七十五）——不处理
 * 它们，「命中源码」在正文段上就一直是空的，而那是最需要看 DOM 的一段。
 *
 * 两条断言分工：① 用 App 自己的 `AnalyzeRule` 钉住「带动作就取不到元素」这个事实
 * （**不联网**，几毫秒——这段语义是上游的，改了就该红）；② 钉住去掉末段之后的取法。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [ShadowBackstageWebView::class])
class MatchedHtmlRuleTest {

    private val html = """
        <div id="nr1"><p>第一段</p><p>第二段</p></div>
        <ul class="mulu"><li><a href="/1.html">第1页</a></li></ul>
    """.trimIndent()

    private fun analyze(rule: String): List<Any> {
        val source = BookSource(bookSourceUrl = "http://example.com",
            bookSourceName = "测试源")
        val analyzeRule = AnalyzeRule(RuleData(), source)
        analyzeRule.setContent(html, "http://example.com/1.html")
        return analyzeRule.getElements(rule)
    }

    @Test
    fun a_trailing_action_makes_get_elements_return_nothing() {
        // App 的 `getElements` 把每个 `@` 段都当选择器 → `html` 被当成「再选一个
        // html 元素」→ 恒空。这正是命中回填要走兜底的原因
        assertTrue("带取值动作的规则不该取到元素（上游语义变了就要回来改兜底）",
            analyze("id.nr1@tag.p@html").isEmpty())
    }

    @Test
    fun dropping_the_trailing_action_gives_the_nodes_the_app_reads() {
        val part = DebugService.elementsPartOf("id.nr1@tag.p@html")
        assertEquals("id.nr1@tag.p", part)
        val nodes = analyze(part!!)
        assertEquals(2, nodes.size)
        assertEquals("<p>第一段</p>", (nodes[0] as Element).outerHtml())
    }

    @Test
    fun the_replace_regex_is_not_part_of_the_elements_part() {
        assertEquals("id.nr1@tag.p",
            DebugService.elementsPartOf("id.nr1@tag.p@html##.*广告.*"))
        // 带 `##替换` 的这种形态，原样跑那次给不出东西（实测在真页面上是抛
        // `SelectorParseException`）——所以命中回填不跑它，直接用元素部分
        val direct = runCatching { analyze("id.nr1@tag.p@html##.*广告.*") }
        assertTrue("原样跑那次本该拿不到元素（拿得到就该重新想兜底）",
            direct.getOrNull().isNullOrEmpty())
    }

    @Test
    fun list_rules_and_unknown_tails_are_left_alone() {
        // 末段是选择器：不去猜（`tag.a` 是选择器）
        assertNull(DebugService.elementsPartOf("class.mulu@tag.a"))
        // 末段是属性名：属性与标签名同形（`title` 既是标签也是属性），猜不了 — AGENTS #21
        assertNull(DebugService.elementsPartOf("id.nr1@tag.p@title"))
        // 单段规则：末段就是选择器
        assertNull(DebugService.elementsPartOf("class.mulu"))
        // 整个规则就是一段 JS
        assertNull(DebugService.elementsPartOf("<js>result</js>"))
    }
}

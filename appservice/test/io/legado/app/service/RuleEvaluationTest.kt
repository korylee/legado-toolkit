package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.RuleData
import kotlinx.coroutines.runBlocking
import org.jsoup.nodes.Element
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** 固定 HTML 上直接通过 App 的规则解释器执行列表与取值规则，不发网络请求。 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35])
class RuleEvaluationTest {

    private val html = """
        <ul class="books">
          <li><a href="/one">第一本</a></li>
          <li><a href="/two">第二本</a></li>
        </ul>
    """.trimIndent()

    private fun source() = BookSource(
        bookSourceUrl = "http://example.com", bookSourceName = "测试源")

    private fun analyzer(): AnalyzeRule = AnalyzeRule(RuleData(), source())
        .setContent(html, "http://example.com/search")

    @Test
    fun list_rules_return_app_elements_and_text_values() {
        val rule = analyzer()
        val elements = rule.getElements("class.books@tag.li")
        assertEquals(2, elements.size)
        assertEquals("<li><a href=\"/one\">第一本</a></li>",
            (elements[0] as Element).outerHtml())
        assertEquals(listOf("第一本", "第二本"),
            rule.getStringList("class.books@tag.li@tag.a@text"))
    }

    @Test
    fun shared_evaluator_returns_bounded_values_and_errors_explicitly() = runBlocking {
        val evaluated = RuleEvaluation.evaluate(
            source(), html, "http://example.com/search",
            "class.books@tag.li@tag.a@text", "list", 1)
        assertEquals(2, evaluated["count"])
        assertEquals(true, evaluated["truncated"])
        assertEquals(listOf("第一本"), evaluated["values"])
    }

    @Test
    fun attribute_and_replacement_rules_use_the_app_value_path() {
        val rule = analyzer()
        assertEquals("http://example.com/one",
            rule.getString("class.books@tag.li.0@tag.a@href", isUrl = true))
        assertTrue(rule.getStringList("class.books@tag.li@tag.a@text##本##册")!!
            .contains("第一册"))
    }
}

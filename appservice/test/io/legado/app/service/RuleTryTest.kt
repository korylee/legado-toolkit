package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 轻量验证（RuleTry）的离线契约：固定 HTML 上钉住「单值 / 列表 / 空规则 /
 * 坏选择器」四种结果的形状。与 Python 侧 `core.rule_try._gate`（tests/
 * test_rule_try_contract.py）的消费对齐——失败不抛、原因进 error（AGENTS #4）。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class RuleTryTest {

    private val infoHtml = """
        <div class="title">甲书</div>
        <div class="author">作者A</div>
    """.trimIndent()
    private val tocHtml = """
        <ul class="chapter"><li><a href="/c/1">第1页</a></li><li><a href="/c/2">第2页</a></li></ul>
    """.trimIndent()

    private fun source() = BookSource(
        bookSourceUrl = "http://example.com",
        bookSourceName = "测试源",
    )

    @Test
    fun singleValueRuleHits() {
        val r = RuleTry.run(source(), infoHtml, "http://example.com",
            "bookUrl", "class.title@text")
        assertTrue(r["ok"] as Boolean)
        assertEquals(listOf("甲书"), r["values"])
        assertEquals(1, r["count"])
        assertEquals("", r["error"])
    }

    @Test
    fun listRuleCountsElements() {
        val r = RuleTry.run(source(), tocHtml, "http://example.com",
            "toc", "class.chapter@tag.a")
        assertTrue(r["ok"] as Boolean)
        assertEquals(2, r["count"])
        assertEquals(listOf("第1页", "第2页"), r["values"])
    }

    @Test
    fun blankRuleSaysSoInsteadOfThrowing() {
        val r = RuleTry.run(source(), infoHtml, "http://example.com", "bookUrl", "")
        assertFalse(r["ok"] as Boolean)
        assertTrue((r["error"] as String).contains("规则为空"))
    }

    @Test
    fun brokenSelectorFailsWithReasonNotException() {
        val r = RuleTry.run(source(), infoHtml, "http://example.com",
            "bookUrl", "class.不存在@text")
        assertFalse(r["ok"] as Boolean)
        // 具体措辞跟 AnalyzeRule 的分支走（选中 0 条 vs 选中但无文本），钉「不抛、有原因」
        assertTrue("失败原因要具体，不是笼统的报错（AGENTS #4）：${r["error"]}",
            (r["error"] as String).contains("取到"))
    }

    @Test
    fun searchStepUsesTheGivenRule() {
        // rule 必须被使用：不选中的规则必须 ok=false——原先这里跑的是源声明的
        // bookList（analyzeBookList 不收外部规则），坏提案也会报 ok（AGENTS #4）
        val r = RuleTry.run(source(), tocHtml, "http://example.com",
            "search", "class.不存在@tag.a")
        assertFalse(r["ok"] as Boolean)
        assertTrue((r["error"] as String).isNotBlank())
    }

    @Test
    fun searchStepListRuleSelectsItems() {
        val r = RuleTry.run(source(), tocHtml, "http://example.com",
            "search", "class.chapter@tag.a")
        assertTrue(r["ok"] as Boolean)
        assertEquals(2, r["count"])
    }

    @Test
    fun unknownStepIsRejected() {
        val r = RuleTry.run(source(), infoHtml, "http://example.com", "elsewhere", "text")
        assertFalse(r["ok"] as Boolean)
        assertTrue((r["error"] as String).contains("未知步骤"))
    }
}

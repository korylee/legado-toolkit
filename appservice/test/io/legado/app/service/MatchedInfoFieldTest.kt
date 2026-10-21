package io.legado.app.service

import io.legado.app.data.entities.BookSource
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 详情段命中回填的**字段表**契约（不联网、不挂 Robolectric 的纯映射部分）：
 *
 * - `bookUrl` 段按 `bookUrl.<字段>` 子键逐字段记（详情段是 `ruleBookInfo.*`
 *   字段规则族，单条 DOM 装不下）；字段名与 Python 侧
 *   `core.app_debug.BOOKINFO_FIELD_LABELS` 逐词比对钉在
 *   `tests/test_jvm_debug_contract.py::TestMatchedStepNameParity`；
 * - 空规则字段不出现在字段表里——没配的规则不该被记成「命中为空」。
 *
 * 命中 DOM 本身怎么算（`hitOf` / `elementsPartOf`）由 `MatchedHtmlRuleTest` 覆盖。
 */
class MatchedInfoFieldTest {

    private fun fullSource() = BookSource(
        bookSourceUrl = "http://example.com",
        bookSourceName = "测试源",
        ruleBookInfo = io.legado.app.data.entities.rule.BookInfoRule(
            name = "class.title@text", author = "class.author@text",
            kind = "class.kind@text", wordCount = "class.count@text",
            lastChapter = "class.last@text", intro = "class.intro@text",
            coverUrl = "class.cover img@src", tocUrl = "class.list a@href"),
    )

    @Test
    fun field_table_lists_all_configured_fields() {
        val fields = DebugService.MATCHED_INFO_FIELDS
            .map { it to DebugService.matchedInfoRuleOf(fullSource(), it) }
        assertEquals(
            listOf(
                "name", "author", "kind", "word_count", "last_chapter",
                "intro", "cover_url", "toc_url"),
            fields.map { it.first })
        assertTrue(fields.all { (_, rule) -> rule.isNotBlank() })
    }

    @Test
    fun unconfigured_fields_return_blank_rule() {
        val source = BookSource(bookSourceUrl = "http://example.com",
            bookSourceName = "测试源",
            ruleBookInfo = io.legado.app.data.entities.rule.BookInfoRule(
                name = "class.title@text"))
        assertEquals("class.title@text", DebugService.matchedInfoRuleOf(source, "name"))
        assertEquals("没配的字段返回空串——collectMatched 靠它跳过，不记「命中为空」",
            "", DebugService.matchedInfoRuleOf(source, "kind"))
    }
}

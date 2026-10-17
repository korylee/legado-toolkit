package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.probe.WindowsPathAssetManagerShadow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 结构化结果采集的离线契约（不联网）：固定 HTML 上钉住每段的字段语义与形状，
 * 与 Python 侧 `core/app_debug.structured_map` / 前端 `debugStepResult` 的消费对齐。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class StructuredResultTest {

    private val listHtml = """
        <ul class="books">
          <li><a href="/one">甲书</a><span class="author">作者A</span></li>
          <li><a href="/two">乙书</a><span class="author">作者B</span></li>
        </ul>
    """.trimIndent()
    private val infoHtml = """
        <div class="title">甲书</div>
        <div class="author">作者A</div>
        <div class="intro">一段简介</div>
        <a class="list" href="/book/1">目录</a>
    """.trimIndent()
    private val tocHtml = """
        <ul class="chapter"><li><a href="/c/1">第1页</a></li><li><a href="/c/2">第2页</a></li></ul>
    """.trimIndent()
    private val contentHtml = "<div id=\"nr1\"><p>第一段</p><p>第二段</p></div>"

    private fun source() = BookSource(
        bookSourceUrl = "http://example.com",
        bookSourceName = "测试源",
        searchUrl = "http://example.com/search?q={{key}}",
        ruleSearch = io.legado.app.data.entities.rule.SearchRule(
            bookList = "class.books@tag.li",
            bookUrl = "tag.a@href", name = "tag.a@text", author = "class.author@text"),
        ruleBookInfo = io.legado.app.data.entities.rule.BookInfoRule(
            name = "class.title@text", author = "class.author@text",
            intro = "class.intro@text", tocUrl = "class.list@tag.a@href"),
        ruleToc = io.legado.app.data.entities.rule.TocRule(
            chapterList = "class.chapter@tag.a", chapterName = "text"),
        ruleContent = io.legado.app.data.entities.rule.ContentRule(
            content = "id.nr1@html"),
    )

    private fun pages() = mapOf(
        "http://e/search" to DebugService.EnginePage(
            listHtml.take(4), truncated = true, originalLength = listHtml.length,
            fullBody = listHtml),
        "http://e/info" to DebugService.EnginePage(infoHtml, false, infoHtml.length, infoHtml),
        "http://e/toc" to DebugService.EnginePage(tocHtml, false, tocHtml.length, tocHtml),
        "http://e/c1" to DebugService.EnginePage(contentHtml, false, contentHtml.length, contentHtml),
    )

    private fun stepUrls() = mapOf(
        "search" to "http://e/search", "bookUrl" to "http://e/info",
        "toc" to "http://e/toc", "content" to "http://e/c1",
    )

    @Test
    fun search_step_collects_full_book_list_not_just_first_item() = runBlocking {
        // `BookList.analyzeBookList` 走 App 的 Debug.log（要 appCtx/Koin）——离线跑先起环境
        ValidateService.ensureStarted()
        val out = StructuredResult.collectSuspend(source(), pages(), stepUrls(), "甲")
        val search = (out["steps"] as? Map<*, *>)?.get("search") as? Map<*, *>
        if (search == null) {
            // 失败也要带着原因走（AGENTS #4）：看到 root 原因才能修
            throw AssertionError("search 段没记成；fails=" + (out["fails"] ?: "无"))
        }
        assertEquals(2, search["total"])
        val items = search["items"] as List<*>
        // 列表项瘦身（Agent 复核只需要定位字段）：名称/作者在，简介不在
        assertEquals(listOf("甲书", "乙书"), items.map { (it as Map<*, *>)["name"] })
        val first = items[0] as Map<*, *>
        assertEquals("作者A", first["author"])
        assertTrue("intro 不进列表项（侧车体积）", !first.containsKey("intro"))
    }

    @Test
    fun info_and_toc_steps_use_their_own_field_rules() = runBlocking {
        val out = StructuredResult.collectSuspend(source(), pages(), stepUrls(), "甲")
        val steps = out["steps"] as Map<*, *>
        val info = steps["bookUrl"] as Map<*, *>
        assertEquals("甲书", info["name"])
        assertEquals("一段简介", info["intro"])
        // URL 字段按 redirectUrl 绝对化（详情页相对路径不原样透出）
        assertEquals("http://e/book/1", info["toc_url"])
        val toc = steps["toc"] as Map<*, *>
        // chapterList 是列表规则（AGENTS #21）：按条目取，名称用 chapterName（兜底 text）
        assertEquals(listOf("第1页", "第2页"), toc["chapter_list"])
    }

    @Test
    fun explore_step_passes_no_key_into_the_rule_context() = runBlocking {
        ValidateService.ensureStarted()
        // explore 模式的入口 key 是 `发现::URL` 整串，不能进 {{key}}（App 的
        // exploreBookAwait 传 key = null）——否则规则算出另一份结果
        val out = StructuredResult.collectSuspend(source(), pages(),
            stepUrls() + ("explore" to "http://e/search"), "发现::http://e/x")
        val explore = (out["steps"] as Map<*, *>)["explore"] as Map<*, *>
        assertEquals("列表解析不依赖 {{key}}，照常出 2 条", 2, explore["total"])
    }

    @Test
    fun truncated_sidecar_body_still_parses_from_full_body() = runBlocking {
        ValidateService.ensureStarted()
        // search 页侧车只留 4 字符（truncated = true），完整版在 fullBody——
        // 解析必须走完整版，截断版只会产出假空结果
        val out = StructuredResult.collectSuspend(source(), pages(), stepUrls(), "甲")
        val steps = out["steps"] as Map<*, *>
        assertTrue("侧车截断不该挡住解析", steps.containsKey("search"))
        assertEquals(2, (steps["search"] as Map<*, *>)["total"])
    }

    @Test
    fun empty_step_urls_produce_nothing_and_version_is_stamped() = runBlocking {
        val out = StructuredResult.collectSuspend(source(), pages(), emptyMap())
        assertTrue("没有页就没有结构化结果", out.isEmpty())
        val withPages = StructuredResult.collectSuspend(source(), pages(), stepUrls(), "甲")
        assertEquals(StructuredResult.SCHEMA_VERSION, withPages["v"])
        assertTrue((withPages["fails"] as? List<*>) != null)
    }
}

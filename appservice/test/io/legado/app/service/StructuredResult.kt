package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.data.entities.rule.BookInfoRule
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.AnalyzeRule.Companion.setCoroutineContext
import io.legado.app.model.analyzeRule.RuleData
import io.legado.app.model.webBook.BookList
import io.legado.app.model.analyzeRule.AnalyzeUrl
import kotlinx.coroutines.runBlocking
import kotlin.coroutines.coroutineContext

/**
 * 引擎侧的**结构化结果采集**：在调试事件流跑完之后，用引擎已取回的整页 +
 * App 自己的解析器，把每一步的字段值算成 `{段名: {字段: 值}}` 写进侧车（
 * `structured` 键，与 `matched_html` 平级，不进 NDJSON——那份流与设备 WS 同构）。
 *
 * 为什么要它：事件行（`┌获取X/└值`）的记账边界由上游决定且不对称（搜索段只有
 * 首条书记全字段、目录段只有一行样本），字符串截取永远在追上游的记账习惯；
 * 结构化结果由**我们自己按同一套解析器**算，字段集固定、条数完整——它是
 * Agent 验收（第二样本复核）的材料接口。
 *
 * 失败纪律同命中回填：任何一段失败只记进 `fails`，不带走已经跑完的调试结果。
 */
internal object StructuredResult {

    /** 侧车 `structured` 的单段条数上限：搜索段全量书目对 Agent 复核是材料，对侧车是体积。 */
    const val LIST_LIMIT = 20

    /** 单字段值的落盘上限：简介可能几千字，正文样本截到可读即可。 */
    const val VALUE_MAX = 2000

    /** 段名 = `core/app_debug.SEGMENT_NAMES` 的取值（逐词比对钉在 TestStructuredParity）。 */
    val STEP_NAMES = listOf("search", "explore", "bookUrl", "toc", "content")

    fun collect(
        source: BookSource,
        enginePages: Map<String, DebugService.EnginePage>,
        stepUrls: Map<String, String>,
        searchKey: String = "",
    ): Map<String, Any> = runBlocking {
        collectSuspend(source, enginePages, stepUrls, searchKey)
    }

    suspend fun collectSuspend(
        source: BookSource,
        enginePages: Map<String, DebugService.EnginePage>,
        stepUrls: Map<String, String>,
        searchKey: String = "",
    ): Map<String, Any> {
        val fails = mutableListOf<String>()
        val out = LinkedHashMap<String, Any>()
        // 每页只跑一次解析：search/explore 共用 bookList（同 DebugService.matchedRuleOf 的口径），
        // 按段各自的 url 取页——同一个 URL 记两个段名，取值只算一遍。
        for (step in STEP_NAMES) {
            val page = stepUrls[step]?.let { url -> enginePages[url] }
            if (page == null || page.body.isBlank()) continue
            // 侧车截断 ≠ 材料没了：解析用完整 body（MAX_PARSE_CHARS 内）。
            // 连完整版都没有（超内存上限）才记 fails——原因要走到用户眼前（AGENTS #4）。
            val body = page.bodyForParse()
            if (body.isBlank()) {
                fails.add("$step 段整页 ${page.originalLength} 字符，超过解析内存上限，结构化材料没采")
                continue
            }
            try {
                val result = stepResult(source, body, stepUrls[step] ?: "", step, searchKey)
                if (result.isNotEmpty()) out[step] = result
            } catch (e: Throwable) {
                val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
                fails.add("$step 段结构化采集没跑成（${root::class.simpleName}: ${root.message?.take(80)}）")
            }
        }
        return if (out.isEmpty() && fails.isEmpty()) emptyMap()
        else linkedMapOf("steps" to out, "v" to SCHEMA_VERSION, "fails" to fails)
    }

    // 2：toc 段从 {chapter_list} 扩成 +total/truncated（2026-10-19）——字段集变化，
    // 旧侧车里没有这两键，版本轴分得出口径（AGENTS #5b）。
    const val SCHEMA_VERSION = 2

    /** 段名 → 字段值 map。空 map = 该段没有可记的字段（规则为空或页面为空）。 */
    private suspend fun stepResult(
        source: BookSource,
        body: String,
        baseUrl: String,
        step: String,
        searchKey: String,
    ): Map<String, Any> = when (step) {
        "search", "explore" -> listResult(source, body, baseUrl, step, searchKey)
        "bookUrl" -> infoResult(source, body, baseUrl)
        "toc" -> stringListCounted(source, body, baseUrl,
            source.getTocRule().chapterList.orEmpty())
        else -> mapOf("content" to stringValue(source, body, baseUrl,
            source.getContentRule().content.orEmpty()))
    }
    /** 搜索/发现段：完整书目列表（不止首条）——Agent 第二样本复核的材料。
     *  `searchKey` 是这次调试的关键词：`AnalyzeUrl` 要按 App 的方式构造（`WebBook.searchBookAwait`
     *  用 searchUrl + key 构造），搜索规则里的 `{{key}}` / `{{page}}` 才有值——只给结果页
     *  URL 的话，依赖这些变量的规则会算出另一份结果（材料与事件对不上）。 */
    private suspend fun listResult(
        source: BookSource,
        body: String,
        baseUrl: String,
        step: String,
        searchKey: String,
    ): Map<String, Any> {
        val ruleData = RuleData()
        // explore 模式不带 key（App 的 exploreBookAwait 传 key = null）：入口 key 是
        // `发现::URL` 整串，原样传进 {{key}} 会算出另一份结果（材料与事件对不上）。
        val listKey = if (step == "explore") null else searchKey
        val analyzeUrl = AnalyzeUrl(
            mUrl = source.searchUrl.orEmpty().ifBlank { baseUrl },
            key = listKey, page = 1,
            baseUrl = source.bookSourceUrl, source = source, ruleData = ruleData)
        val books = BookList.analyzeBookList(
            bookSource = source, ruleData = ruleData, analyzeUrl = analyzeUrl,
            baseUrl = baseUrl, body = body, isSearch = step == "search")
        val shown = books.take(LIST_LIMIT)
        return linkedMapOf(
            "total" to books.size,
            "truncated" to (books.size > shown.size),
            "items" to shown.map { b ->
                linkedMapOf(
                    "name" to b.name,
                    "author" to b.author,
                    "kind" to b.kind,
                    "word_count" to b.wordCount,
                    "latest_chapter" to b.latestChapterTitle,
                    "book_url" to b.bookUrl,
                )
            },
        )
    }

    /** 详情段：BookInfoRule 全组字段，按 AnalyzeRule 直跑（与 BookInfo.analyzeBookInfo 同口径，
     *  包括 `setRedirectUrl`——URL 字段的绝对化基准在它手上，少了它相对路径原样返回）。 */
    private suspend fun infoResult(
        source: BookSource,
        body: String,
        baseUrl: String,
    ): Map<String, Any> {
        val rule: BookInfoRule = source.getBookInfoRule()
        val analyzer = AnalyzeRule(RuleData(), source).setContent(body, baseUrl)
        analyzer.setRedirectUrl(baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        rule.init?.takeIf { it.isNotBlank() }?.let {
            analyzer.setContent(analyzer.getElement(it))
        }
        fun field(ruleText: String?): String =
            if (ruleText.isNullOrBlank()) "" else analyzer.getString(ruleText).take(VALUE_MAX)
        return linkedMapOf(
            "name" to field(rule.name),
            "author" to field(rule.author),
            "kind" to field(rule.kind),
            "word_count" to field(rule.wordCount),
            "last_chapter" to field(rule.lastChapter),
            "intro" to field(rule.intro),
            "cover_url" to analyzer.getString(rule.coverUrl, isUrl = true),
            "toc_url" to analyzer.getString(rule.tocUrl, isUrl = true),
        )
    }

    /** 目录段：`chapter_list` + 计数（`total`/`truncated` 与列表段同语义）——
     *  超过 [LIST_LIMIT] 的目录不加标记的话，「完整列表（20 条）」会把截断样本
     *  说成全部（AGENTS #4：截断要写明，不静默）。 */
    private suspend fun stringListCounted(
        source: BookSource,
        body: String,
        baseUrl: String,
        rule: String,
    ): Map<String, Any> {
        if (rule.isBlank()) {
            return linkedMapOf("chapter_list" to emptyList<String>(),
                "total" to 0, "truncated" to false)
        }
        val analyzer = AnalyzeRule(RuleData(), source).setContent(body, baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        // `chapterList` 是**列表规则**（每条是一个条目选择器，不是取值规则）：
        // 拿 getStringList 跑它等于把选择器当取值末段（AGENTS #21），恒空。
        // 正确口径与 App 一致：先 getElements 取条目，再在条目上取名称。
        val elements = analyzer.getElements(rule)
        if (elements.isNotEmpty()) {
            // total 是**条目数**，只对前 [LIST_LIMIT] 条跑取值——几千章的目录
            // 按「计数」付全量解析（每条一个 AnalyzeRule）是白付的钱
            val shown = elements.take(LIST_LIMIT).map { el ->
                val item = AnalyzeRule(RuleData(), source).setContent(el)
                item.getString(tableNameRule(source)).take(VALUE_MAX)
            }
            return linkedMapOf("chapter_list" to shown,
                "total" to elements.size, "truncated" to (elements.size > shown.size))
        }
        val all = analyzer.getStringList(rule).orEmpty()
        return linkedMapOf("chapter_list" to all.take(LIST_LIMIT).map { it.take(VALUE_MAX) },
            "total" to all.size, "truncated" to (all.size > all.take(LIST_LIMIT).size))
    }

    /** 目录条目取名称用的规则：`ruleToc.chapterName`，没配就退回条目文本（同 App 的兜底）。 */
    private fun tableNameRule(source: BookSource): String =
        source.getTocRule().chapterName?.takeIf { it.isNotBlank() } ?: "text"

    private suspend fun stringValue(
        source: BookSource,
        body: String,
        baseUrl: String,
        rule: String,
    ): String {
        if (rule.isBlank()) return ""
        val analyzer = AnalyzeRule(RuleData(), source).setContent(body, baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        return analyzer.getString(rule).take(VALUE_MAX)
    }
}

package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.AnalyzeRule.Companion.setCoroutineContext
import io.legado.app.model.analyzeRule.RuleData
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.runBlocking
import kotlin.coroutines.coroutineContext

/**
 * 轻量验证（rule_try）：「这一步的整页 HTML + 一条规则」直接过 App 真解析器，
 * 返回取到的值——**不走调试链**：不发请求、不跑事件流、不等网络。
 *
 * 它回答的只有「这条规则在这份 HTML 上选不选得中」，用于一键填充建议的
 * 语法级快验（填错规则 → 重调整步 → 等引擎 的空跑循环从分钟级压到秒级）。
 * **它不替代整步验收**：URL 规则的 `{{key}}/{{page}}` 求值、JS 规则的全局对象
 * 都不在它手上——能不能用仍由「重调本步验证」的真引擎说了算。
 *
 * 与 StructuredResult 的关系：bookUrl/content/toc 的求值口径同源（单字段走
 * AnalyzeRule.getString、列表走 getElements + 条目取名），但规则文本来自外部
 * 而不是源声明。search/explore **不跑** `BookList.analyzeBookList`——它内部读的是
 * 源声明的 searchRule/exploreRule，不收外部规则（收了也会被静默忽略）；而
 * rule_try 的承诺是「这条规则选不选得中」，走与 toc 同一 getElements 口径。
 * 不直接调用 StructuredResult 的 private 函数，写法保持同构（改口径要两边同步，
 * 契约测试钉住）。
 *
 * 失败纪律（AGENTS #4）：**不抛出**——所有失败进 `error` 字段带原因返回，
 * 解析异常给 `type: message`，规则为空直说。调用方（daemon / 一次性 DebugService）
 * 不用 try 包它。
 */
internal object RuleTry {

    /** 返回给调用方的值条数上限与单条长度：给预览表展示，样本级就够。 */
    const val VALUE_MAX = 200
    const val VALUES_MAX = 5

    /**
     * 跑一条规则。``step`` 决定求值口径（与 StructuredResult.stepResult 同表）；
     * ``rule`` 为空、解析失败、取 0 条都**不抛**，进 `error`。
     */
    fun run(
        source: BookSource,
        body: String,
        baseUrl: String,
        step: String,
        rule: String,
        itemRule: String = "",
    ): Map<String, Any> = runBlocking {
        val ruleText = rule.trim()
        if (ruleText.isBlank()) {
            return@runBlocking fail("规则为空")
        }
        if (body.isBlank()) {
            return@runBlocking fail("页面材料为空")
        }
        try {
            when (step) {
                "bookUrl" -> single(source, body, baseUrl, ruleText)
                "toc" -> list(source, body, baseUrl, ruleText, itemRule)
                "content" -> single(source, body, baseUrl, ruleText)
                "search", "explore" -> list(source, body, baseUrl, ruleText, itemRule)
                else -> fail("未知步骤：%s（支持 search / explore / bookUrl / toc / content）".format(step))
            }
        } catch (e: Throwable) {
            val root = generateSequence(e as Throwable?) { it.cause }.lastOrNull() ?: e
            fail("%s: %s".format(root::class.simpleName, root.message?.take(120) ?: ""))
        }
    }

    /** 单值规则（bookUrl 字段 / content）：getString 取一次，非空即成功。 */
    private suspend fun single(
        source: BookSource,
        body: String,
        baseUrl: String,
        rule: String,
    ): Map<String, Any> {
        val analyzer = AnalyzeRule(RuleData(), source).setContent(body, baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        // 与 StructuredResult.infoResult 同一纪律：URL 字段的绝对化基准在 setRedirectUrl
        analyzer.setRedirectUrl(baseUrl)
        val value = analyzer.getString(rule).take(VALUE_MAX)
        return if (value.isBlank()) {
            fail("取到空（规则没选中，或选中的节点没有文本）")
        } else {
            ok(listOf(value), 1)
        }
    }

    /** 列表规则（search/explore/toc 同一口径）：getElements 取条目，条目上取值。
     *
     *  ``itemRule`` 是**条目内规则**（相对条目元素，如 `tag.a@href`）：引擎是两段式
     *  （`BookList.analyzeBookList` 先按 bookList 取条目、再逐条目取 name/bookUrl），
     *  只有先切条目才连得上。不给它就取条目文本（旧的单条列表口径）。
     */
    private suspend fun list(
        source: BookSource,
        body: String,
        baseUrl: String,
        rule: String,
        itemRule: String = "",
    ): Map<String, Any> {
        val analyzer = AnalyzeRule(RuleData(), source).setContent(body, baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        val elements = analyzer.getElements(rule)
        if (elements.isEmpty()) {
            return fail("取到 0 条（选择器没选中任何条目）")
        }
        val inner = itemRule.trim().ifBlank { "text" }
        val values = elements.take(VALUES_MAX).map { el ->
            val item = AnalyzeRule(RuleData(), source).setContent(el)
            item.getString(inner).take(VALUE_MAX)
        }
        // 「选中了条目」与「从条目里取到了值」是两件事：条目内规则找的是条目里的
        // 某个子节点或属性，取不到时每条都是空串，而元素数是够的。只报条数会把
        // 「这条规则取不到东西」说成通过（实测 2026-10-26：`.txtA@href` 取空——那个
        // `a` 没有 href——却因为 `li` 选中 1 条判 ok，落源后 bookUrl 回落成搜索页
        // 地址，引擎照样 pass）。判据与 single 的 `value.isBlank()` 对齐。
        if (values.all { it.isBlank() }) {
            return fail("选中 %d 条，但条目内规则没取到值".format(elements.size))
        }
        return ok(values, elements.size)
    }

    private fun ok(values: List<String>, count: Int): Map<String, Any> = linkedMapOf(
        "ok" to true, "values" to values, "count" to count, "error" to "",
    )

    private fun fail(error: String): Map<String, Any> = linkedMapOf(
        "ok" to false, "values" to emptyList<String>(), "count" to 0, "error" to error.take(200),
    )

    /**
     * 从文件读源（数组取第一条 / 单对象，取法与 DebugService 共用一份）。
     * 抛 IO/解析异常由调用方转成失败应答——这里不做静默兜底；
     * 空数组（文件里没有源）显式报错，不是越界异常。
     */
    fun readSource(path: String): BookSource {
        val sourceJson = DebugService.sourceJsonOf(java.io.File(path).readText(Charsets.UTF_8))
        if (sourceJson.isEmpty()) throw IllegalStateException("源文件里没有源: $path")
        return GSON.fromJsonObject<BookSource>(sourceJson).getOrThrow()
    }
}

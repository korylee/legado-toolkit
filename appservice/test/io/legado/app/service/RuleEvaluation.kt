package io.legado.app.service

import io.legado.app.data.entities.BookSource
import io.legado.app.model.analyzeRule.AnalyzeRule
import io.legado.app.model.analyzeRule.AnalyzeRule.Companion.setCoroutineContext
import io.legado.app.model.analyzeRule.RuleData
import org.jsoup.nodes.Element
import kotlin.coroutines.coroutineContext

/** Fixed-input rule execution through the App parser, shared by tests and the debug daemon. */
object RuleEvaluation {

    suspend fun evaluate(
        source: BookSource,
        html: String,
        baseUrl: String,
        rule: String,
        mode: String,
        limit: Int,
    ): Map<String, Any?> {
        require(html.isNotBlank()) { "输入页面为空" }
        require(rule.isNotBlank()) { "规则为空" }
        require(limit in 1..200) { "结果条数限制须在 1～200" }
        val analyzer = AnalyzeRule(RuleData(), source)
            .setContent(html, baseUrl)
        analyzer.setCoroutineContext(coroutineContext)
        return when (mode) {
            "elements" -> {
                val all = analyzer.getElements(rule)
                val shown = all.take(limit)
                mapOf(
                    "mode" to mode,
                    "count" to all.size,
                    "truncated" to (all.size > shown.size),
                    "values" to shown.map { (it as? Element)?.outerHtml() ?: it.toString() },
                )
            }
            "list" -> {
                val all = analyzer.getStringList(rule).orEmpty()
                val shown = all.take(limit)
                mapOf("mode" to mode, "count" to all.size,
                    "truncated" to (all.size > shown.size), "values" to shown)
            }
            "value" -> mapOf("mode" to mode, "count" to 1,
                "truncated" to false, "values" to listOf(analyzer.getString(rule)))
            "url" -> mapOf("mode" to mode, "count" to 1,
                "truncated" to false, "values" to listOf(analyzer.getString(rule, isUrl = true)))
            else -> error("未知规则执行模式：$mode")
        }
    }
}

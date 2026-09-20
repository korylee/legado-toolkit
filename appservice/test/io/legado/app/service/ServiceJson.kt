package io.legado.app.service

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * 结论行（`Map<String, Any?>` → JSON）的**唯一**编码器。
 *
 * 抽出来不只是为了去重，而是因为**两处各写一遍时语义已经分家了**，而分家的那一点
 * 正好是出错过的那一点：
 *
 * - 跑批那份（原 `ValidateService.main`）**少一个 null 分支**，落到
 *   `JsonPrimitive(x.toString())`——Kotlin 的 `Any?.toString()` 把 null 变成
 *   **字符串 `"null"`**。而 `runContentStage` 对下载类源（type 3）正是返回
 *   `"content_ok" to null`：那一行会让 `GET /api/sources` 的 response_model
 *   （`jvm_content_ok: Optional[bool]`）校验失败 → **整页书源列表 500**
 *   （实证：`SourcePage.model_validate` 对字符串 `"null"` 报 `items.0.jvm_content_ok`。
 *   这把火从 S3-2 起就埋着，只差一个 depth=content 且掺了 type 3 的批次）。
 * - 调试那份（原 `DebugService.writeMeta`）**没有 List 分支**，列表会变成
 *   `"[a, b]"` 这样的字符串。
 *
 * 所以这里是两者的**并集**。调用点别再各写一遍 `when`——两处各写一遍的代价不是
 * 多打几个字，是上面这两条**不报错**的漂移。
 */
object ServiceJson {

    /** `Map<String, Any?>` → `JsonObject`。键序按入参（结论行是人要读的，别改成无序容器）。 */
    fun toJsonObject(data: Map<String, Any?>): JsonObject =
        JsonObject(data.mapValues { (_, v) -> element(v) })

    private fun element(v: Any?): JsonElement = when (v) {
        null -> JsonNull
        is String -> JsonPrimitive(v)
        is Number -> JsonPrimitive(v)
        is Boolean -> JsonPrimitive(v)
        is List<*> -> JsonArray(v.map { element(it) })
        // 其余类型照原样转字符串：不猜结构，猜错会静默丢字段
        else -> JsonPrimitive(v.toString())
    }
}

package io.legado.app.service

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 结论行编码器的形状钉（#4）。
 *
 * **为什么要有这条**：这个编码器原来有**两份实现**，而两份对 `null` 的处理不同——
 * 跑批那份少一个 null 分支，落到 `Any?.toString()`，于是 `content_ok = null`
 * （下载类源在正文段的正常结论）被写成**字符串 `"null"`**。下游
 * `SourceOut.jvm_content_ok: Optional[bool]` 校验不过 → `GET /api/sources`
 * **整页 500**（`/api/sources` 是列表页的唯一数据来源）。
 *
 * 这种错的特点：**本进程不报错、日志里看不到、只在下游另一个语言那边炸**。
 * 所以它必须靠一个**不联网**的测试钉住形状，而不是靠某次跑批的运气。
 *
 * 不需要 Robolectric：这个对象不碰 Android（纯 kotlinx.serialization）。
 */
class ServiceJsonTest {

    private fun encode(data: Map<String, Any?>): String =
        Json.encodeToString(JsonObject.serializer(), ServiceJson.toJsonObject(data))

    @Test
    fun null_is_json_null_not_the_string_null() {
        val line = encode(linkedMapOf("content_ok" to null, "state" to "no_result"))
        assertEquals("""{"content_ok":null,"state":"no_result"}""", line)
        // 旧 Bug 的形状：整行里不许出现字符串 "null"
        assertTrue("出现了字符串 \"null\"（旧 Bug 形状）：$line", !line.contains("\"null\""))
    }

    @Test
    fun list_becomes_a_json_array_and_elements_are_encoded_by_value() {
        assertEquals("""{"s":["a","b"]}""", encode(linkedMapOf("s" to listOf("a", "b"))))
        // 列表里的 null 也是 JSON null（旧实现会写成字符串 "null"）
        assertEquals("""{"s":["a",null]}""", encode(linkedMapOf("s" to listOf("a", null))))
        assertEquals("""{"s":[1,true]}""", encode(linkedMapOf("s" to listOf(1, true))))
    }

    @Test
    fun numbers_and_booleans_stay_primitives_and_everything_else_is_stringified() {
        assertEquals("""{"i":1,"l":9223372036854775807,"b":true}""",
            encode(linkedMapOf("i" to 1, "l" to Long.MAX_VALUE, "b" to true)))
        // 其余类型照原样转字符串：不猜结构，猜错会静默丢字段
        // （**原来这条钉的是 Map**——那时它落到 toString() 上、变成 `{k=v}`；
        //  第三期给侧车加 `matched_html` 时才发现那正是它的形状，见下一条）
        val weird = object { override fun toString() = "w" }
        assertEquals("""{"x":"w"}""", encode(linkedMapOf("x" to weird)))
    }

    @Test
    fun nested_map_becomes_a_json_object_not_a_kotlin_map_to_string() {
        // 侧车里的 `matched_html` 是 `{url: {step: html}}`。落到 `toString()` 会是
        // `{http://a/x={search=<div/>}}`，Python 侧的形状闸门（`matched_map`）只能
        // 报「形状不对」并把整块丢掉——证据永远空着，而两侧都不报错（AGENTS #22）
        assertEquals(
            """{"matched_html":{"http://a/x":{"search":"<div class='b'></div>"}}}""",
            encode(linkedMapOf("matched_html" to
                linkedMapOf("http://a/x" to linkedMapOf("search" to "<div class='b'></div>")))))
        // 空 map 也是对象，不是 "{}" 这个字符串
        assertEquals("""{"m":{}}""", encode(linkedMapOf("m" to emptyMap<String, String>())))
    }
}

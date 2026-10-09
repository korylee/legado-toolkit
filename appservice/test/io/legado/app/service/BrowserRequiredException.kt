package io.legado.app.service

/**
 * 源要求**人机校验**（要人在浏览器里点、或认图片验证码），而本机校验无人可等。
 *
 * **必须有独立的名字**：它从规则 JS 里抛出后被 Rhino 包成 `ScriptException`
 * （`WrappedException` 的 cause 是空的），`classifyRoot` 只看得到那层壳，所以归因
 * 只能靠消息里的这个类名。跟着用 `NoStackTraceException` 的后果是实测过的：
 * 判成「源的规则/配置有问题（下一步是修）」——**指错动作**（该做的是换通道去连 App
 * 调试，不是去改规则），AGENTS #20 那类"归因说反"。
 *
 * 对应 `core/jvm_health.CAUSE_BROWSER`；词表两侧逐词同形由
 * `tests/test_jvm_service_parity.py` 钉住（AGENTS #22⑤）。
 */
class BrowserRequiredException(message: String) : Exception(message)

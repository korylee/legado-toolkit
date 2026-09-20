package io.legado.app.service

import java.io.File

/**
 * 浏览器会话的生命周期：**整个进程共享一个**，不是一次渲染一个。
 *
 * 这原是 `ValidateService` 里的私有实现（S3-4 写的），S5-A2 的
 * [ShadowBackstageWebView] 也要用同一套——**所以抽出来，两边都调它**。
 * 复制第二份的代价在这里尤其明显：这段逻辑全是踩出来的（见下三条），
 * 两份必然漂移，而漂移的表现是「一个通道能渲染、另一个报浏览器不可用」，
 * 排查时根本想不到是两份实现。
 *
 * 三条都是实测（2026-09-19/20）：
 *
 * 1. **一批一个进程**：一源一个的后果是并发直接失效——第二个进程因 profile 目录
 *    被占用**立刻退出**，9 条源里 7 条报 `browser_unavailable: 浏览器进程已退出`。
 * 2. **启动失败要记住**：不然 3774 条源各试一次，每次都等满超时。
 * 3. **profile 被占用要能自愈**：固定 profile 的价值是留住 cookie（登录墙后的源有用），
 *    但上一轮残留的浏览器实例会占着它——那时新实例会立刻退出（实测：手工起过一个
 *    同 profile 的 Edge，之后所有启动都报「浏览器进程已退出」）。所以固定 profile
 *    打不开就换一个本次专属 profile（`<base>-run<时间戳>`），并顺手清扫残留。
 *
 * **没有「本次禁用浏览器」的开关**：曾有一个 `disabled` 字段，但从声明到删除
 * （2026-09-20）**没有任何调用点会置位它**——那正是 AGENTS #12 说的「读起来像
 * 存在的信号」。真要能停用，得先有调用点（args 键 + 启动器转发 + 后端写入三层），
 * 不是留一个恒假的字段在这里。
 */
object BrowserSession {

    private val lock = Any()
    private var session: BrowserBridge.Session? = null
    private var error: String? = null

    private fun profileDir(): File =
        File(System.getProperty("legado.browser.profile")
            ?: System.getenv("LEGADO_BROWSER_PROFILE")
            ?: File(System.getProperty("java.io.tmpdir"), "legado-appservice-profile").path)

    /** f 是不是自愈路径创建的专属 profile（`<base>-run<时间戳>`）。
     *  只认这个模式，用户经系统属性/环境变量指定的 profile **永不匹配**；
     *  base 自己恰好叫这名字时也排除，防止把用户的目录删了。 */
    private fun isRunProfile(f: File, base: File): Boolean {
        if (f.absolutePath == base.absolutePath) return false
        val prefix = base.name + "-run"
        if (!f.name.startsWith(prefix)) return false
        return f.name.drop(prefix.length).toLongOrNull() != null
    }

    /**
     * 取（必要时启动）共享的浏览器会话。返回 `(会话, 失败原因)`。
     *
     * 失败时**显式给原因**，调用方据此判 unknown——不静默退回「空壳」结论
     * （那会把工具的欠缺说成源的问题，AGENTS #4）。
     */
    fun get(): Pair<BrowserBridge.Session?, String> {
        synchronized(lock) {
            error?.let { return null to it }
            session?.let { return it to "" }
            // 兜底清扫：上次进程被外部杀掉时 close 走不到，-run<时间戳>
            // 专属 profile 会残留（含缓存，几十 MB 级）。趁浏览器没起先扫一遍；
            // 删不掉（文件还被占）不碍事，下次再试。
            runCatching {
                val base = profileDir()
                base.parentFile?.listFiles()?.forEach { f ->
                    if (isRunProfile(f, base)) f.deleteRecursively()
                }
            }
            var (s, why) = BrowserBridge.launch(profileDir())
            if (s == null) {
                // 退一步：换本次专属 profile 再试一次（理由见类注释第 3 条）
                val base = profileDir()
                val fresh = File(base.parentFile, base.name + "-run" + System.currentTimeMillis())
                val (s2, why2) = BrowserBridge.launch(fresh, tempProfile = true)
                if (s2 == null) {
                    error = "$why2（固定 profile 也失败：$why）"
                    return null to error!!
                }
                s = s2
                why = ""
            }
            session = s
            return s to ""
        }
    }

    /** 收尾：关掉共享会话。跑批在 `shutdown()` 里调、调试在收尾时调。 */
    fun close() {
        synchronized(lock) {
            session?.close()
            session = null
        }
    }
}

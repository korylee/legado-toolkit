package io.legado.app.service

import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

/**
 * 浏览器桥探针（S3-4）：先证明「JVM 里能把页面渲染出来」，再谈接进校验链路。
 *
 * 用 `-Pappservice.render=<url>`（经 args.properties 的 `render=`）传目标页；
 * 不传就跑一个内置的 JS 壳页面（`document.write` 注入内容），
 * 用来验证「渲染后 DOM 里确实有浏览器执行 JS 产生的节点」。
 *
 * 断言的是**渲染真的发生了**：静态 HTML 里没有的字符串，渲染后必须出现。
 * 只断言"拿到了 HTML"是不够的——那种情况下 shell 页与渲染页长得一样。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class BrowserBridgeProbeTest {

    private fun readArgs(): Map<String, String> {
        val f = listOf(
            File("appservice-args.properties"),
            File(System.getenv("LEGADO_APPSERVICE_DIR") ?: "", "args.properties"),
        ).firstOrNull { it.exists() } ?: return emptyMap()
        val props = java.util.Properties()
        f.reader(Charsets.UTF_8).use { props.load(it) }
        return props.entries.associate { (k, v) -> k.toString() to v.toString() }
    }

    @Test
    fun render() {
        val args = readArgs()
        val target = args["render"]
        val exe = BrowserBridge.findBrowser()
        println("PROBE: browser=${exe ?: "没找到"}")
        if (exe == null) {
            println("PROBE: 跳过（browser_unavailable）")
            return
        }
        val profile = File(args["profile"] ?: "data/app_probe/browser_profile")
        val (session, why) = BrowserBridge.launch(profile)
        if (session == null) {
            println("PROBE: 启动失败 → $why")
            return
        }
        try {
            if (target != null && target.isNotBlank()) {
                val r = BrowserBridge.render(session, target)
                println("PROBE: $target → ok=${r.ok} reason=${r.reason} len=${r.html.length}")
                if (r.ok) {
                    File("data/app_probe/rendered.html").writeText(r.html)
                    println("PROBE: 已写 data/app_probe/rendered.html")
                    println("PROBE: 前 200 字：" + r.html.take(200).replace('\n', ' '))
                }
            } else {
                // 内置壳页：静态 HTML 里没有 "RENDERED-BY-JS"，只有 JS 跑完才有
                val url = "data:text/html,<html><body><div id=x>shell</div>" +
                    "<script>document.getElementById('x').innerHTML='RENDERED-BY-JS';</script>" +
                    "</body></html>"
                val r = BrowserBridge.render(session, url)
                println("PROBE: shell 页 → ok=${r.ok} reason=${r.reason}")
                println("PROBE: 含 RENDERED-BY-JS = ${r.html.contains("RENDERED-BY-JS")}")
                check(r.ok && r.html.contains("RENDERED-BY-JS")) {
                    "渲染没生效：JS 注入的内容不在 DOM 里"
                }
            }
        } finally {
            session.close()
        }
    }
}

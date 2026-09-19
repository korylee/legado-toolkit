package io.legado.app.service

import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import io.legado.app.probe.WindowsPathAssetManagerShadow

/**
 * ValidateService 的启动器：`main` 逻辑需要 Robolectric 环境（appCtx/Koin/assets），
 * 而进入该环境的标准途径就是测试任务。所以 S1 的「CLI」= 给这个测试传参。
 *
 * 传参方式：`-Pappservice.args="--dir ... --keyword 我 ..."`（Gradle 属性 → 环境变量
 * → 这里读出来转发给 [ValidateService.main]）。
 *
 * 结果写到 `--out` 指定的 NDJSON 文件；进度/日志走 stderr。
 *
 * ⚠️ 会**真联网**，并发与超时由参数控制。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class ValidateServiceLauncher {

    @Test
    fun run() {
        // 传参走 appservice-args.properties（key=value：file/dir/keyword/out/
        // concurrency/timeout/limit/depth/noStripWebview）。比命令行转义可靠——
        // bash→cmd→gradle 三层引号转义已经坑过三轮。
        // 文件定位：先 CWD，再按 LEGADO_APPSERVICE_DIR 兜底（启动器会把 CWD
        // 设为 App 仓库根）。
        val candidates = listOf(
            java.io.File("appservice-args.properties"),
            java.io.File(System.getenv("LEGADO_APPSERVICE_DIR") ?: "", "args.properties"),
        )
        val f = candidates.firstOrNull { it.exists() }
        if (f == null) {
            println("APPSERVICE-LAUNCHER: 找不到 args.properties（试过 " +
                candidates.map { it.absolutePath } + "），跳过")
            return
        }
        val props = java.util.Properties()
        f.reader(Charsets.UTF_8).use { props.load(it) }
        val args = mutableListOf<String>()
        props.getProperty("file")?.let { args += listOf("--file", it) }
        props.getProperty("dir")?.let { args += listOf("--dir", it) }
        props.getProperty("keyword")?.let { args += listOf("--keyword", it) }
        props.getProperty("out")?.let { args += listOf("--out", it) }
        props.getProperty("concurrency")?.let { args += listOf("--concurrency", it) }
        props.getProperty("timeout")?.let { args += listOf("--timeout", it) }
        props.getProperty("limit")?.let { args += listOf("--limit", it) }
        props.getProperty("depth")?.let { args += listOf("--depth", it) }
        // 浏览器 profile 目录（S3-4）：给跑批用，探针自己读同一个键
        props.getProperty("profile")?.let {
            System.setProperty("legado.browser.profile", it)
        }
        if (props.getProperty("noStripWebview") == "1") args += "--no-strip-webview"
        println("APPSERVICE-LAUNCHER: args=" + args)
        ValidateService.main(args.toTypedArray())
        println("APPSERVICE-LAUNCHER: done")
    }
}

package io.legado.app.service

import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import io.legado.app.probe.WindowsPathAssetManagerShadow

/**
 * DebugService（S5-A1）的启动器：`main` 逻辑需要 Robolectric 环境（appCtx/Koin/assets），
 * 而进入该环境的标准途径就是测试任务。形态与 [ValidateServiceLauncher] 一致——
 * 参数走 `appservice/args.properties`，由 `legado-gradle.bat` 拉起：
 *
 *     cmd /c appservice/legado-gradle.bat :app:testAppDebugUnitTest \
 *         --tests io.legado.app.service.DebugServiceLauncher --rerun
 *
 * **为什么单开一个启动器而不是并进 ValidateServiceLauncher**：两者的参数集不同
 * （跑批是 `dir/keyword/concurrency/depth`，调试是 `key/out` 单源），混在一个
 * `@Test` 里要靠猜参数决定走哪条路；分开之后 `--tests` 就是唯一的分派开关，
 * 跑批与调试互不干扰。两者读同一个 args.properties，靠 **`key=` 在不在**区分：
 * 没有 `key=` 说明这是跑批的参数文件，本启动器直接跳过（而不是拿跑批参数去调试）。
 *
 * **返回码 → Gradle 退出码**：`main` 返回非 0 时这里抛 `AssertionError`，
 * 让测试任务失败，Gradle 于是给出非 0 退出码——调用方（A4 的 subprocess）靠它
 * 区分「跑完了」和「零事件/超时」。**不能只打在 stderr 上了事**：那种失败
 * 会被 `--rerun` 之后的「构建成功」掩盖掉。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class, ShadowBackstageWebView::class])
class DebugServiceLauncher {

    @Test
    fun run() {
        // 文件定位由 [AppserviceEnv] 统一负责（与 ValidateServiceLauncher、两个探针同一份）。
        // **注意**：下面的键清单别搬走——tests/test_jvm_debug_contract.py 钉着
        // `getProperty("file"/"key"/"out"/"timeout")` 这几个字面量，那是两侧的约定
        val props = AppserviceEnv.loadArgs()
        if (props == null) {
            println("APPSERVICE-LAUNCHER: 找不到 args.properties（试过 " +
                AppserviceEnv.argCandidates().map { it.absolutePath } + "），跳过")
            return
        }

        // 没有 key= 就是跑批的参数文件，不归本启动器管——**跳过而不是失败**：
        // 不带上 --tests 跑全量测试时两个启动器都会执行，这里失败会平白弄红一次构建
        val key = props.getProperty("key")
        if (key.isNullOrBlank()) {
            println("APPSERVICE-LAUNCHER: args.properties 里没有 key=，不是调试参数，跳过")
            return
        }

        val args = mutableListOf<String>()
        props.getProperty("file")?.let { args += listOf("--file", it) }
        args += listOf("--key", key)
        props.getProperty("out")?.let { args += listOf("--out", it) }
        props.getProperty("timeout")?.let { args += listOf("--timeout", it) }
        // A3：手工 cookie（可选）——不给就由服务按源 URL 从浏览器 profile 读
        props.getProperty("cookie")?.let { args += listOf("--cookie", it) }
        println("APPSERVICE-LAUNCHER(debug): args=" + args)

        val code = DebugService.main(args.toTypedArray())
        println("APPSERVICE-LAUNCHER(debug): done, code=$code")
        if (code != DebugService.OK) {
            // 理由已经打在 stderr 上（DebugService 里）；这里只负责让构建失败
            throw AssertionError(
                "DebugService 退出码 $code（0=正常 / 2=零事件 / 3=超时 / 4=入参错误）")
        }
    }
}

package io.legado.app.service

import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * [AppServiceDaemon] 的启动器：常驻循环需要 Robolectric 环境（appCtx/Koin/assets），
 * 而进入该环境的唯一途径就是测试任务/测试运行器——所以它也是一个 JUnit4 测试类，
 * 与 [DebugServiceLauncher]、[ValidateServiceLauncher] 同一形态。
 * （2026-10-18 起**只有一个** daemon 启动器：DebugServiceDaemonLauncher 与
 * ValidateServiceDaemonLauncher 并入，`--tests` 按 op 分派不再是两个进程。）
 *
 * ## 参数走环境变量，不走 args.properties
 *
 * `args.properties` 是**每次调试/校验**的参数文件（含源文件路径、输出路径），常驻进程活的
 * 比它久，读它只会读到某一次的旧值。所以常驻只从环境变量拿「它自己的」参数：
 *
 *     LEGADO_DAEMON_PORT      监听端口（必须给，由客户端选一个空闲端口）
 *     LEGADO_DAEMON_IDLE_SEC  空闲退出秒数（默认 [AppServiceDaemon.DEFAULT_IDLE_SEC]）
 *     LEGADO_DAEMON_SIG       版本键（可选）：`op=ping` 时原样回吐，供客户端判「进程里
 *                             的是不是最新编译的那份类」
 *
 * 单次调试/校验的参数**随每个请求走 socket**（见 `AppServiceDaemon` 的协议）——
 * 这正是「两个文件换成一条流」的意思：文件是「进程外的一次性握手」，流是「同一份
 * 契约按请求走」。
 *
 * ## shadow 并集（2026-10-18 拍板：调试链也接管人机校验）
 *
 * 合并前 validate 侧注册 `ShadowSourceVerification`（人机校验绝不 park 等用户）、
 * debug 侧不注册（复现 App 管线）。合并成一个 JVM 后 shadow 必须并集，调试链的
 * `java.startBrowserAwait` 从此**立刻带 BrowserRequiredException 结束**（原因走到
 * 用户眼前，AGENTS #4），而不是 park 到超时——这个行为变化是拍板过的，见 TODO
 * `daemon-merge` 结账记录。
 *
 * ## 退出码
 *
 * 空闲退出是 **0**（正常终态，客户端据此判断是「它自己到点走了」而不是「崩了」）；
 * 缺端口是 4（入参错），与一次性那条同一套分档。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class, ShadowBackstageWebView::class,
        ShadowSourceVerification::class])
class AppServiceDaemonLauncher {

    @Test
    fun serve() {
        val port = System.getenv("LEGADO_DAEMON_PORT")?.trim()?.toIntOrNull() ?: 0
        val idle = System.getenv("LEGADO_DAEMON_IDLE_SEC")?.trim()?.toLongOrNull()
            ?: AppServiceDaemon.DEFAULT_IDLE_SEC
        val sig = System.getenv("LEGADO_DAEMON_SIG")?.trim().orEmpty()
        if (port <= 0) {
            println("APPSERVICE-LAUNCHER(daemon): 缺 LEGADO_DAEMON_PORT，退出")
        }
        val code = AppServiceDaemon.serve(port, idle, sig)
        println("APPSERVICE-LAUNCHER(daemon): done, code=$code")
        if (code != DebugService.OK) {
            // 与 DebugServiceLauncher 同一手法：让测试失败，Gradle 才会给非 0 退出码
            throw AssertionError("AppServiceDaemon 退出码 $code（0=正常空闲退出 / 4=入参错误）")
        }
    }
}

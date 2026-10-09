package io.legado.app.service

import io.legado.app.probe.WindowsPathAssetManagerShadow
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** 通过现有测试 JVM 启动常驻 ValidateService。 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class, ShadowSourceVerification::class])
class ValidateServiceDaemonLauncher {

    @Test
    fun serve() {
        val port = System.getenv("LEGADO_VALIDATE_DAEMON_PORT")?.trim()?.toIntOrNull() ?: 0
        val idle = System.getenv("LEGADO_VALIDATE_DAEMON_IDLE_SEC")?.trim()?.toLongOrNull()
            ?: ValidateServiceDaemon.DEFAULT_IDLE_SEC
        val sig = System.getenv("LEGADO_VALIDATE_DAEMON_SIG")?.trim().orEmpty()
        val code = ValidateServiceDaemon.serve(port, idle, sig)
        println("APPSERVICE-VALIDATE-LAUNCHER: done, code=$code")
        if (code != 0) {
            throw AssertionError("ValidateServiceDaemon 退出码 $code（0=正常空闲退出 / 4=入参错误）")
        }
    }
}

package io.legado.app.service

import java.io.File
import java.util.Properties

/**
 * 进程级共享的环境事实：**跑批 / 调试 / 两个探针都走这一份**。
 *
 * 单独一个文件的理由不是「好看」，是这两样东西**只能有一份**，而它们原来各被抄了
 * 三到四遍（UA 四处、参数文件定位三处）：
 *
 * - [USER_AGENT]：源自己没有配 `header.User-Agent` 时 App 发出去的那条。四处各写一份
 *   的后果不是报错，是**同一条源在我们不同通道里拿到不同页面**（站点按 UA 分派
 *   HTML / JS），而且**不报错**——与 AGENTS #12 是同一类：读起来像一个存在的信号。
 * - [loadArgs]：参数文件的唯一入口。各写一份的后果是「一个入口读到了参数、另一个
 *   没读到」，表现成「跑了但什么都没发生」。
 */
object AppserviceEnv {

    /**
     * 请求头里的 User-Agent（`BaseSource.getHeaderMap` → `AppConst.UA_NAME`）。
     *
     * ⚠️ **它不是「设备侧那一条」**——别再照抄那种说法（原 `DebugService` 的注释就是
     * 这么写的，两层都不成立：那条是 `private`，且值也对不上）：
     * - App 自己的默认值随 Cronet 版本走：`Chrome/${Cronet_Main_Version}`
     *   （`FeatureSettingsRepositories.kt` 的 `toDownloadCacheSettings()`，版本号来自
     *   `gradle.properties`，2026-09 是 128.0.0.0），而且用户可以在设置里改它——
     *   **我们从这边读不到设备上的实际取值**。
     * - Python 侧（本地回放）是 `core/constants.py` 的 `DEFAULT_UA`（`Chrome/124.0`）。
     *
     * 三条通道因此各用一条，谁也不保证与谁相同。这个常量只保证一件事：**JVM 侧
     * 所有调用点永远是同一条**。真要「与设备一致」，那不是改这个字符串能解决的——
     * 得把设备的 UA 传进来（与 S5-A3 注入 cookie 同一条路）。
     */
    const val USER_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    /**
     * 参数文件的候选路径，**顺序即优先级**：
     *
     * 1. `LEGADO_APPSERVICE_ARGS` 指的**那个文件**——手跑要换参数时的显式覆盖口；
     *    跑批/调试自己那份也走这条（Python 侧写在 `data/app_probe/args.properties`，
     *    拉起 Gradle 时把它塞进这个环境变量）。**第 2 条只服务手工场景**：
     *    指哪读哪，不猜位置，也不往别人的仓库里放东西。
     * 2. `LEGADO_APPSERVICE_DIR/args.properties`：启动器（`legado-gradle.bat`）设的环境
     *    变量。今天所有自动链路（后端跑批 / `scripts/jvm_debug_run.py`）都命中它。
     *
     * **曾经还有第三个**候选（CWD 下的 `appservice-args.properties`），2026-09-20 删除：
     * 那个 CWD 是 **App 仓库根**（启动器 `pushd` 到那里），而那个仓库必须保持干净
     * （零入侵红线）；且我们仓库与 App 仓库里**都没有**这个文件——它从来不会命中，
     * 却会在有人真放了的那一刻**静默**盖掉第 2 条（「我明明改了 args.properties」却
     * 毫无效果）。手工覆盖请用第 1 条（AGENTS #12 的同一种病：读起来像存在的通道）。
     */
    fun argCandidates(): List<File> = listOfNotNull(
        System.getenv("LEGADO_APPSERVICE_ARGS")?.takeIf { it.isNotBlank() }?.let { File(it) },
        File(System.getenv("LEGADO_APPSERVICE_DIR") ?: "", "args.properties"),
    )

    /**
     * 定位并加载参数文件。**找不到返回 null**，由调用方决定怎么处置——两个启动器
     * 是「打印一句、跳过」，探针是「用内置默认值」，三种处置不同，别在加载器里替它们决定。
     */
    fun loadArgs(): Properties? {
        val f = argCandidates().firstOrNull { it.exists() } ?: return null
        val props = Properties()
        f.reader(Charsets.UTF_8).use { props.load(it) }
        return props
    }
}

package io.legado.app

import io.legado.app.data.entities.BookSource
import io.legado.app.domain.gateway.DownloadCacheSettingsGateway
import io.legado.app.domain.model.settings.DownloadCacheSettings
import io.legado.app.model.webBook.WebBook
import io.legado.app.probe.WindowsPathAssetManagerShadow
import io.legado.app.utils.GSON
import io.legado.app.utils.fromJsonObject
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.koin.core.context.startKoin
import org.koin.core.context.stopKoin
import org.koin.dsl.module
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import splitties.init.injectAsAppCtx

/**
 * JVM 校验服务方案的成败探针（TODO §2 待实测点 6）。
 *
 * 判据只有一条：**Robolectric 下对一条真源调 `searchBookAwait` 能不能出结果**。
 * 通了 → 「App 源码进项目、脚本起 JVM 服务」的技术前提成立；
 * 每挂一次，就把「要补的 stub / 要绕的环境差异」记下来——尾巴长度 = 该方案的真实代价。
 *
 * 数据源：`src/test/resources/probe_source.json`（管理库里挑的最简单的一条真源，
 * 搜索链路无 JS/模板；先在本地回放器验证过能搜出结果，避免把「源坏了」
 * 误判成「环境跑不通」）。
 *
 * 会**真联网**。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = android.app.Application::class, sdk = [35],
    shadows = [WindowsPathAssetManagerShadow::class])
class WebBookProbeTest {

    /** 只提供 AnalyzeUrl 需要的设置网关（纯数据：UA / 线程数），不碰 Android。 */
    private object StubSettingsGateway : DownloadCacheSettingsGateway {
        override val currentSettings = DownloadCacheSettings(
            userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        override val settings: Flow<DownloadCacheSettings> = flowOf(currentSettings)
        override suspend fun update(transform: (DownloadCacheSettings) -> DownloadCacheSettings) = Unit
    }

    private val gatewayModule = module {
        single<DownloadCacheSettingsGateway> { StubSettingsGateway }
    }

    @Before
    fun setUp() {
        // 生产环境由 AndroidX Startup 的 AppCtxInitializer 注入；测试里手工注入同一个
        // 入口（splitties 的公开 API），让 `appDb` 等全局单例能正常构建。
        RuntimeEnvironment.getApplication().injectAsAppCtx()
    }

    @After
    fun tearDown() {
        stopKoin()
    }

    /** 诊断：assets 能不能按 `/` 打开（判断 Windows 的 `File.separator` 是否是硬伤）。 */
    private fun diagnoseAssets() {
        val assets = RuntimeEnvironment.getApplication().assets
        for (p in listOf("defaultData/keyboardAssists.json",
                         "defaultData" + java.io.File.separator + "keyboardAssists.json")) {
            val shown = p.replace("\\", "\\\\")
            try {
                assets.open(p).use { println("PROBE-ASSET: open(\"$shown\") OK, ${it.available()} bytes") }
            } catch (e: Exception) {
                println("PROBE-ASSET: open(\"$shown\") FAILED: ${e::class.simpleName}: ${e.message}")
            }
        }
    }

    private fun loadSource(): BookSource {
        val json = javaClass.classLoader!!
            .getResourceAsStream("probe_source.json")!!
            .bufferedReader(Charsets.UTF_8)
            .use { it.readText() }
        return GSON.fromJsonObject<BookSource>(json).getOrThrow()
    }

    @Test
    fun searchBookAwait_returnsResults_forRealSource() {
        startKoin { modules(gatewayModule) }
        diagnoseAssets()
        val source = loadSource()
        println("PROBE: source=${source.bookSourceName} url=${source.bookSourceUrl}")

        val books = runBlocking {
            WebBook.searchBookAwait(source, "我")
        }

        println("PROBE: got ${books.size} books")
        books.take(3).forEach { println("PROBE:   ${it.name} / ${it.author} / ${it.bookUrl}") }
        assertTrue("searchBookAwait 应返回结果（0 条 = 环境或源有问题）", books.isNotEmpty())
    }
}

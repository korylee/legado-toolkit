package io.legado.app.service

import io.legado.app.data.entities.BaseSource
import io.legado.app.help.source.SourceVerificationHelp
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * `SourceVerificationHelp` 的 shadow：**无人值守的校验通道里，人机校验要么用真浏览器做完，
 * 要么立刻带原因失败——绝不 park 等一个永远不会来的用户。**
 *
 * ## 为什么必须拦这里
 *
 * 上游 `SourceVerificationHelp.getVerificationResult`（SourceVerificationHelp.kt:33-72）：
 * `appCtx.startActivity(...)` 开浏览器 Activity，然后
 *
 * ```kotlin
 * while (getResult(source.getKey()) == null) { ...; LockSupport.parkNanos(this, waitTime) }
 * ```
 *
 * 我们这套 JVM 校验没有 UI、没人点，结果永远写不回来 → **一条源的 JS**
 * （`java.startBrowserAwait` → `JsExtensions.kt:358` → 这里）把线程钉死。
 * 25s 每源预算对它无效：`ValidateService.validateOne` 的 `withTimeout(remaining())`
 * 包的是同一个协程里的调用，而这是**阻塞调用不是挂起**，协程没走到挂起点，超时无从投递。
 * 实测 2026-10-09：一条这样的源让本块停滞 75s（触发停滞看门狗）→ 隔离重跑再等 55s，
 * 本块 384s；100 条批里 3 条这样的源吃掉约 10 分钟。
 *
 * ## 我们凭什么能替它做
 *
 * `BrowserSession` + `BrowserBridge`（CDP 真浏览器）就是为这类页面建的，**校验路径
 * 早就在用**（`ValidateService.validateByRendering`：`renderSerial` + 挑战页等待）；
 * 缺的只是「JS 桥里那句 `java.startBrowserAwait`」这条线。所以这里不是降级，
 * 而是把同一个能力接到第二个入口上。
 *
 * ## 边界（都**显式抛原因**，不静默）
 *
 * - 图片验证码（`useBrowser=false`，`java.getVerificationCode`）要人认图 → 抛；
 * - 浏览器不可用（本机没装 Edge/Chrome）→ 抛，带 `BrowserSession` 的原因；
 * - 渲染失败 / 空页 / 浏览器错误页 / 渲染完仍是挑战页 → 抛，带渲染原因。
 *
 * 抛出的原因会顺着规则求值变成结论里的 `reason`（用户据此走「连 App 调试」），
 * 而不是变成一句语焉不详的「引擎对该源无响应」。
 *
 * ## 注册范围
 *
 * 只进两个**校验**启动器（`ValidateServiceLauncher` / `ValidateServiceDaemonLauncher`）。
 * 调试通道（`DebugService*Launcher`）没有注册：那里的定位是"复现 App 自己那条管线"，
 * 要不要同样接管是另一个决定。
 *
 * TODO（都还没做，也不确定要不要做）：①每源预算 `withTimeout` 拦不住阻塞调用——要让它生效
 * 得**放弃式等待**（被放弃的工作跑在调用方 Job 树之外；`interrupt` 会把 park 打成空转），
 * 并在结论里记一笔以便回收被污染的 daemon；②调试通道要不要同样接管这个人机校验入口。
 */
@Implements(SourceVerificationHelp::class)
class ShadowSourceVerification {

    companion object {
        /** 渲染窗口（秒）。由 `ValidateService.validateOne` 在跑每条源前交过来，
         *  与那条源的**总预算同值**——浏览器这一段不能额外多花时间
         *  （同 `validateByRendering` 的 `timeoutSec * 1000`）。同一块的源共用一个值。 */
        @Volatile
        var budgetSec: Long = 25L

        /** 被拦下的次数（自证用：0 = 这批源一条都没走到人机校验）。 */
        val calls = AtomicInteger(0)

        /** 最近一次：`browser` / `captcha` / `no_browser` / `render_failed` / `challenge`。 */
        val lastMode = AtomicReference("")
        val lastUrl = AtomicReference("")
        val lastNote = AtomicReference("")

        const val MODE_BROWSER = "browser"
        const val MODE_CAPTCHA = "captcha"
        const val MODE_NO_BROWSER = "no_browser"
        const val MODE_RENDER_FAILED = "render_failed"
        const val MODE_CHALLENGE = "challenge"

        const val VERIFY_PREFIX = "本机引擎要过该源的人机校验但没成功——请用「连 App 调试」："
        const val CAPTCHA_REASON = "该源要人工图片验证码，本机引擎无人可认——请用「连 App 调试」"

        fun reset() {
            calls.set(0)
            lastMode.set("")
            lastUrl.set("")
            lastNote.set("")
        }

        /**
         * 渲染结果 → 失败原因（`null` = 这一页可以用）。
         *
         * **纯函数**：策略在这里单独可测，不用起浏览器。挑战页必须算失败——`renderSerial`
         * 等到上界仍没解掉挑战时会把**手里那份如实交回**，照收就成了"拿挑战页当正文"，
         * 源那边只会得到一条 no_result，看着像源坏了（AGENTS #4）。
         */
        internal fun renderFailure(
            ok: Boolean,
            body: String,
            reason: String,
            finalUrl: String,
        ): String? = when {
            !ok -> reason.ifBlank { "浏览器渲染失败" }
            body.isBlank() -> "浏览器渲染出空页面"
            BrowserBridge.isBrowserError(finalUrl, body) -> "浏览器打不开这一页（$finalUrl）"
            BrowserBridge.isChallenge(body) -> "渲染完仍停在人机校验/挑战页"
            else -> null
        }
    }

    @Implementation
    fun getVerificationResult(
        source: BaseSource?,
        url: String,
        title: String,
        useBrowser: Boolean,
        refetchAfterSuccess: Boolean,
        html: String?,
    ): Pair<String, String> {
        calls.incrementAndGet()
        lastUrl.set(url)

        // 图片验证码要人认图，本机没有这个人
        if (!useBrowser) {
            lastMode.set(MODE_CAPTCHA)
            lastNote.set(CAPTCHA_REASON)
            throw BrowserRequiredException(CAPTCHA_REASON)
        }

        val (session, why) = BrowserSession.get()
        if (session == null) {
            val note = "浏览器不可用（$why）"
            lastMode.set(MODE_NO_BROWSER)
            lastNote.set(note)
            throw BrowserRequiredException(VERIFY_PREFIX + note)
        }

        // 与 App 同形：拿这一页（App 是等用户在浏览器里点完再取），窗口 = 该源的总预算
        val rendered = BrowserBridge.renderSerial(session, url, budgetSec * 1000)
        val failure = renderFailure(rendered.ok, rendered.body, rendered.reason, rendered.url)
        if (failure != null) {
            lastMode.set(if (failure.contains("挑战")) MODE_CHALLENGE else MODE_RENDER_FAILED)
            lastNote.set(failure)
            throw BrowserRequiredException(VERIFY_PREFIX + failure)
        }

        lastMode.set(MODE_BROWSER)
        lastNote.set("")
        // App 的 `refetchAfterSuccess` 是"过验证后拿页面再请求一次"；我们手里就是渲染出来的
        // 那一页，直接交回去（`JsExtensions.startBrowserAwait` 要的正是 body）
        return rendered.url.ifEmpty { url } to rendered.body
    }
}

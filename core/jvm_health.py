# -*- coding: utf-8 -*-
"""本机引擎（App 真源码）的结论 → `checks` 那份口径：失败分因、五档和验证阶段。

**为什么要有这一层**：App 引擎回的是 `state` + 异常原文（`reason` / `root` /
`root_stack`），而列表要的是五档健康、验证阶段和逐段结果。这一层只做映射——
判据复用本地的传输归因和错误措辞，不另写第二份（AGENTS #10）。

**两条边界**（照本地口径，不许放宽）：

1. **失败要分因，但不把「这次没验成」判成「源坏了」**：`reset` / `tls` 是被墙特征 →
   需翻墙；`dns` 要拿 :mod:`core.dns_check` 的**外部视角**交叉
   验证（两个公共 DNS 都说域名不存在才判死）；其余（超时、连接被拒、原因不明）一律
   「待验证」——重跑是共同的下一步动作。`classify_transport_error` 的原话：**别在这里
   改成 DEAD**，那等于用一次本机解析失败判死。
2. **引擎自身的问题单列**（Koin 缺绑定、OOM…）：那是**我们没跑成**，不是源的结论
   （AGENTS #4）。档位只能是「待验证」，而且 `error` 里要说明白，否则用户会拿它去改源。

结论行里的异常类名只是**观测**，把它判成哪一档由本模块决定（AGENTS #11：判定里不能
放"我们自己写进去的结论"当输入——那三个字段是引擎原样抛出来的，不是我们的判断）。
"""
from __future__ import annotations

import asyncio
import json
import concurrent.futures
import time
from typing import Any, Dict, List, Optional, Tuple

from core import dns_check
from core.loader import _normalize_url, fingerprint
from core.models import ANTI_BOT_MARKERS, LOGIN_MARKERS, Engine, Health
from core.settings_store import DEPTH_CONTENT, DEPTH_SEARCH, DEPTH_TOC
from core.tags import RULE_INCOMPLETE_TAG

# ---------------------------------------------------- 校验判定表（原 core/checker.py）
#
# 2026-10-18 随消费者并入：本地校验链退场后，本模块（引擎结论 → checks 口径）是
# 这些符号的唯一消费方（判据跟着调用者走）。判据、版本轴与历史理由
# 原样搬来，**不 bump 版本**——符号搬家不改判定口径。别在别处再开第二份定义。

# 缓存版本 8：判定口径新增「验过搜索」这一维（结论行的 `search_probed` 列）。
# 现在库里的条目没有 search_probed 字段，item.get("search_probed") 为假——对开着
# 搜索探测的用户来说，所有历史 OK 缓存都会被判为"没验过搜索"而重验。这正是想要的，
# 但必须**显式**发生：只靠"字段缺失"这个巧合的话，将来若给旧条目补回该字段就会
# 静默失效，而且很难看出是版本号的锅。
#
# 缓存版本 7：反爬特征词表删掉裸 "cloudflare"（它匹配的是 Cloudflare 的邮箱保护
# 脚本，站点当 CDN 用就会被注入，与反爬无关）。判定口径变了——原来被判 auth 的源
# 现在会判 ok，旧缓存里的 health 是旧逻辑的产物，必须整体作废。
# 不作废的后果不是"结果旧一点"，而是**修了等于没修**：那条源的 auth 会在 7 天
# TTL 内一直命中缓存，看起来像修复失效。（v6 是同一理由：正文/目录判定收拢到
# core.quality，底线改为「非空即通过」。）
# 缓存版本 9：4xx 不再算「可达」。`_classify` 原来末尾直接 `return Health.OK`，
# 注释却写「其他 2xx/3xx 视为可达」——404/400/410/451 全被判成「可用」
# （实测库里 91 条是 ok+4xx）。口径变了：原来判 ok 的现在判 dead，
# 旧缓存的 health 是旧逻辑的产物，必须整体作废——理由同 v7，
# **不作废就是「修了等于没修」**（结论会在 TTL 内一直命中旧缓存）。
# 缓存版本 11：登录词表删掉裸 `login` / `sign in`（它们匹配的是页面里的登录**入口**，
# 实测 `m.cread.com` 的首页零反爬词、唯一命中的是 `<a href="/user/login.aspx">`，
# 797 条带 cookieJar 的源因此被判「需验证」），并给「200 + 登录词」判出来的 auth
# 一个短 TTL。口径变了 → 旧缓存里的 auth 是旧逻辑的产物，必须整体作废。
# 缓存版本 10：探测深度从「1/2/3 + 独立的搜索开关」合并成「1/2/3/4 一根轴」，
# 缓存里的 `probe_depth` 是**实际执行到的深度**，所以整列的含义随编号一起平移了
# （旧 1 档 = 域名+搜索 = 新 2 档，旧 2/3 = 新 3/4）。
#
# 严格说旧编号是**单调小于**新编号的，沿用旧行不会放出错误的结论（只会更保守地
# 重验）。但「深度 1 + 搜索开」这档旧行在新口径下必然判「深度不够」，等于整体重跑
# 一遍——把这件事标成版本变化，比让它以「我什么都没改，怎么全量又跑了」的样子
# 出现要好。
# 12：本地回放的能力边界补了两类写法（`text.` / `children.` 简写、方括号索引式
# `[-1]` / `[0]` / `[1,3]` / `[!0]`）。它们此前被判成「解析为空」＝**源失效**，
# 现在一律 unknown（无法离线回放）；同批还修了取值类末段语义（对齐 getResultLast）
# 与 tocUrl 进探针。判定变了，旧缓存里的 toc/content 结论作废。
# 13：健康档位收拢——timeout / error / no_search / skipped 并入 pending
# （「待验证」），判据是下一步动作相同；AUTH / GFW 的标签改名「需登录 / 需翻墙」。
# 结论词表变了：旧缓存里的 health 是旧词表的产物，必须整体作废（checks 表的
# 历史值已在当前管理库外部完成归一；缓存这边直接重探，重跑是已知代价）。
# 14：目录判定改在 **tocUrl 指向的目录页**上做（此前一律在详情页数章节）——
# 「目录在独立页上」的源（实测 1617 条）从「解析为空＝失效」翻成真实章节数，
# 结论方向变了，旧缓存的 toc/content 必须作废。
# 16：**撤掉「证书问题」这一档**（校验收成 App 引擎后它产不出来：App 要么直接
# 通过——不校验证书信任链，要么报 TLS 阻断 → 需翻墙）。存量 cert 行由
# 存量 cert 行已在当前管理库外部并入 pending（动作相同：重跑一次定案），
# 缓存这边整体作废——**词表变了**（少了一个取值）。
# 15：**多了一根轴：结论是谁判的**（`engine`，见 core/models.Engine）。本机引擎
# （App 真源码）的结论写进同一张 checks 表，而它与本地回放的判据强度不同（本地跑不了
# `@js:`、没有登录态、超时 8s vs App 60s）——缓存条目不记出处的话，换个引擎重跑会
# **静默复用**另一台引擎的结论（AGENTS #5b）。
# 17：校验结果改为只保留健康状态和逐段验证事实，移除星级及来源字段。
# 旧缓存即使含有星级，也不能作为新结果形状继续复用。
# 18：校验引擎新增 retry / keyword / root_kind 口径，旧结论不可复用。
# 19：正文豁免口径收严（2026-10-19）：图片/音频源正文规则为空时 content_ok 从
# true 改为 null（豁免不是验证）。旧结论把豁免记成「验证通过」，不可复用。
CACHE_VERSION = 19


def classify_transport_error(error: str) -> str:
    """将传输层错误映射为保守状态，避免断网把源误判为永久失效。

    **DNS 单独说**：这里给的 ``PENDING``（待复检）是**保守兜底**，因为只凭本机一次
    解析失败无法区分「域名注销」和「本机解析被污染/断网」。真正的归因在
    ``health_for`` 里做——它拿 ``core/dns_check`` 的外部视角去交叉验证，验得出来
    才会升级成 ``DEAD``（两个公共 DNS 都说域名不存在）或 ``GFW``（公共 DNS 能解析
    到）。**别在这里改成 DEAD**：那等于用一次本机解析失败判死。
    """
    if error in ("reset", "tls"):
        return Health.GFW
    # timeout / proxy / dns / 其他传输层错误：都是「这次没测出结论」，
    # 下一步动作相同——重跑。原因留在 record.error。
    return Health.PENDING


def dns_verdict_text(verdict: str, note: str) -> Tuple[str, str]:
    """DNS 交叉验证的结论 → ``(健康态, 给用户看的一句话)``。

    **判词只有这一份**：本机引擎那条链（``health_for``）与 DNS 归因
    结论的映射都用它。判定口径在 :mod:`core.dns_check`
    （两个独立来源都同意才判死）；``note`` 说的是"我们观察到了什么"。
    """
    if verdict == dns_check.POLLUTED:
        return (Health.GFW,
                "本机解析失败，但%s，本地 DNS 疑似被污染，可开代理复检" % note)
    if verdict == dns_check.GONE:
        return Health.DEAD, "域名已注销（%s），建议删除" % note
    return Health.PENDING, "DNS 解析失败待复检（%s）" % note


def classify_http_status(status: Optional[int], text: str,
                         enabled_cookie_jar: bool = False) -> str:
    """HTTP 状态码 + 响应体 → 健康态。**全仓库唯一的判定表**。

    需要它的是登录墙判定（`is_login_wall`，`repair/suggest` 的归因走它）。
    原来各写一份，实测已在 5 种输入上分叉：

        503（无响应体）         域名探测=dead    归因=需登录
        404 / 500 / 406         域名探测=dead    归因=继续判
        200 + 登录页 + cookie   域名探测=auth    归因=继续判

    **判定与后果要分开**：本函数只回答「这个响应算什么」，各调用点自己决定怎么用。
    最典型的是搜索入口 404——那**不能**推出「源死了」（可能只是搜索规则过期），
    所以调用点只取它的 AUTH，不套用 4xx → DEAD。
    """
    if status is None:
        return Health.DEAD
    low = (text or "").lower()
    # 401 / 403 / 429 一律 AUTH：它们本身就是「要登录 / 被拒」，不必再看响应体
    if status in (401, 403, 429):
        return Health.AUTH
    # 503 要看响应体：带反爬特征才算「需登录」，否则是服务端挂了
    if status == 503 and any(m in low for m in ANTI_BOT_MARKERS):
        return Health.AUTH
    if status >= 500:
        return Health.DEAD
    if status == 200:
        if any(m in low for m in ANTI_BOT_MARKERS):
            return Health.AUTH
        # 登录墙要看源有没有声明 cookie jar——「请登录」在正常页面的导航栏里太常见
        if any(m in low for m in LOGIN_MARKERS) and enabled_cookie_jar:
            return Health.AUTH
        return Health.OK
    # 2xx / 3xx 都是可达（204、301、302 是大量正常源的形态）
    if 200 <= status < 400:
        return Health.OK
    # 其余 4xx：服务端明确说「这个入口拿不到东西」——不是可达。
    # **原来这里直接 return OK**，而注释写「其他 2xx/3xx 视为可达」，于是
    # 404/400/410/451 全被判成「可用」（实测库里 91 条 ok+4xx）。
    return Health.DEAD


def is_login_wall(text: str, enabled_cookie_jar: bool = False) -> bool:
    """这一页是不是登录墙 / 反爬挑战页。

    判定**完全交给 ``classify_http_status``**（全仓唯一的判定表），这里只回答
    「算不算 AUTH」——状态码固定传 200，因为这是给「已经拿到 HTML 的人」用的：
    403 那类在抓取时就抛了，走不到这里；真正骗人的恰恰是 **200 + 登录页**。

    ``enabled_cookie_jar`` 必须传源自己的：那一档「200 + 登录词」以它为前提
    （「请登录」在正常页面的导航栏里太常见，不声明 cookie 的源不算登录墙）。
    """
    return classify_http_status(200, text, enabled_cookie_jar) == Health.AUTH


def err_desc(err: str, detail: str = "") -> str:
    """失败原因 → 中文描述（用于诊断信息）。

    ``detail`` 是底层异常类名，**只附在「网络异常」这一档上**：dns/timeout/reset/tls/cert
    的中文描述本身已经指向具体成因，而「网络异常」什么也没说——实测那一档底下混着
    连接被拒、对端断开、协议错误等好几种（20 条抽样里 4 种），排查时那句"网络异常"
    等于没有。
    """
    desc = {
        "dns": "DNS解析失败(待交叉验证)",
        "timeout": "连接超时",
        "reset": "连接被重置(典型被墙特征)",
        "tls": "TLS握手失败(可能SNI阻断)",
        "proxy": "代理连接失败",
        "cert": "证书不被信任(自签/过期/域名不匹配)",
        "other": "网络异常",
    }.get(err, "网络异常")
    if err == "other" and detail:
        return "%s（%s）" % (desc, detail)
    return desc


# ------------------------------------------------------------ 静态规则完整度


def _has_rule_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def static_rule_complete(raw: Optional[Dict[str, Any]]) -> Optional[bool]:
    """静态判定书源是否具备最低可用规则；不执行或解释规则文本。

    小说 / 听书 / 漫画源要能搜索到书、解析目录和正文；下载源只需要搜索结果能
    指向目标。书名、列表与详情链接是搜索入口的必要项，作者、封面等展示字段可选。
    不支持的类型或非对象输入返回 None，不能把「无法判定」冒充成「规则不完整」。
    """
    if not isinstance(raw, dict):
        return None
    try:
        source_type = int(raw.get("bookSourceType", 0) or 0)
    except (TypeError, ValueError):
        return None
    if source_type not in (0, 1, 2, 3):
        return None

    search = raw.get("ruleSearch")
    if not isinstance(search, dict):
        search = {}
    search_url = raw.get("searchUrl") or search.get("url")
    search_ready = all(_has_rule_value(value) for value in (
        search_url, search.get("bookList"), search.get("name"), search.get("bookUrl")))
    if not search_ready:
        return False
    if source_type == 3:
        return True

    toc = raw.get("ruleToc")
    content = raw.get("ruleContent")
    toc_list = toc.get("chapterList") if isinstance(toc, dict) else None
    content_rule = content.get("content") if isinstance(content, dict) else None
    return _has_rule_value(toc_list) and _has_rule_value(content_rule)

#: 归因结果。前七个与本地传输层那套**同名同义**（`classify_transport_error` /
#: `err_desc` 直接认它们）；后两个是 App 结论特有的桶。
CAUSE_DNS = "dns"
CAUSE_TIMEOUT = "timeout"
CAUSE_RESET = "reset"
CAUSE_TLS = "tls"
CAUSE_CERT = "cert"
CAUSE_PROXY = "proxy"
CAUSE_OTHER = "other"
#: 源自己的规则 / 配置错（JS 语法错、搜索 URL 为空、取值路径不存在…）：下一步是**修**，
#: 不是重跑，也不是删源。
CAUSE_RULE = "rule"
#: 引擎自身没跑成（Koin 缺绑定、OOM、环境缺口）：**不是源的结论**。
CAUSE_SELF = "self"
#: 源要求人机校验（要人在浏览器里点、或认图片验证码），本机校验无人可等：
#: **不是源的规则错**，下一步是换通道（连 App 调试），不是去改规则。
#: 对应 Kotlin 的 `io.legado.app.service.BrowserRequiredException`。
CAUSE_BROWSER = "browser"

#: `reason` / `root` / `root_stack` 里的特征 → 归因。**顺序有意义**：更具体的先判
#: ——`NoDefinitionFoundException` 的栈里也可能带 `Socket…` 字样；而 `Socket closed`
#: 是本地关掉的连接，不是「连接被重置」（那一类才是被墙特征），所以它落在兜底里。
_CAUSE_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (CAUSE_SELF, ("NoDefinitionFoundException", "ExceptionInInitializerError",
                  "OutOfMemoryError", "Koin")),
    # **必须排在 rule 之前**：Rhino 把它包成 ScriptException，消息里同时有 `ScriptException`
    # 与我们的类名；顺序反了就会判成"源的规则有问题，下一步是修"（实测 2026-10-09）
    (CAUSE_BROWSER, ("BrowserRequiredException",)),
    (CAUSE_RULE, ("ScriptException", "EcmaError", "搜索url不能为空",
                  "PathNotFoundException", "Expected URL scheme",
                  "json string can not be null")),
    (CAUSE_CERT, ("CertPathValidatorException", "CertificateException",
                  "CertificateExpiredException", "证书")),
    (CAUSE_TLS, ("SSLHandshakeException", "SSLException", "SSLProtocolException")),
    (CAUSE_RESET, ("Connection reset", "StreamResetException", "Connection shutdown",
                   "Broken pipe", "Connection closed")),
    (CAUSE_TIMEOUT, ("SocketTimeoutException", "InterruptedIOException",
                     "TimeoutCancellationException", "timed out", "timeout")),
    (CAUSE_DNS, ("UnknownHostException", "UnresolvedAddressException",
                 "No address associated")),
    (CAUSE_PROXY, ("Proxy",)),
    (CAUSE_OTHER, ("Exception", "Error")),
)

#: 结论行的 `stage` → 本地那根深度轴（搜索 2 / 目录 3 / 正文 4）。
_STAGE_DEPTH = {"search": DEPTH_SEARCH, "toc": DEPTH_TOC, "content": DEPTH_CONTENT}

#: 这些 state 本身就把话说清了，不再按异常分因。
_STATE_HEALTH = {
    "ok": Health.OK,
    "login_wall": Health.AUTH,
    #: 搜索页是反爬/验证页（验证码、Cloudflare 挑战）：**不是源的规则错**，也不是
    #: 「没出结果」——需人工过一次验证，本机引擎过不去。动作与 pending 不同
    #: （连 App 试），但它是**站点的临时门**而非源坏了，不下 DEAD 断言，落 PENDING
    #: 的保守档，下一步写在判词里（Kotlin 侧 reason 自带）。
    "human_check": Health.PENDING,
    "no_result": Health.PENDING,
    "timeout": Health.PENDING,
    "empty_js_shell": Health.PENDING,
    "invalid": Health.PENDING,
}


#: Kotlin `classifyRoot` 的码 → 这里的 CAUSE_*。**码表两侧逐词同形**
#: （tests/test_jvm_service_parity.py 钉住，AGENTS #22⑤）；`connect` 在本地口径里
#: 没有单独的桶（与「网络异常」同动作：重跑，一次连接被拒不是「确认不可达」的证据），
#: 落 CAUSE_OTHER。
_ROOT_KIND_TO_CAUSE: Dict[str, str] = {
    "self": CAUSE_SELF,
    "browser": CAUSE_BROWSER,
    "rule": CAUSE_RULE,
    "cert": CAUSE_CERT,
    "tls": CAUSE_TLS,
    "reset": CAUSE_RESET,
    "connect": CAUSE_OTHER,
    "dns": CAUSE_DNS,
    "proxy": CAUSE_PROXY,
    "timeout": CAUSE_TIMEOUT,
    "other": CAUSE_OTHER,
}


def classify_cause(row: Dict[str, Any]) -> str:
    """结论行 → 归因；分不出来返回 ``""``（调用方按保守处理）。

    **优先吃 `root_kind`**——那是 Kotlin 在产生结论的一侧按异常类型/消息判的
    （比字符串匹配稳）。两个口子不短路：`other` 表示 Kotlin 的类型/消息都定不了，
    而字符串匹配知道得更多（rule/self 的消息特征），所以落回原文再判一遍；
    未知码（Kotlin 新增、Python 还没跟）也落回字符串匹配——前向兼容，不让
    码表漂移变成静默错判。
    """
    kind = str(row.get("root_kind") or "")
    if kind and kind != "other":
        cause = _ROOT_KIND_TO_CAUSE.get(kind)
        if cause is not None:
            return cause
    blob = " ".join(str(row.get(k) or "") for k in ("reason", "root", "root_stack"))
    for cause, patterns in _CAUSE_PATTERNS:
        if any(p in blob for p in patterns):
            return cause
    return ""


def _brief(row: Dict[str, Any], limit: int = 80) -> str:
    """异常原文的一小段——写进 `error` 给用户看，细节仍在结论行里。"""
    text = str(row.get("root") or row.get("reason") or "").strip()
    return text[:limit]


def _detail(row: Dict[str, Any]) -> str:
    """底层异常类名（`err_desc` 只在「网络异常」那一档上附它）。"""
    text = str(row.get("reason") or "")
    name = text.split(":", 1)[0].strip()
    return name if name.endswith(("Exception", "Error")) else ""


def _run_probe(probe, host: str) -> Tuple[str, str]:
    """DNS 交叉验证。``probe`` 可注入（测试与调用点都不必联网）。"""
    if probe is not None:
        return probe(host)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 批量校验那条路在 FastAPI 的线程池里，没有事件循环 → 直接起一个
        return asyncio.run(dns_check.probe(host))
    # 已经在事件循环里：`asyncio.run` 会炸，丢到一个临时线程里跑
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(dns_check.probe(host))).result()


def health_for(row: Dict[str, Any], *, host: str = "",
               probe=None) -> Tuple[str, str]:
    """结论行 → ``(五档, 给用户看的原因)``。

    `host` 只在归因为 dns 时用来跑交叉验证（空则维持「待复查」）。
    """
    state = str(row.get("state") or "")
    if state in _STATE_HEALTH:
        # 非 error 的几态：判词直接用结论行自带的那句（它是我们 Kotlin 侧按术语表写的），
        # 没有就留空——**不要在这里另编一句**。ok 行的 reason 也放行：豁免/浏览器渲染
        # 这类「通过但要说清怎么来的」的说明写在 ok 行的 reason 里，压掉它
        # 「豁免不是验证」就到不了用户眼前（AGENTS #4：原因被上层压掉）。
        return _STATE_HEALTH[state], str(row.get("reason") or "")
    cause = classify_cause(row)
    if cause == CAUSE_SELF:
        return Health.PENDING, "本机引擎这次没跑成（不是源的问题）：%s" % _brief(row)
    if cause == CAUSE_BROWSER:
        return Health.PENDING, "需要浏览器/人工完成人机校验，本机引擎过不去（下一步：连 App 调试）：%s" % _brief(row)
    if cause == CAUSE_RULE:
        return Health.PENDING, "源的规则/配置有问题（下一步是修）：%s" % _brief(row)
    if cause == CAUSE_DNS:
        if not host:
            return Health.PENDING, "DNS 解析失败待复查（没有域名可交叉验证）"
        return dns_verdict_text(*_run_probe(probe, host))
    # 传输层：口径与本地**同一份**（reset/tls→需翻墙，其余→待验证）。证书不被信任
    # 也走这里（它落 pending，原因由 err_desc("cert") 写进 error）——那一档已撤

    return classify_transport_error(cause), err_desc(cause, _detail(row))


def _hit_name(row: Dict[str, Any]) -> str:
    """命中的书名（从引擎样本中取第一条用于展示）。"""
    sample = row.get("sample")
    if isinstance(sample, list) and sample:
        return str(sample[0])
    return ""


#: 超时归因的**闭集**（产出侧 `ValidateService.TIMEOUT_BUDGET` / `TIMEOUT_APP`）：
#: budget = 我们的每源总预算用尽（下一步是调参数）；app = App 内部请求超时（源慢）。
#: 逐词比对见 tests/test_jvm_service_parity.py。**认不出的取值原样透传**，不在这里
#: 改写成「未知」——那等于把「对端换了词表」这件事故意藏起来（AGENTS #4）。
TIMEOUT_KINDS = ("budget", "app")


def checks_row(row: Dict[str, Any], *, batch: str, checked_at: str = "",
               raw: Optional[Dict[str, Any]] = None,
               has_search: Optional[bool] = None,
               fp: str = "",
               probe=None) -> Optional[Dict[str, Any]]:
    """一条 App 结论 → 一条 checks 行（形状同 `Store.save_checks` 吃的 dict）。

    ``fp``：库内 `sources.fingerprint` 列的现值——**优先用它**，没有才从 raw 现算。
    指纹是导入侧维护的派生列，这里重算既慢又会掩盖「列没跟上」的 bug
    （AGENTS #11：派生字段只有一个维护点）。

    返回 ``None`` ＝这条结论不该落库（没 URL）。
    """
    url = _normalize_url(str(row.get("url") or ""))
    if not url:
        return None
    host = url.split("//", 1)[-1].split("/")[0] if "//" in url else ""
    health, note = health_for(row, host=host, probe=probe)

    state = str(row.get("state") or "")
    stage = str(row.get("stage") or "search")
    cost_ms = int(row.get("cost_ms") or 0)
    # 搜索**真的跑过**才认它连通（口径同本地：search_response_ms > 0）
    search_ms = cost_ms if state in ("ok", "no_result") else 0
    toc_ok = row.get("toc_complete") if stage in ("toc", "content") else None
    content_ok = row.get("content_ok") if stage == "content" else None
    return {
        "url": url,
        "v": CACHE_VERSION,
        "engine": Engine.JVM,
        "fingerprint": fp or (fingerprint(raw) if raw else ""),
        "health": health,
        # 段级结论（S3-4）：`state` / `stage` / `timeout_kind` 原样落库——以前只把
        # `stage` 折成 `probe_depth`、把原因折成一句 `error` 文本，于是界面上同一条
        # 「超时」分不出是搜索段还是正文段、是我们的预算还是站点慢（动作不同）。
        "state": state,
        "stage": stage,
        "timeout_kind": str(row.get("timeout_kind") or ""),
        "error": note,
        "status_code": None,
        "response_time_ms": cost_ms,
        "search_hit": _hit_name(row),
        "search_response_ms": search_ms,
        "search_probed": bool(search_ms),
        "quality_tags": ([RULE_INCOMPLETE_TAG]
                         if static_rule_complete(raw) is False else []),
        "probe_depth": _STAGE_DEPTH.get(stage, DEPTH_SEARCH),
        "chapter_count": int(row.get("toc_count") or 0),
        "toc_complete": toc_ok,
        "toc_fail_reason": "",
        "content_ok": content_ok,
        "content_fail_reason": "",
        "content_response_ms": None,
        "checked_at": checked_at or time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def checks_rows(rows: List[Dict[str, Any]], *, batch: str, checked_at: str = "",
                sources: Optional[Dict[str, Dict[str, Any]]] = None,
                probe=None) -> List[Dict[str, Any]]:
    """一批 App 结论 → checks 行。

    ``sources``：``{url: {"raw": 源JSON, "fingerprint": 库内指纹列}}``，用来取
    静态规则完整度与指纹（给不出的行也能落库，只是没有规则完整度标签）。
    DNS 交叉验证**按主机缓存**：同一主机的源常常成批出现（实测 35% 的主机有两条以上）。
    """
    cache: Dict[str, Tuple[str, str]] = {}

    def cached_probe(h: str) -> Tuple[str, str]:
        if h not in cache:
            cache[h] = _run_probe(probe, h)
        return cache[h]

    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        url = _normalize_url(str(r.get("url") or ""))
        src = (sources or {}).get(url)
        item = checks_row(r, batch=batch, checked_at=checked_at,
                          raw=(src or {}).get("raw"),
                          fp=str((src or {}).get("fingerprint") or ""),
                          probe=cached_probe)
        if item:
            out.append(item)
    return out

def store_checks(rows: List[Dict[str, Any]], *, batch: str,
                 store=None, probe=None) -> int:
    """把一批 App 结论按 checks 口径落库（含五档 / 深度）+ 重建组名。

    **两条路共用这一份**：产品（``POST /api/jvm/run``）与 CLI
    （``scripts/jvm_readback.py``）——各写一份必然漂，而这个端点的两次 500 都是
    「同一件事两处各写一遍」的产物。

    源数据只查**这一批**涉及的 url：批量落库的成本随批大小走，不随库大小走
    （2026-10-18 前这里是全表扫 raw_json，增量批次也在解析整个库）。指纹读
    `sources.fingerprint` 列，不从 raw 重算——列是导入侧唯一维护点。
    """
    from core.store import Store
    own = store is None
    st = store or Store()
    try:
        urls = {_normalize_url(str(r.get("url") or ""))
                for r in rows or [] if isinstance(r, dict)}
        urls.discard("")
        sources: Dict[str, Dict[str, Any]] = {}
        if urls:
            for r in st.conn.execute(
                    "SELECT source_url, raw_json, fingerprint FROM sources "
                    "WHERE deleted_at = ''"):
                url = _normalize_url(str(r["source_url"] or ""))
                if url not in urls:
                    continue
                try:
                    raw = json.loads(r["raw_json"] or "{}")
                except Exception:
                    raw = {}
                sources[url] = {"raw": raw,
                                "fingerprint": str(r["fingerprint"] or "")}
        items = checks_rows(rows, batch=batch, sources=sources, probe=probe)
        n = st.save_checks(items)
        if n:
            # 组名是从 checks 推出来的派生字段——观测变了就要重建（AGENTS #11）
            st.rebuild_system_tags([it["url"] for it in items])
        return n
    finally:
        if own:
            st.close()

# -*- coding: utf-8 -*-
"""App 调试 key 的拼装——**唯一一份**：权威形态是 Kotlin `Debug.startDebug`
的 when 链（Debug.kt:236-279），Python 是唯一的编码处。

key 是 App `Debug.startDebug`（BookSource 分派，Debug.kt:236-279）的入口语法，
App 按这一串 when 分派：``isAbsUrl`` → 详情起步 / 含 ``::`` → 发现页 /
``++URL`` → 目录起步 / ``--URL`` → 正文起步 / 其余 → 搜索关键词。

**拼错的后果不是报错，是 App 静默无响应**（或整个落进 else 被当关键词搜掉），
所以形态判据集中在这里、有测试钉着（tests/test_debug_keys.py），不再各语言抄一份。
"""

EXPLORE_PREFIX = "发现::"      # App 的分派判据是 contains("::")，不是这个前缀字面量

#: 调试目标（界面词表，前端 DEBUG_TARGETS 与之同名）。错误信息逐字列出合法值，
#: 调用方原样透传给用户——别让它只看到「不合法」三个字
TARGETS = ("search", "explore", "info", "toc", "content")

_TOC_PREFIX = "++"
_CONTENT_PREFIX = "--"


def _strip_once(text: str, prefix: str) -> str:
    # 只去**一次**（等价 Kotlin 的 removePrefix）：用户手抄时常把前缀一起带上，
    # 不去重会拼成 ++++；但也不能循环去——`++++url` 本身是合法输入，去两遍就改了语义
    return text[len(prefix):] if text.startswith(prefix) else text


def build_key(target: str, query: str, explore_url: str = "", keyword: str = "") -> str:
    """目标 + 用户输入 → App 的调试 key。

    **输入为空时的回落是界面承诺的一部分**：详情/目录/正文留空 = 从搜索起步
    （App 自己沿链往下串），所以回落到 ``keyword``；发现留空 = 用源配置的
    ``exploreUrl``，两者都没有就抛 ``ValueError``——静默回落成搜索会把
    「想逛发现页」偷偷变成「搜默认词」，原因必须走到调用方眼前（AGENTS #4）。

    URL 用**原文**（含 `,{...}` 请求选项）：它是要交给 App ``AnalyzeUrl`` 的目标，
    库内那种规范化（AGENTS #5）是关联键的口径，用在这里反而改坏 App 的目标。
    """
    t = str(target or "").strip()
    q = str(query or "").strip()
    if t not in TARGETS:
        raise ValueError("未知的调试目标：%s（只能是 %s）"
                         % (t or "未提供", " / ".join(TARGETS)))
    if t == "explore":
        url = q or str(explore_url or "").strip()
        if not url:
            raise ValueError("这个源没配 exploreUrl，请先填发现页 URL")
        return EXPLORE_PREFIX + url
    if t == "toc":
        return (_TOC_PREFIX + _strip_once(q, _TOC_PREFIX)) if q else _search_start(keyword)
    if t == "content":
        return (_CONTENT_PREFIX + _strip_once(q, _CONTENT_PREFIX)) if q else _search_start(keyword)
    if t == "info":
        return q or _search_start(keyword)
    return q or _search_start(keyword)      # search：空则用默认关键词


#: 步骤名 → 调试目标（`core.debug_keys` 自己那套词表）。渲染升级要按步拼 key，
#: 而「详情」那一步的目标叫 `info`——两套词表不能靠调用方各自记。
STEP_TARGET = {"search": "search", "explore": "explore", "bookUrl": "info",
               "toc": "toc", "content": "content"}

#: URL 规则上的 **webView 选项**（上游 `AnalyzeUrl` 读它，写法与用户手填的完全一样）。
#: 加上它以后这一次取数走 App 的后台 WebView（本机是它的真浏览器 shadow）——页面脚本
#: 因此真的跑一遍：渲染后的 DOM、运行时对象形状、XHR 摘要都是那时才有的事实。
#: 实测 2026-10-28 发发漫画正文页：不带它时 `#commicBox` 里只有 `pic_loading.gif`；
#: 带上以后同一容器里是 34 张真图，引擎取到的正文从 357 字符变成 2000 字符。
WEBVIEW_OPTION = '{"webView":true}'


def render_key(step: str, url: str) -> str:
    """按步骤拼一个**强制走 WebView** 的调试 key（渲染升级用）。

    URL 是**原文**，选项直接拼在它后面——与用户在源里写 `,{"webView":true}` 同一套语法，
    不是我们另发明的一种「渲染参数」：上游按这条语法解析，所以同一条 URL 在调试里与在
    阅读时行为一致。
    """
    target = STEP_TARGET.get(str(step or ""))
    if not target:
        raise ValueError("未知的步骤名：%s（只能是 %s）"
                         % (step or "未提供", " / ".join(sorted(STEP_TARGET))))
    base = str(url or "").strip()
    if not base:
        raise ValueError("没有 URL 就渲染不了——那一步的结果里没有取到页面地址")
    # 已经带过选项就以原样为准（用户/AI 自己加过）：再拼一次会出现两个 `,{...}`，
    # 上游只认最后一个，反而把前面的选项吃掉
    if ",{" in base:
        return build_key(target, base)
    return build_key(target, "%s,%s" % (base, WEBVIEW_OPTION))


def is_content_key(key: str) -> bool:
    """这个调试 key 是不是「从章节点起跑」（= 正文步）。

    `--` 前缀的权威就在本文件（:data:`_CONTENT_PREFIX`）——调用方别自己写
    ``startswith("--")``，否则 App 侧语法一改就有第二处要同步。
    """
    return str(key or "").startswith(_CONTENT_PREFIX)


def _search_start(keyword: str) -> str:
    if not str(keyword or "").strip():
        raise ValueError("没有可用的搜索关键词（设置里的 jvm.keyword 为空）")
    return keyword

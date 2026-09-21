# -*- coding: utf-8 -*-
"""页面侧的定层：**一份判据，两个消费者**（十-5 生成链的编排要用它）。

层与动作的判据原来只有前端一份（`frontend/src/utils/layers.js`），但生成链在 Python 里也要
判「这一页要不要引擎取」——两条路是：两边各写一份（必然漂 + 得配一条逐词比对测试），或者
**只留一份**。留 Python：它本来就在生产 `pages[]`，判完顺手写进 `pages[].layer`，前端**只渲染**。
这与 AGENTS #7/#8（枚举与默认值只在后端一处定义、前端只渲染）是同一条原则。

**分工（别把两边写成同一份）**：

- 这里只判**页面侧**那几档：L1（原文里就有目标）/ L2（容器在但空）/ L3（加密负载）/ L4（接口取数）。
- **L5 需要源声明**（`loginUrl` / `enabledCookieJar`），而**生成的时候还没有源**——所以登录词
  在这里只作为**事实**带出去（`login_marker`），由前端（它握着表单那份源）决定要不要算 L5。
- 前端那半只剩「**源自己声明的能力**」（`ruleContent.webJs` / URL 规则带 `webView`）——那个
  要对正在编辑的表单即时反应，只有前端知道。

**判据全是静态的**：不执行 JS、不解密。每档都给**证据行**（`{why, snippet, line, note}`，
与前端那份同形，界面直接渲染）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.models import LOGIN_MARKERS

#: 目标（「这一步要拿到什么」）——与前端 `STEP_WANT` 是**同一套语义**，两边都在用：
#: 前端还拿它算候选，这里拿它判「这一页在原文里有没有我要的东西」。**层本身只有这一份**。
WANT_LIST = "list"
WANT_LINK = "link"
WANT_TEXT = "text"
WANT_MEDIA = "media"

#: 页面上的「加密负载」痕迹。每条都必须**自证**是加密 / 混淆产物——别用「含 encrypt 字样」
#: 这类宽判据（正文里出现这些词的概率不为零）。
PAGE_MARKERS = [
    # 判据是「一整段 200+ 字符的 base64 字面量」，**不是**「`var X = '…'`」：实测小爱漫画
    # 写成逗号连写（`var config = {…}, params = '<12716 字符>'`），按前一种写法一条都不命中，
    # 一个加密页就被判成了 L2（2026-09-21）。**别收回到「赋值语句」上**。
    ("长 base64 负载", re.compile(r"""['"][A-Za-z0-9+/=]{200,}['"]"""),
     "页面把一大段负载藏在一个字符串里（base64）"),
    ("CryptoJS", re.compile(r"CryptoJS"), "页面用 CryptoJS 自己解密"),
    # 加密库是**外部脚本**时页面上没有 `CryptoJS` 这个标识符，只有这一行 `<script src>`。
    # 只认自证是加密库的名字，别扩成「含 encrypt 字样」。
    ("加密库 script src",
     re.compile(r"""<script[^>]+\bsrc\s*=\s*["'][^"']*(?:crypto-js|cryptojs|jsencrypt"""
                r"""|sm-crypto|aes\.min)[^"']*["']""", re.IGNORECASE),
     "页面引了加密库（解密代码在外部脚本里）"),
    ("_0x 混淆", re.compile(r"_0x[0-9a-f]{4,}"), "脚本被混淆（_0x 变量名）"),
    ("decrypt(", re.compile(r"decrypt\s*\("), "页面里有解密调用"),
    ("xhr_mode", re.compile(r"xhr_mode"), "正文走 XHR 拉取（DOM 里不会有）"),
    ("createObjectURL", re.compile(r"createObjectURL"), "图片/数据是运行时生成的 blob"),
]

#: 接口取数的痕迹：原文里没有目标、但页面在调接口（渲染后才有）。
#: **这一套只跑页面原文**——脚本里的 `fetch(` 遍地都是（框架自己的代码），照它判会把
#: 每个 SPA 都判成 L4；脚本用下面那套更严的。
API_MARKERS = [
    ("fetch(", re.compile(r"\bfetch\s*\("), "页面用 fetch 取数据（渲染后才进 DOM）"),
    ("axios", re.compile(r"\baxios\b"), "页面用 axios 取数据"),
    ("/api/", re.compile(r"/api/"), "页面里有接口路径"),
]

#: **外部脚本**里的接口痕迹。判据是「取数调用 + 带引号的地址」而不是裸的 `fetch(`：
#: 后者在打包产物里几乎每份都有，松一格就会把普通 SPA 全判成 L4，而 L4 的动作
#: （手写接口 + JSONPath）是**要用户动手的**，乱判比判不了贵（2026-09-21）。
API_MARKERS_JS = [
    ("$.ajax({url:", re.compile(r"""\$\.(?:ajax|get|post|getJSON)\s*\(\s*["'{]"""),
     "脚本里有 jQuery 的取数调用"),
    ("fetch(url)", re.compile(r"""\bfetch\s*\(\s*["']"""),
     "脚本里用 fetch 取一个地址"),
    ("axios", re.compile(r"""\baxios\s*(?:\.\s*(?:get|post|request)|\()"""),
     "脚本里用 axios 取数"),
]

#: 「前端框架的挂载点」的空壳形态：<div id="app"></div> / #root / __next
_EMPTY_SHELL_RE = re.compile(
    r"""<(?:div|main|section)[^>]*(?:id|class)=["'][^"']*\b(app|root|__next|__nuxt)\b[^"']*["'][^>]*>\s*</""",
    re.IGNORECASE)


def page_stats(html: str) -> Dict[str, int]:
    """页面的节点统计（与前端 `pageStats` 同一口径，各自的原文各自算）。"""
    text = str(html or "")
    low = text.lower()
    return {
        "links": len(re.findall(r"<a[\s>][^>]*href=", low)),
        "images": len(re.findall(r"<img[\s/>]", low)),
        # 只有「有值的 src」才算真能取到的图：`<img src="">` 是 JS 注入留下的占位
        "images_with_src": len(re.findall(r"""<img[\s>][^>]*src=["'](?!["'])""", text, re.IGNORECASE)),
        "text_len": len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", text))),
    }


def has_wanted(stats: Optional[Dict[str, int]], want: str) -> Optional[bool]:
    """这一页上有没有「你要的那个东西」（门槛取粗：只用来挡住「怎么改选择器都取不到」）。"""
    if not stats or not want:
        return None
    if want == WANT_MEDIA:
        return stats["images_with_src"] > 1        # >1：排除只有 logo 的情况
    if want == WANT_LINK:
        return stats["links"] > 0
    if want == WANT_LIST:
        return stats["links"] > 0 or stats["images"] > 0
    return stats["text_len"] > 200


def _line_of(text: str, index: int) -> int:
    """第几行（1 起）：证据行要能指回去。"""
    return text.count("\n", 0, index) + 1


def _gather(text: str, markers, source: str = "") -> List[Dict[str, Any]]:
    """在某一份材料（页面原文，或页面引用的某一份脚本）上找痕迹。

    ``source`` 指回是哪一份（空 = 页面原文）：结论要能被反驳，就先得说清它是从哪看出来的
    ——脚本里的证据与页面里的长得一样，不标出来用户没法复核（AGENTS #4）。
    """
    out: List[Dict[str, Any]] = []
    for why, rx, note in markers:
        m = rx.search(text)
        if not m:
            continue
        out.append({"why": why, "note": note, "source": source,
                    "snippet": m.group(0)[:120].replace("\n", " ").strip(),
                    "line": _line_of(text, m.start())})
    return out


def _gather_all(page_text: str, js_docs, markers) -> List[Dict[str, Any]]:
    """页面原文 + 页面引用的每份脚本，都用同一套判据过一遍。"""
    out = _gather(page_text, markers)
    for doc in js_docs or []:
        out += _gather(str((doc or {}).get("text") or ""), markers,
                       str((doc or {}).get("url") or ""))
    return out


def classify_page(html: str, want: str = "",
                  js_docs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """判**这一页**属于哪一层（页面侧）。返回：

    - ``layer``：``"L1"`` / ``"L2"`` / ``"L3"`` / ``"L4"`` / ``""``（判不了——既没有目标定义、
      也没有任何痕迹时不硬猜；``unsure`` 里写明为什么）
    - ``evidence``：证据行（``{why, snippet, line, note, source}``；``source`` 空 = 页面原文，
      否则是**哪一份外部脚本**）
    - ``stats`` / ``has_wanted``：页面统计与「有没有目标」
    - ``login_marker``：页面上命中的登录词（**事实**；要不要算 L5 由前端看源声明决定）

    顺序（最具体 → 最一般）：**L3 → L4 → L2 → L1**。加密痕迹优先于接口痕迹——前者是
    「数据要解密」，后者只是「数据要渲染」，下一步动作不同。

    ``js_docs``（可选）是页面引用的**外部脚本**（``core/js_hints.collect_js_docs`` 取回来的
    ``[{url, text}]``）。**为什么它们要进来**：站点把取数 / 解密逻辑放 bundle 里时，页面
    HTML 上一个痕迹都没有——实测小爱漫画章节页就这么被判低了整整一档（2026-09-21）。
    **判据仍然只有这一份**：脚本只是多一份材料，层还是在这里判。
    """
    text = str(html or "")
    stats = page_stats(text)
    wanted = has_wanted(stats, want)
    login = next((w for w in LOGIN_MARKERS if w in text), "")
    out: Dict[str, Any] = {"layer": "", "evidence": [], "stats": stats,
                           "has_wanted": wanted, "login_marker": login}
    if not text:
        out["unsure"] = "这一步没有页面 HTML"
        return out

    enc = _gather_all(text, js_docs, PAGE_MARKERS)
    if enc:
        out["layer"] = "L3"
        out["evidence"] = enc
        return out

    if wanted is False:
        api = _gather(text, API_MARKERS) + _gather_all("", js_docs, API_MARKERS_JS)
        if api:
            out["layer"] = "L4"
            out["evidence"] = api
            return out
        # 容器在但空：图片源最常见（`<img>` 有、地址是页面脚本注入的）；框架挂载点空壳同理
        if want == WANT_MEDIA and stats["images"] > 0 and stats["images_with_src"] <= 1:
            out["layer"] = "L2"
            out["evidence"] = [{"why": "图片容器在、但图没有地址",
                                "note": "地址由页面脚本给（渲染后进 DOM，或干脆只在接口里）",
                                "source": "", "snippet": "<img>", "line": 0}]
            return out
        m = _EMPTY_SHELL_RE.search(text)
        if m:
            out["layer"] = "L2"
            out["evidence"] = [{"why": "框架挂载点是空的（" + m.group(1) + "）",
                                "note": "数据要页面脚本跑起来才有", "source": "",
                                "snippet": m.group(0)[:120].strip(),
                                "line": _line_of(text, m.start())}]
            return out
        seen = len(js_docs or [])
        out["unsure"] = ("原文里没有目标，也没有可判的痕迹" if not seen else
                         "原文与引用的 %d 份脚本里都没有目标，也没有可判的痕迹" % seen)
        return out

    if wanted is True:
        out["layer"] = "L1"
    else:
        out["unsure"] = "没给「这一步要什么」，判不了 L1"
    return out

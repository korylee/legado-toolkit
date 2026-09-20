# -*- coding: utf-8 -*-
"""URL 工具。从 add_source 抽出，避免 core 反向依赖 services。"""

import json
import re
from typing import Any, Dict, Tuple
from urllib.parse import urljoin

#: Legado 的 URL 选项分隔：``url,{json}``。逗号两侧允许空白（实测源里见过）。
#: 与 ``AnalyzeUrl.paramPattern`` 同语义，见 core/checker.py 的历史注释。
_URL_OPTION_RE = re.compile(r"\s*,\s*(?=\{)")


def split_url_options(rule: str) -> Tuple[str, Dict[str, Any]]:
    """把 ``url,{json}`` 拆成 ``(url, 选项 dict)``。

    **JSON 解不出来时照样把 URL 切下来**、选项当空——这是**对齐 App**，不是随手：
    Legado 先按 `paramPattern` 切出 `urlNoOption` 拿去发请求，**之后**才解析选项；
    解析失败只是不应用选项，URL 已经被切了（`AnalyzeUrl.kt:219-231`）。

    从 checker 抽来：它是对 URL 语法的解析，不是校验逻辑，而补抓
    （core/app_debug.py）也要用它且不该为此拉进 checker 的依赖链。
    """
    text = rule or ""
    m = _URL_OPTION_RE.search(text)
    if not m:
        return text, {}
    url = text[:m.start()]
    try:
        opt = json.loads(text[m.end():])
    except Exception:
        return url, {}
    return url, opt if isinstance(opt, dict) else {}


def abs_url(base_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base_url or "", href)


def rule_url(base_url: str, raw_value: str) -> str:
    """把**规则产出的 URL**变成可直接请求的地址：先剥 `,{json}` 选项，再补绝对地址。

    两件事必须一起做。选项里装的是 **App 才懂的东西**（`webView` / `method` /
    `body`），带上它去发请求必然拿不到页面——实测
    `.../1.html,{"webView":true}` 返回 **404**，同一个地址剥掉选项后返回 **200**。
    后果不是「少验一点」：`_probe_content` 会把 404 写成
    `正文请求失败(status=404)`，读起来像**源坏了**，而其实是我们拼错了地址
    ——AGENTS #4 的同一类错：不能把我们的问题说成源的问题。

    与 `core/app_debug.py` 里那条**保留选项尾巴**的用法不同：那边要把 URL 连同
    选项一起当身份存起来（`absolute + tail`），这里只要一个能发的地址。
    """
    pure, _opts = split_url_options(str(raw_value or ""))
    return abs_url(base_url, pure)

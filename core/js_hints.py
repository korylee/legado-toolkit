# -*- coding: utf-8 -*-
"""页面引用的外部脚本：**只负责取回来 + 说清取了哪几份**，判读交给 `core/page_layer`。

为什么需要它（2026-09-21 实测）：小爱漫画章节页的图片列表在解密后的
`params.chapter_images` 里，而解密函数 `CMS.chapter.decrypt` 全在外部 `cms-2.0.1.min.js`
里——那一页的 HTML 里连 `CryptoJS` 三个字母都没有。判据只读页面 HTML，就会把这一页
判成 L2（真档位是 L3）：**逻辑藏在 bundle 里的站一律会被低估一档。**

边界（= 判据的输入面，都是实测出来的）：

- **只取同站**：跨站的 `edge.<域名>/script.js` 那类多半是统计脚本（实测那份就是），
  取回来只会把上限挤掉。
- **常见库跳过**：jquery / crypto-js 这类库自身不含站点逻辑，而**用**它们的那份
  bundle 才是线索（跳掉库还能省下几十 KB 与一次请求）。
- **硬上限**：最多 ``MAX_DOCS`` 份、每份 ``MAX_BYTES`` 字节。取不到的、跳过的都留一行
  原因——证据行要能说清「这一页我们看了哪几份材料，没看哪几份、为什么」。
- **判读不在这里**：这里不判层，只交材料（判据只有一份，在 `core/page_layer.py`）。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from core.urls import abs_url

#: 最多取几份脚本。超过就按下面的排序取前几份——**排序是为了让重要的那几份排进上限**。
MAX_DOCS = 3
#: 单份脚本最多读多少字节（`.min.js` 动辄几十 KB，这个上限是「够找线索」而不是「读全」）
MAX_BYTES = 256 * 1024
#: 取脚本的超时（秒）
TIMEOUT = 10

#: `<script src="...">`——引号有双有单，也有不带引号的写法。
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]*\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""",
                            re.IGNORECASE)

#: 常见库 / 统计脚本：**自证是通用代码**，不含站点自己的取数逻辑。
#: 判据是「换任何一个站都会引它」——别把 `cms-2.0.1.min.js` 这类站点自己的 bundle 加进来
#: （名字里带 `.min` 不是判据）。
LIB_HINTS = (
    "jquery", "zepto", "lodash", "underscore", "bootstrap", "popper",
    "vue.min", "vue.js", "react", "preact", "angular", "polyfill",
    "crypto-js", "cryptojs", "jsencrypt", "sm-crypto", "md5.min", "sha256.min",
    "moment", "dayjs", "swiper", "layui", "tailwind", "animate.min",
    "analytics", "gtag", "ga.js", "tongji", "hm.js", "cnzz", "51.la",
)

#: 文件名里带这些词的脚本**更可能**是站点的取数逻辑（排序用，不是过滤条件）。
TITLE_HINTS = (
    "cms", "chapter", "content", "comic", "book", "novel", "reader", "read",
    "list", "detail", "view", "api", "data", "app", "site", "main", "global", "common",
)

#: ``fetcher(url) -> html``：默认就是 ``core.fetch.fetch``（这里不 import，测试好替换）。
Fetcher = Callable[[str], str]


def _site_key(host: str) -> str:
    """粗粒度「同一个站」：取最后两段（``edge.a.com`` 与 ``www.a.com`` 同为 ``a.com``）。"""
    parts = [p for p in str(host or "").lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


def _rank(path: str) -> int:
    """有题材词的排前面（实测那份关键 bundle 叫 `cms-2.0.1.min.js`）。"""
    low = str(path or "").lower()
    return 1 if any(h in low for h in TITLE_HINTS) else 0


def script_srcs(html: str, base_url: str) -> List[str]:
    """页面引用的外部脚本（绝对地址、去重、保序）。"""
    out: List[str] = []
    for m in _SCRIPT_SRC_RE.finditer(str(html or "")):
        raw = next((g for g in m.groups() if g), "")
        url = abs_url(base_url, raw.strip())
        if url.startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out


def collect_js_docs(html: str, base_url: str, fetcher: Optional[Fetcher] = None,
                    max_docs: int = MAX_DOCS, max_bytes: int = MAX_BYTES,
                    timeout: int = TIMEOUT) -> Dict[str, Any]:
    """取回页面引用的那几份脚本。返回：

    - ``docs``：``[{"url", "text", "truncated"}]``——交给 ``page_layer.classify_page``
    - ``skipped``：``[{"url", "why"}]``——跳过的每一份都要有原因（界面上能看见）
    - ``considered``：页面上引用的脚本总数（含跳过与没轮上的）

    取不到就是取不到：**这里不抛**，失败只留一行原因（判层的调用方按「这份没看到」
    处理，绝不能因为一个附属脚本抓不到就把整页判成「没目标」）。
    """
    urls = script_srcs(html, base_url)
    base_host = re.sub(r":\d+$", "", (str(base_url or "").split("//")[-1].split("/")[0]))
    base_site = _site_key(base_host)
    docs: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    candidates: List[str] = []
    for url in urls:
        host = re.sub(r":\d+$", "", url.split("//")[-1].split("/")[0])
        path = url.split("//")[-1]
        low = path.lower()
        if _site_key(host) != base_site:
            skipped.append({"url": url, "why": "不是本站的脚本"})
            continue
        if any(h in low for h in LIB_HINTS):
            skipped.append({"url": url, "why": "常见库或统计脚本"})
            continue
        candidates.append(url)
    candidates.sort(key=lambda u: -_rank(u))
    if fetcher is None:
        from core.fetch import fetch
        fetcher = lambda u: fetch(u, timeout=timeout)  # noqa: E731  取页器与全仓一致
    for url in candidates[:max(0, max_docs)]:
        try:
            text = str(fetcher(url) or "")
        except Exception as e:
            skipped.append({"url": url, "why": "抓不到（%s）" % _short(e)})
            continue
        cut = len(text) > max_bytes
        docs.append({"url": url, "text": text[:max_bytes], "truncated": cut})
    for url in candidates[max(0, max_docs):]:
        skipped.append({"url": url, "why": "超过每次最多看 %d 份的上限" % max_docs})
    return {"docs": docs, "skipped": skipped, "considered": len(urls)}


def _short(err: Any, limit: int = 60) -> str:
    return re.sub(r"\s+", " ", str(err or ""))[:limit]


#: 轻判落在这两档时，才值得为脚本多打几次请求：L1 已经定了、L3/L4 已经由页面上的痕迹
#: 定下来了，再看脚本也改不了结论（**这是「省请求」的那道闸**）。
DEEP_LAYERS = ("L2", "")


def classify_with_scripts(html: str, want: str, base_url: str,
                          fetcher: Optional[Fetcher] = None) -> Dict[str, Any]:
    """判层；**只在轻判落在 L2 / 判不了时**才取回页面引用的脚本再判一次。

    返回 ``{"verdict", "light_layer", "docs", "skipped", "considered"}``：

    - ``verdict``：最终的判层结果（判据仍在 `core/page_layer.py`，这里只补材料）
    - ``light_layer``：只看页面 HTML 时的层；与 ``verdict["layer"]`` 不同才说明
      **这次是脚本改了结论**，调用方该把这件事写进备注（「结论是在什么材料上得出的」）
    - ``docs`` / ``skipped`` / ``considered``：这次看了哪几份、跳过了哪几份

    **两条通道（生成链、抽屉）都走这一个函数**：判据与「什么时候才去看脚本」都只有一份，
    否则同一页会在两个通道里被判成两层（AGENTS #22⑤ 那一类）。
    """
    from core.page_layer import classify_page

    verdict = classify_page(html, want)
    out: Dict[str, Any] = {"verdict": verdict, "light_layer": verdict.get("layer", ""),
                           "docs": [], "skipped": [], "considered": 0}
    if verdict.get("layer") not in DEEP_LAYERS:
        return out
    got = collect_js_docs(html, base_url, fetcher=fetcher)
    out.update({"docs": got["docs"], "skipped": got["skipped"],
                "considered": got["considered"]})
    if got["docs"]:
        out["verdict"] = classify_page(html, want, js_docs=got["docs"])
    return out

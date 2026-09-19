# -*- coding: utf-8 -*-
"""目录页地址的解析：`ruleBookInfo.tocUrl` 是**规则**，不是 URL。

**为什么单独一个模块**：这件事原本有两处实现，只有一处做对了——

  - `core/verify.py`（试跑 / 修复循环）：在详情页上求值取目录页地址 ✅
  - `core/checker.py::_probe_toc`（写 `checks` 的全量校验）：**根本没看 tocUrl**，
    直接在详情页数章节 ❌

后果不是"少验一点"，而是**假结论**：库里 1617 条源的目录在独立页上，它们被
判成 `toc_complete=False`（「解析结果为空」＝看起来像源坏了），实测 SF轻小说
本地判 0 章、JVM 用 App 引擎在同一个源上拿到 1201 章。这正是 lessons §二十三
「同一件事可能走两个函数」的第二次实证——所以判定逻辑收在这一处，
两边都调它，不再各写一份。

**对齐的口径**（App 的 `BookInfo.kt` 里 `analyzeRule.getString(infoRule.tocUrl,
isUrl = true)` 那一段，按符号查而非行号）：

  1. `tocUrl` 为空 → 目录就在详情页上（App 求值为空时回退 `baseUrl`）
  2. `tocUrl` 是纯地址（`https://…`）→ 直接用它（相对地址按详情页拼接）
  3. `tocUrl` 是规则 → **在详情页的 HTML 上求值**，取第一个非空结果（相对地址
     按详情页拼接）；求值为空同样回退详情页
  4. 规则**本地调试不了** → 返回 `None` + 原因，调用方必须判 **unknown**，
     不能退回详情页假装没事：App 能求值而我们求不了，那是我们的能力边界
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from core.rules.replayer import extract_all_nodes, rule_supported
from core.urls import abs_url
from core import quality as Q

#: 纯地址：`https://…`，且不含模板/JS/规则语法。
#: 实测库里 121 条是这种写法、1496 条是规则——两者要分开处理。
_PLAIN_URL_RE = re.compile(r"^https?://\S+$")


def is_plain_url(value: str) -> bool:
    v = str(value or "").strip()
    return bool(_PLAIN_URL_RE.match(v)) and "{" not in v and "<js" not in v and "@js:" not in v


def resolve_toc_page(
    raw: Dict[str, Any],
    detail_html: str,
    detail_url: str,
) -> Tuple[Optional[str], str]:
    """从详情页解析出目录页地址。

    返回 ``(目录页地址, 原因)``：

    - 成功：``(url, "")``——可能是详情页本身（`tocUrl` 为空或求值为空）
    - 求值不了：``(None, "…规则不支持本地调试…")``——调用方必须判 unknown

    `detail_html` 为空也不报错：纯地址分支不需要页面，规则分支会自然求值为空
    并回退详情页（与 App 的兜底一致）。
    """
    rule = str((raw.get("ruleBookInfo") or {}).get("tocUrl", "") or "").strip()
    if not rule:
        return detail_url, ""
    if is_plain_url(rule):
        return abs_url(detail_url, rule), ""
    ok, why = rule_supported(rule)
    if not ok:
        return None, "tocUrl 规则不支持本地调试：%s" % why
    vals, _hits, _err = extract_all_nodes(
        detail_html or "", rule, Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
    picked = next((str(v).strip() for v in vals if str(v or "").strip()), "")
    return (abs_url(detail_url, picked) if picked else detail_url), ""

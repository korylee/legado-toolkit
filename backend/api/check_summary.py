# -*- coding: utf-8 -*-
"""任务结果的**形状**：两条校验路（本地 `ops.run_check_job` / 本机引擎 `jvm.run_jvm_job`）
给前端的 `items` / `transitions` 必须一模一样。

为什么单开一个模块：前端读这套键的是**同一份代码**（`parseCheckResult` /
`applyCheckResults`）——「一条源点校验」走哪条路，界面上不该看得出来。两处各写一份
必然漂，而漂的表现是「某几条源校验完列表不更新」，看不出来是谁的问题（lessons §二十三）。

两个 builder 的入参不同（一个吃 Record、一个吃 checks 行），但**吐出来的键必须完全相同**：
`tests/test_check_summary.py` 有一条断言逐键比对（同形状不是靠注释保证的）。
"""
from typing import Any, Dict, Iterable, List, Optional

from core.loader import _normalize_url

#: 「变成 X」的**明细**最多带多少条（计数不受影响）。全量校验时变化可能有几千条，
#: 明细全带上会把 result_json 撑到几百 KB——而它是要走 SSE 推送的
CHANGED_ITEMS_LIMIT = 200

#: 结果体里带多少条明细（前端就地回填要看它们）。与 CHANGED_ITEMS_LIMIT 分开：
#: 那个是「变成 X」的明细，这个是**本次全部结论**——放太多会把 result_json 撑大
ITEMS_LIMIT = 500

#: 前端 `applyCheckResults` 逐字段回填的就是这些键。**加字段要两条路一起加**，
#: 否则另一条路回填时会把那一列清空（它就是按 key 取的）
ITEM_KEYS = ("url", "name", "health", "stars", "star_basis", "error",
             "toc_complete", "content_ok", "search_hit", "checked_at")


def check_items_from_records(results: Iterable[Any]) -> List[Dict[str, Any]]:
    """本地校验那条：`BookSourceRecord` 列表 → items。"""
    return [{
        # url 必须**归一化**后再下发：前端拿它当 key 回填列表，而列表里的
        # `source_url` 是 Store 归一化后存的（去空白/尾斜杠/转小写），而 `r.url`
        # 是 build_record 从 bookSourceUrl 直接取的原文——两侧不归一的话前端
        # 一条都匹配不上，表现是「校验完了列表不更新」，且看不出任何异常（lessons §五）
        "url": _normalize_url(r.url), "name": r.name, "health": r.health,
        "stars": r.quality_stars, "star_basis": r.star_basis, "error": r.error,
        "toc_complete": r.toc_complete, "content_ok": r.content_ok,
        "search_hit": r.search_hit, "checked_at": r.checked_at,
    } for r in results]


def check_items_from_checks(checks: Dict[str, Dict[str, Any]],
                            names: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """本机引擎那条：`Store.checks_map()` 的行 → items。

    引擎结论**已经按 checks 口径落库**（六档 / 星级 / 深度，`core/jvm_health`），
    这里只做键名映射（`quality_stars` → `stars`，见 `ITEM_KEYS`）——**不再算一遍**
    六档与星级：那是 `jvm_health` 的事，两份实现必然漂。

    ``names``：`{url: 源名}`。checks 行里不存名字，而「变成 X」的明细要显示它。
    """
    out: List[Dict[str, Any]] = []
    for url, d in (checks or {}).items():
        key = _normalize_url(str(d.get("url") or url))
        out.append({
            "url": key,
            "name": (names or {}).get(key, ""),
            "health": d.get("health"),
            "stars": d.get("quality_stars"),
            "star_basis": d.get("star_basis"),
            "error": d.get("error") or "",
            "toc_complete": d.get("toc_complete"),
            "content_ok": d.get("content_ok"),
            "search_hit": d.get("search_hit") or "",
            "checked_at": d.get("checked_at") or "",
        })
    return out


def summarize_transitions(prev: Dict[str, Dict[str, Any]],
                          items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """统计本次校验相对**上一次结论**的变化，供任务结果展示。

    ``prev`` 是跑之前读的 ``Store.checks_map()``（``{url: item}``，键已规范化）；
    ``items`` 是 :func:`check_items_from_records` / :func:`check_items_from_checks`
    的产出（**收 dict**：两条路都走它，不再要求对象带属性）。

    **两侧的 URL 必须用同一套规范化再比**：``prev`` 的键在写库时就归一了，而
    items 里的 url 是抓取时的原文（实测 20.7% 的源带尾斜杠）。不归一的话那些源会被
    当成「首次有结论」，把没变的说成变了。

    「首次有结论」与「变成 X」**分开计**：库里绝大多数源从未校验过，第一次全量
    之后「新增可用 2000 条」不代表比上次好，那是首次。
    """
    first_checked = 0
    changed: Dict[str, int] = {}
    changed_items: List[Dict[str, str]] = []
    for it in items:
        url = _normalize_url(str(it.get("url") or ""))
        health = it.get("health")
        old = (prev.get(url) or {}).get("health")
        if old is None:
            # 之前没有校验记录，这次有了结论 → 首次，不进 changed
            first_checked += 1
        elif old != health:
            # 只统计**变了**的，按新的 health 分桶
            changed[health] = changed.get(health, 0) + 1
            # 明细。**只有计数是不够的**：摘要说「6 条变成失效」，用户下一步
            # 一定是问「哪 6 条」——只给计数等于让他自己去 3800 行里翻。
            # 超出上限时只截明细，**计数仍然准确**（前端靠两者对比报「还有 N 条」）
            if len(changed_items) < CHANGED_ITEMS_LIMIT:
                changed_items.append({"url": url, "name": it.get("name") or url,
                                      "from": old, "to": health})
    return {"first_checked": first_checked, "changed": changed,
            "changed_items": changed_items}

# -*- coding: utf-8 -*-
"""调试任务行 → 材料（**唯一一份**，端点和 AI 补全任务都从这里取）。

「材料在后端、前端只报引用」是同一条纪律：前端传 ``job_id + step``，谁需要材料谁
在这里取。API 层和任务体各抄一遍的后果不是报错，是两边对「哪一步、哪一页、缺材料
时说什么」的判断慢慢分开——那时界面上会同时出现两种说法。

失败一律给 HTTP 语义的 400/404（调用方是端点；任务体拿到的是同一个异常，`runner`
会把它记成任务失败原因）。**不静默返回空 HTML**：空材料必须能分辨出「没有任务」
「没有这一步」「这一步没抓到页面」（AGENTS #4）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from fastapi import HTTPException


def job_source(job: Any) -> Dict[str, Any]:
    """任务行里的源 JSON（rule_try 的求值要用完整 BookSource）。"""
    payload = (job or {}).get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    source = (payload or {}).get("source") if isinstance(payload, dict) else None
    if not isinstance(source, dict) or not source:
        raise HTTPException(400, "任务里没有源 JSON")
    return source


def step_health(result: Any) -> Dict[str, str]:
    """**每步的材料体检**：``{步名: 码}``，码 ∈ ``ok`` / ``interstitial`` / ``browser_error``。

    为什么要有它：调试结果里四段的材料都在，而 AI 补全一次只看**当前段**
    （`debug_step_material` 按 step 取）。反爬往往只拦某一段（搜索页被拦、正文页正常），
    只看本段就回答不了「这个源到底哪一步没法写规则」。

    判据**复用** `core.quality` 的两个标记函数（一份判据，AGENTS #10）：
    拦截页与浏览器错误页的下一步动作不同，所以是两个码、不是一句笼统的「异常」。
    """
    from core.quality import browser_error_marker, interstitial_marker

    steps = (result or {}).get("steps") if isinstance(result, dict) else None
    pages = (result or {}).get("pages") if isinstance(result, dict) else None
    by_id = {str(p.get("id") or ""): p for p in (pages or []) if isinstance(p, dict)}
    out: Dict[str, str] = {}
    for step in (steps or []):
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "")
        if not name or name in out:
            continue
        html = str((by_id.get(str(step.get("page_id") or "")) or {}).get("html") or "")
        if not html:
            continue                      # 没材料不判（读不到 ≠ 材料坏了）
        if interstitial_marker(html):
            out[name] = "interstitial"
        elif browser_error_marker(html):
            out[name] = "browser_error"
        else:
            out[name] = "ok"
    return out


def debug_step_material(job: Any, job_id: str,
                        step_name: str) -> Tuple[str, str, Dict[str, Any], str,
                                                 Dict[str, Any], Dict[str, Any]]:
    """从任务行取一步的材料：``(html, base_url, result, kind, structured, runtime)``。

    ``kind`` 是**材料种类**（判据在 `core.jvm_debug.material_kind`，结果已判在
    `page["material_kind"]` 上）：`rendered_dom` / `webjs_output` / `static_html` /
    `unknown`（读不到渲染事实）。这份「整页」在页面脚本生效时其实是那段脚本的返回值
    （实测 27150 字节全是图片地址、没有任何标签）——消费方拿它解 HTML 会得到空结构，
    而**看起来像「这页没有结构」**。所以种类必须跟着材料一起交出去。

    ``structured`` / ``runtime`` 是这一步引擎侧的两份材料（没有就是空 dict）：消费方要的是
    **我们查出来的那一行**，不是自己再从 ``result`` 里找一遍——同一段「哪一步、拿哪个」
    的判据长两份，漂起来只会是两边对同一步给出不同结果。

    ``runtime`` 的 `globals` 是页面里候选数据源（全局对象/数组）的**形状**：正文的地址
    常常不在 DOM 里，而在某个全局变量上，而变量名不能猜（`params` 只是某个站点的叫法）。
    """
    if not job:
        raise HTTPException(404, "没有这个任务：%s" % job_id)
    try:
        result = json.loads(job.get("result_json") or "{}")
    except ValueError:
        result = {}
    steps = result.get("steps") if isinstance(result, dict) else None
    step = next((s for s in (steps or [])
                 if isinstance(s, dict) and s.get("name") == step_name), None)
    if not step:
        raise HTTPException(404, "任务 %s 的结果里没有 %s 步" % (job_id, step_name))
    page_id = str(step.get("page_id") or "")
    page = next((p for p in (result.get("pages") or [])
                 if isinstance(p, dict) and p.get("id") == page_id), None)
    html = str((page or {}).get("html") or "")
    if not html.strip():
        raise HTTPException(400, "这一步没有页面材料，先跑一次完整调试")
    from core.jvm_debug import material_kind

    structured = step.get("structured") if isinstance(step.get("structured"), dict) else {}
    runtime = step.get("runtime") if isinstance(step.get("runtime"), dict) else {}
    return (html, str(step.get("url") or ""), result,
            material_kind(page), structured, runtime)

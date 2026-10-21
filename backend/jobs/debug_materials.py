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


def debug_step_material(job: Any, job_id: str,
                        step_name: str) -> Tuple[str, str, Dict[str, Any]]:
    """从任务行取一步的材料：``(html, base_url, result)``。"""
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
    return html, str(step.get("url") or ""), result

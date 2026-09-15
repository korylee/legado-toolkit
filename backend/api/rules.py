# -*- coding: utf-8 -*-
"""规则试跑接口：按完整链路回放 source。"""

import asyncio

from fastapi import APIRouter, HTTPException

from backend.schemas import AppDebugRequest, RuleChainTest

router = APIRouter()


@router.post("/chain")
async def chain_test(body: RuleChainTest):
    from core.verify import verify_chain

    source = dict(body.source or {})
    if not source.get("bookSourceUrl"):
        raise HTTPException(400, "缺少 bookSourceUrl")
    try:
        return await asyncio.to_thread(
            verify_chain, source, body.keyword or "我",
            body.detail_url or "", int(body.pick or 1),
        )
    except Exception as e:
        raise HTTPException(400, "试跑失败: %s: %s" % (type(e).__name__, e))


@router.post("/app-debug")
async def app_debug(body: AppDebugRequest):
    """连 App 跑一次调试：借阅读 App 的调试 WebSocket 走完整链路（含 JS 规则）。

    返回体与 ``/rules/chain`` **同形状**（steps / pages），前端抽屉与卡片零改动。

    `tag` **必须用 ``bookSourceUrl`` 的导入原文**：App 的
    ``getBookSource(tag)?.let{}`` 查不到源就什么都不做——表现为静默无响应，
    排查成本极高。详情接口返回的对象里这个字段就是原文，所以这里不做任何
    规范化（尤其不要 ``rstrip("/")`` / ``lower()``）。
    """
    from core.app_debug import run_app_debug

    source = dict(body.source or {})
    tag = str(source.get("bookSourceUrl", "") or "").strip()
    if not tag:
        raise HTTPException(400, "缺少 bookSourceUrl")
    host = str(body.host or "").strip()
    if not host:
        raise HTTPException(400, "缺少 App 的 IP（App 通知栏里有）")
    try:
        # run_app_debug 是同步阻塞的（标准库 socket 收发），必须让出事件循环。
        # 连不上/超时它自己会返回带 error 的结果体（不抛），
        # 这里兜的是脏端口之类的入参异常——同样给 400，不给 500。
        return await asyncio.to_thread(
            run_app_debug, host, tag, body.key or "我",
            body.port or None, 60, source,
        )
    except Exception as e:
        raise HTTPException(400, "连 App 调试失败: %s: %s" % (type(e).__name__, e))

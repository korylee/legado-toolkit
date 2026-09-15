# -*- coding: utf-8 -*-
"""规则试跑接口：按完整链路回放 source。"""

import asyncio

from fastapi import APIRouter, HTTPException

from backend.schemas import (
    AppDebugRequest,
    AppHostRequest,
    ReplayStepRequest,
    RuleChainTest,
)

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
    if body.push:
        # 调试 WS 的 tag 是拿去 App 库里精确匹配的，库里没有这个源就静默无响应。
        # 先推一次（App 侧是 REPLACE，幂等），新源/改过还没保存的源就都能调试了。
        from core.app_debug import push_source

        ok, err = await asyncio.to_thread(push_source, host, source, body.port or None)
        if not ok:
            return {"error": "推送到 App 失败：%s" % (err or "未知原因")}
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


@router.post("/app-preflight")
async def app_preflight(body: AppHostRequest):
    """连 App 调试前的预检。

    把以前那个「点完等 60 秒、什么都不发生」拆成能对症的状态：
    连不上 / App 里没有这个源 / App 里是旧版本 / 可以直接调试。

    传**完整 source**（不是只传 URL）：要拿它和 App 里那份比对规则，
    才能发现「App 里有，但是旧版本」——那种情况下直接调试跑的是旧规则，
    结果看着正常、答的却不是你在改的东西。
    """
    from core.app_debug import preflight

    source = dict(body.source or {})
    if not str(source.get("bookSourceUrl", "") or "").strip():
        raise HTTPException(400, "缺少 bookSourceUrl")
    return await asyncio.to_thread(
        preflight, str(body.host or "").strip(), source, body.port or None)


@router.post("/app-push")
async def app_push(body: AppHostRequest):
    """把源推送到 App（幂等）。

    **会改动用户 App 里的书源数据**——新增或覆盖同 URL 的源。所以只由用户
    显式点击触发，任何流程都不得自动调用。失败不抛 400，把原因放在 body 里，
    前端好就地提示。
    """
    from core.app_debug import push_source

    source = dict(body.source or {})
    host = str(body.host or "").strip()
    if not host:
        return {"ok": False, "error": "没有填 App 的 IP"}
    ok, err = await asyncio.to_thread(push_source, host, source, body.port or None)
    return {"ok": ok, "error": err}


@router.post("/replay-step")
async def replay_rule_step(body: ReplayStepRequest):
    """用已抓到的 HTML 重放一步规则，**不发网络请求**。

    改完规则想立刻看判定变化时用它：试跑结果里已经存了每页 HTML，
    不必重跑整条链（那要重新联网搜索）。
    """
    from core.verify import replay_step

    try:
        return await asyncio.to_thread(
            replay_step, body.html, body.rule, body.step, body.source_type)
    except Exception as e:
        raise HTTPException(400, "重放失败: %s: %s" % (type(e).__name__, e))

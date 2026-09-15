# -*- coding: utf-8 -*-
"""规则试跑接口：按完整链路回放 source。"""

import asyncio

from fastapi import APIRouter, HTTPException

from backend.schemas import RuleChainTest

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

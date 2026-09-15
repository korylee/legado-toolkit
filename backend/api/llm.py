# -*- coding: utf-8 -*-
"""LLM 配置管理接口。"""

import time

from fastapi import APIRouter, HTTPException

from backend.schemas import LLMProfileIn, LLMProfilePatch
from core import llm_store

router = APIRouter()


@router.get("/presets")
def presets():
    return llm_store.PRESETS


@router.get("/status")
def status():
    return llm_store.status()


@router.get("/profiles")
def list_profiles():
    return llm_store.list_profiles(mask=True)


@router.post("/profiles")
def create_profile(body: LLMProfileIn):
    p = llm_store.create_profile(body.model_dump())
    return llm_store.sanitize_profile(p)


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: str, body: LLMProfilePatch):
    p = llm_store.update_profile(profile_id, body.model_dump(exclude_unset=True))
    if not p:
        raise HTTPException(404, "配置不存在")
    return llm_store.sanitize_profile(p)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str):
    if not llm_store.delete_profile(profile_id):
        raise HTTPException(404, "配置不存在")
    return {"deleted": True}


@router.post("/profiles/{profile_id}/activate")
def activate_profile(profile_id: str):
    if not llm_store.set_active(profile_id):
        raise HTTPException(404, "配置不存在")
    return {"ok": True, "active": profile_id}


@router.post("/profiles/{profile_id}/test")
async def test_profile(profile_id: str):
    from core.repair.llm import LLMClient
    p = llm_store.get_profile(profile_id, mask=True)
    if not p:
        raise HTTPException(404, "配置不存在")
    client = LLMClient(profile_id=profile_id)
    started = time.time()
    try:
        reply = await client.chat("你是一个连接测试助手。", "只回复：pong")
        return {
            "ok": bool(reply),
            "model": client.config.model,
            "latency_ms": int((time.time() - started) * 1000),
            "reply": (reply or "")[:200],
        }
    except Exception as e:
        return {"ok": False, "model": client.config.model,
                "latency_ms": int((time.time() - started) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}

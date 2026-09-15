# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容 /chat/completions）。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMConfig:
    """LLM 配置：优先来自 JSON store，其次环境变量。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: float = 90.0, temperature: float = 0.2,
                 max_tokens: int = 0, extra_headers: Optional[Dict[str, Any]] = None,
                 extra_body: Optional[Dict[str, Any]] = None,
                 provider: str = "openai-compatible"):
        self.base_url = (base_url or os.getenv("LEGADO_LLM_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.getenv("LEGADO_LLM_API_KEY") or ""
        self.model = model or os.getenv("LEGADO_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = float(timeout or 90.0)
        self.temperature = float(temperature if temperature is not None else 0.2)
        self.max_tokens = int(max_tokens or 0)
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self.provider = provider or "openai-compatible"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def build_llm_config(profile_id: str = "") -> LLMConfig:
    """从 JSON store 读取指定/默认配置，失败时回退环境变量。"""
    try:
        from core.llm_store import get_effective_profile
        p = get_effective_profile(profile_id)
    except Exception:
        p = None
    if not p:
        return LLMConfig()
    api_key = p.get("api_key", "") or os.getenv(p.get("api_key_env") or "", "")
    return LLMConfig(
        base_url=p.get("base_url", ""),
        api_key=api_key,
        model=p.get("model", ""),
        timeout=p.get("timeout", 90),
        temperature=p.get("temperature", 0.2),
        max_tokens=p.get("max_tokens", 0),
        extra_headers=p.get("extra_headers") or {},
        extra_body=p.get("extra_body") or {},
        provider=p.get("adapter") or p.get("provider") or "openai-compatible",
    )


class LLMClient:
    """异步客户端。未配置 api_key 时 chat() 返回 None，调用方据此走 dry-run。"""

    def __init__(self, config: Optional[LLMConfig] = None, profile_id: str = ""):
        self.config = config or build_llm_config(profile_id)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def chat(self, system: str, user: str, session: Any = None,
                   temperature: Optional[float] = None) -> Optional[str]:
        if not self.config.enabled:
            return None
        import aiohttp

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        temp = self.config.temperature if temperature is None else temperature
        if temp is not None:
            payload["temperature"] = temp
        if self.config.max_tokens > 0:
            payload["max_tokens"] = self.config.max_tokens
        payload.update(self.config.extra_body or {})
        headers = {"Authorization": "Bearer " + self.config.api_key,
                   "Content-Type": "application/json"}
        headers.update(self.config.extra_headers or {})
        url = self.config.base_url + "/chat/completions"
        own = session is None
        if own:
            session = aiohttp.ClientSession()
        try:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError("LLM HTTP %s: %s" % (resp.status, body[:300]))
                data = json.loads(body)
                return data["choices"][0]["message"]["content"]
        finally:
            if own:
                await session.close()


def extract_json(text: Optional[str]) -> Any:
    """从模型输出里抠出第一个合法 JSON（兼容 ```json 包裹和前后废话）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        chunks = t.split("```")
        if len(chunks) >= 2:
            t = chunks[1]
        if t.lower().lstrip().startswith("json"):
            t = t.lstrip()[4:]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = t.find(open_ch)
        j = t.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    try:
        return json.loads(t)
    except Exception:
        return None

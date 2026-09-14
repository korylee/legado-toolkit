# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容 /chat/completions）。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMConfig:
    """环境变量：LEGADO_LLM_BASE_URL / LEGADO_LLM_API_KEY / LEGADO_LLM_MODEL。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: float = 90.0):
        self.base_url = (base_url or os.getenv("LEGADO_LLM_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.getenv("LEGADO_LLM_API_KEY") or ""
        self.model = model or os.getenv("LEGADO_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class LLMClient:
    """异步客户端。未配置 api_key 时 chat() 返回 None，调用方据此走 dry-run。"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def chat(self, system: str, user: str, session: Any = None,
                   temperature: float = 0.2) -> Optional[str]:
        if not self.config.enabled:
            return None
        import aiohttp

        payload = {
            "model": self.config.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        headers = {"Authorization": "Bearer " + self.config.api_key,
                   "Content-Type": "application/json"}
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


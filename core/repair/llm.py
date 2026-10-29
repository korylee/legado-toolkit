# -*- coding: utf-8 -*-
"""LLM 客户端：三条接口协议一个调用入口。

| ``protocol`` | 请求 | 正文取值 | 备注 |
| :--- | :--- | :--- | :--- |
| ``openai`` | ``POST {base}/chat/completions`` | ``choices[0].message.content`` | 国内外兼容端点与本地推理服务 |
| ``responses`` | ``POST {base}/responses`` | ``output[].content[].text`` | OpenAI 新接口 |
| ``anthropic`` | ``POST {base}/messages`` | ``content[].text`` | 需 ``x-api-key`` + ``anthropic-version``，``max_tokens`` 必填 |

``last_usage`` 一律归一成 ``prompt_tokens`` / ``completion_tokens`` /
``prompt_cache_hit_tokens`` 三项——界面（``AgentRunDialog``）与调用账按这三个读，
三家字段名不同，差异只在这里处理，别让消费方按协议分支。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

#: 只有「一道配置都没有、只有环境变量」时才用这两个兜底值。配置里 ``base_url`` /
#: ``model`` 是必填的：按协议猜地址只会把请求发去错的地方。
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

PROTOCOL_PATHS = {
    "openai": "/chat/completions",
    "responses": "/responses",
    "anthropic": "/messages",
}

ANTHROPIC_VERSION = "2023-06-01"
#: Anthropic 的 ``max_tokens`` 必填，缺了直接 400；界面不再暴露这个参数，
#: 固定一个够用的值（一轮提议的输出远小于它）。
ANTHROPIC_MAX_TOKENS = 4096
#: 生成温度不再可配（没有真实调参场景）。Responses 那条不传，见 :meth:`LLMClient._request`。
TEMPERATURE = 0.2


class LLMConfig:
    """一套模型配置：协议、地址、Key、模型、超时。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: float = 90.0, protocol: str = "openai"):
        if protocol not in PROTOCOL_PATHS:
            protocol = "openai"
        self.protocol = protocol
        self.base_url = (
            base_url or os.getenv("LEGADO_LLM_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = api_key or os.getenv("LEGADO_LLM_API_KEY") or ""
        self.model = model or os.getenv("LEGADO_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = float(timeout or 90.0)

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
    return LLMConfig(
        base_url=p.get("base_url", ""),
        api_key=p.get("api_key", ""),
        model=p.get("model", ""),
        timeout=p.get("timeout", 90),
        protocol=p.get("protocol", "openai"),
    )


def error_is_transient(exc: BaseException) -> bool:
    """这次失败是**偶发**（值得重试）还是**配置/内容错**（重试也白搭）。

    判据只有一条：**重试同一次请求会不会得到不同结果**。

      - 偶发：超时、连接被重置、5xx、限流 —— 同一次请求再发一遍可能就成
        （实测 2026-10-27：正常响应 6–16 秒、超时 90 秒，长尾照样会撞上，
        撞上一次不该让整段 AI 停手）；
      - 配置/内容错：没配 key、模型名不存在、认证失败、请求体不合法 ——
        原样重发一遍必然还是同样的错，重试只是把失败推迟。

    判据按**异常类型与状态码**认，不按错误文案（文案随 provider 变）。
    """
    import asyncio

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    # aiohttp 的连接类异常（ClientError 家族）都是「这次没连上 / 断了」
    try:
        import aiohttp

        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError,
                            aiohttp.ServerDisconnectedError)):
            return True
    except Exception:
        pass
    # HTTP 层的 `RuntimeError("LLM HTTP %s: ...")`：只有 5xx 与 429 值得重试。
    # 4xx（401 认证 / 404 模型名 / 400 请求体）重发必然一样。
    text = str(exc)
    if text.startswith("LLM HTTP "):
        try:
            code = int(text[len("LLM HTTP "):].split(":", 1)[0].strip())
        except (ValueError, IndexError):
            return False
        return code >= 500 or code == 429
    return False


def error_is_rate_limited(exc: BaseException) -> bool:
    """这次失败是不是**限流**（429）：是的话重试前要等，立刻重发必然还是 429。

    与 :func:`error_is_transient` 分开：那是「值不值得再发一次」，这是「再发之前要不要等」。
    两者的答案可以不同——5xx 立刻重发没问题，429 不等就白重试。
    """
    text = str(exc)
    if not text.startswith("LLM HTTP "):
        return False
    try:
        return int(text[len("LLM HTTP "):].split(":", 1)[0].strip()) == 429
    except (ValueError, IndexError):
        return False


def _reply_text(data: Dict[str, Any], protocol: str) -> str:
    """从响应体里取正文——三家形状不同，只在这里分叉。"""
    if protocol == "anthropic":
        return "".join(
            str(block.get("text") or "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if protocol == "responses":
        parts = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return data["choices"][0]["message"]["content"]


def _normalize_usage(raw: Any, protocol: str) -> Dict[str, int]:
    """三家的 usage 字段名不同，归一成界面与账本读的那三个。"""
    u = raw if isinstance(raw, dict) else {}
    if protocol == "anthropic":
        hit = u.get("cache_read_input_tokens") or 0
    elif protocol == "responses":
        hit = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    else:
        hit = u.get("prompt_cache_hit_tokens")
        if hit is None:
            hit = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or u.get("output_tokens") or 0),
        "prompt_cache_hit_tokens": int(hit or 0),
    }


class LLMClient:
    """异步客户端。未配置 api_key 时 chat() 返回 None，调用方据此走 dry-run。"""

    def __init__(self, config: Optional[LLMConfig] = None, profile_id: str = ""):
        self.config = config or build_llm_config(profile_id)
        #: 最近一次调用的 usage（已归一，见 :func:`_normalize_usage`）。
        #: **不动 chat() 的返回签名**：三个既有调用方（修复循环 / CLI / 设置页的
        #: 测试按钮）要的都是字符串，为一个可选字段改签名不值当。失败时清空。
        self.last_usage: Dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _request(self, system: str, user: str,
                 temperature: Optional[float]) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
        c = self.config
        url = c.base_url + PROTOCOL_PATHS[c.protocol]
        if c.protocol == "anthropic":
            payload = {
                "model": c.model,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            payload["temperature"] = (
                TEMPERATURE if temperature is None else temperature
            )
            headers = {
                "x-api-key": c.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            }
        elif c.protocol == "responses":
            # 不传 temperature：推理模型（gpt-5 / o 系列）只接受默认值，传了直接 400，
            # 而这些模型正是走 Responses 的那批。
            payload = {"model": c.model, "instructions": system, "input": user}
            headers = {"Authorization": "Bearer " + c.api_key,
                       "Content-Type": "application/json"}
        else:
            payload = {
                "model": c.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": TEMPERATURE if temperature is None else temperature,
            }
            headers = {"Authorization": "Bearer " + c.api_key,
                       "Content-Type": "application/json"}
        return url, payload, headers

    async def chat(self, system: str, user: str, session: Any = None,
                   temperature: Optional[float] = None) -> Optional[str]:
        if not self.config.enabled:
            return None
        self.last_usage = {}
        import aiohttp

        url, payload, headers = self._request(system, user, temperature)
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
                self.last_usage = _normalize_usage(
                    data.get("usage"), self.config.protocol
                )
                return _reply_text(data, self.config.protocol)
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

# -*- coding: utf-8 -*-
"""LLM 配置 JSON 存储（本地单用户，原子写）。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from core.paths import data_path

CONFIG_NAME = "llm_profiles.json"
VERSION = 2

PRESETS: List[Dict[str, Any]] = [
    {"provider": "openai", "name": "OpenAI",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"provider": "deepseek", "name": "DeepSeek",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"provider": "moonshot", "name": "Moonshot / Kimi",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"provider": "dashscope", "name": "阿里云百炼 / 通义",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"provider": "zhipu", "name": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"provider": "openrouter", "name": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"},
    {"provider": "siliconflow", "name": "SiliconFlow",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct"},
    {"provider": "groq", "name": "Groq",
     "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    {"provider": "ollama", "name": "Ollama 本地",
     "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    {"provider": "lmstudio", "name": "LM Studio 本地",
     "base_url": "http://127.0.0.1:1234/v1", "model": "local-model"},
]


def config_path() -> str:
    env = os.getenv("LEGADO_LLM_CONFIG")
    if env:
        return env
    d = data_path("config")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, CONFIG_NAME)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty() -> Dict[str, Any]:
    return {"schema_version": VERSION, "active_profile_id": "", "profiles": []}


def load() -> Dict[str, Any]:
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    out = _empty()
    out["active_profile_id"] = str(data.get("active_profile_id") or data.get("active") or "")
    for item in profiles:
        if isinstance(item, dict):
            out["profiles"].append(_normalize(item))
    return out


def _atomic_save(data: Dict[str, Any]) -> None:
    path = config_path()
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name or "").strip().lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def _unique_id(profiles: List[Dict[str, Any]], base: str) -> str:
    ids = {p.get("id") for p in profiles}
    if base not in ids:
        return base
    i = 2
    while "%s-%d" % (base, i) in ids:
        i += 1
    return "%s-%d" % (base, i)


def _normalize(profile: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(profile or {})
    if not str(p.get("id") or "").strip():
        p["id"] = _slug(str(p.get("name") or "profile"))
    p["id"] = str(p["id"])
    if not str(p.get("name") or "").strip():
        p["name"] = p["id"]
    if not str(p.get("provider") or "").strip():
        p["provider"] = "openai-compatible"
    if not str(p.get("base_url") or "").strip():
        p["base_url"] = "https://api.openai.com/v1"
    p.setdefault("api_key", "")
    p.setdefault("api_key_env", "")
    p.setdefault("adapter", "openai-compatible")
    if not str(p.get("model") or "").strip():
        p["model"] = "gpt-4o-mini"
    if not p.get("created_at"):
        p["created_at"] = _now()
    if not p.get("updated_at"):
        p["updated_at"] = p["created_at"]
    p.setdefault("temperature", 0.2)
    p.setdefault("timeout", 90)
    p.setdefault("max_tokens", 0)
    p.setdefault("extra_headers", {})
    p.setdefault("extra_body", {})
    p.setdefault("enabled", True)
    p.setdefault("sort_order", 0)
    return p


def _env_profile() -> Optional[Dict[str, Any]]:
    key = os.getenv("LEGADO_LLM_API_KEY") or ""
    if not key:
        return None
    return _normalize({
        "id": "env",
        "name": "环境变量",
        "provider": "openai-compatible",
        "base_url": os.getenv("LEGADO_LLM_BASE_URL") or "https://api.openai.com/v1",
        "api_key": key,
        "model": os.getenv("LEGADO_LLM_MODEL") or "gpt-4o-mini",
        "sort_order": -1,
    })


def sanitize_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(profile or {})
    key = str(out.get("api_key") or "")
    out["api_key_set"] = bool(key)
    out["api_key_masked"] = ("****" + key[-4:]) if len(key) > 4 else ("****" if key else "")
    out.pop("api_key", None)
    return out


def list_profiles(mask: bool = True) -> List[Dict[str, Any]]:
    profiles = load()["profiles"]
    return [sanitize_profile(p) if mask else p for p in profiles]


def get_profile(profile_id: str, mask: bool = False) -> Optional[Dict[str, Any]]:
    for p in load()["profiles"]:
        if p.get("id") == profile_id:
            return sanitize_profile(p) if mask else p
    return None


def get_effective_profile(profile_id: str = "", mask: bool = False) -> Optional[Dict[str, Any]]:
    data = load()
    target = None
    if profile_id:
        target = next((p for p in data["profiles"] if p.get("id") == profile_id), None)
    active_id = data.get("active_profile_id") or data.get("active") or ""
    if target is None and active_id:
        target = next((p for p in data["profiles"] if p.get("id") == active_id), None)
    if target is None and data["profiles"]:
        target = data["profiles"][0]
    if target is None:
        target = _env_profile()
    if target is None:
        return None
    return sanitize_profile(target) if mask else target

def create_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = load()
    p = _normalize(payload)
    p["id"] = _unique_id(data["profiles"], p["id"])
    p["created_at"] = p.get("created_at") or _now()
    p["updated_at"] = _now()
    data["profiles"].append(p)
    if not data.get("active_profile_id"):
        data["active_profile_id"] = p["id"]
    _atomic_save(data)
    return p


def update_profile(profile_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = load()
    for p in data["profiles"]:
        if p.get("id") != profile_id:
            continue
        for key, value in (patch or {}).items():
            if key == "api_key" and not value:
                continue
            if key in ("id", "created_at"):
                continue
            p[key] = value
        p.update(_normalize(p))
        p["updated_at"] = _now()
        _atomic_save(data)
        return p
    return None


def delete_profile(profile_id: str) -> bool:
    data = load()
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
    if len(data["profiles"]) == before:
        return False
    if data.get("active_profile_id") == profile_id:
        data["active_profile_id"] = data["profiles"][0]["id"] if data["profiles"] else ""
    _atomic_save(data)
    return True


def set_active(profile_id: str) -> bool:
    data = load()
    if not any(p.get("id") == profile_id for p in data["profiles"]):
        return False
    data["active_profile_id"] = profile_id
    _atomic_save(data)
    return True


def status() -> Dict[str, Any]:
    data = load()
    active = get_effective_profile(mask=True)
    return {
        "active": active,
        "active_profile_id": (active or {}).get("id", ""),
        "schema_version": int(data.get("schema_version") or VERSION),
        "count": len(data["profiles"]),
        "has_env_fallback": bool(_env_profile()),
    }

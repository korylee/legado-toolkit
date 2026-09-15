# -*- coding: utf-8 -*-
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceOut(BaseModel):
    id: int
    source_url: str
    name: str = ""
    source_type: int = 0
    group_name: str = ""
    user_tags: str = ""
    system_tags_locked: int = 0
    enabled: int = 1
    fingerprint: str = ""
    health: Optional[str] = None
    stars: Optional[int] = None
    checked_at: Optional[str] = None
    probe_depth: Optional[int] = None
    toc_complete: Optional[int] = None
    content_ok: Optional[int] = None
    search_hit: Optional[str] = None


class SourcePage(BaseModel):
    total: int
    items: List[SourceOut]


class GroupPatch(BaseModel):
    url: str
    group: str


class TagPatch(BaseModel):
    urls: List[str] = Field(default_factory=list)
    add: List[str] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)


class TagRename(BaseModel):
    old: str
    new: str


class TagMerge(BaseModel):
    sources: List[str] = Field(default_factory=list)
    target: str


class TagDelete(BaseModel):
    tag: str


class SourceSave(BaseModel):
    source: Dict[str, Any]
    user_tags: List[str] = Field(default_factory=list)
    lock_system_tags: bool = False


class RuleChainTest(BaseModel):
    source: Dict[str, Any]
    keyword: str = "我"
    detail_url: str = ""
    pick: int = 1


class AppDebugRequest(BaseModel):
    """连 App 调试（借阅读 App 内建的调试 WebSocket 跑一次完整链路）。

    `source` 里的 ``bookSourceUrl`` 会被后端**直接当调试 tag 用**——它必须是
    导入原文（详情接口返回的源对象就是原文，前端不用做任何转换）；
    换成列表里的 ``source_url``（规范化过）App 会查不到源而静默无响应。
    """

    source: Dict[str, Any]
    key: str = "我"
    host: str = ""
    #: 0 = 用默认端口（App 的 HTTP 端口 1122 + 1 = 1123）
    port: int = 0


class LLMProfileIn(BaseModel):
    id: str = ""
    name: str = ""
    provider: str = "openai-compatible"
    adapter: str = "openai-compatible"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: float = 90
    max_tokens: int = 0
    extra_headers: Dict[str, Any] = Field(default_factory=dict)
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sort_order: int = 0


class LLMProfilePatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    adapter: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[float] = None
    max_tokens: Optional[int] = None
    extra_headers: Optional[Dict[str, Any]] = None
    extra_body: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class JobCreate(BaseModel):
    kind: str = Field(description="check / diagnose / repair")
    payload: Dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: str
    kind: str = ""
    status: str = ""
    progress: int = 0
    total: int = 0
    created_at: str = ""
    updated_at: str = ""
    result_json: str = ""

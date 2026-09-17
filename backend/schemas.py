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
    #: 这个星级是实测来的还是按静态规则推的（"measured" / "static" / ""）。
    #: **必须声明在这里**：response_model 会按模型裁字段，漏了它就静默丢掉，
    #: 前端那边表现为「这个词永远不显示」——查起来很像前端 bug
    star_basis: Optional[str] = None
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


class SourceDeleteIn(BaseModel):
    """批量软删除的请求体。

    urls 走 body 而不是查询串。**阈值实测**（库副本 + uvicorn）：约 1600 条 / 57KB
    通过，2000 条 / 72KB 被服务端以 400 拒绝，而全库 3850 条拼起来约 139KB——
    「全选全部」正好落在会撞上的那一档。形状与既有的 ``POST /sources/restore`` 一致。
    """
    urls: List[str] = Field(default_factory=list)
    reason: str = ""


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


class ImportBody(BaseModel):
    content: str = ""
    source: str = ""


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
    #: 调试前先把源推送到 App。**会改动用户 App 里的书源数据**（新增或覆盖），
    #: 所以必须由用户显式触发，不要在流程里默认打开
    push: bool = False


class AppHostRequest(BaseModel):
    """只需要 host 的 App 侧操作（预检 / 推送）。"""

    source: Dict[str, Any]
    host: str = ""
    port: int = 0


class ReplayStepRequest(BaseModel):
    """用已抓到的 HTML 重放一步规则。"""

    html: str = ""
    rule: str = ""
    step: str = ""
    source_type: int = 0


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


class CheckSettingsPatch(BaseModel):
    """校验参数（core.settings_store.DEFAULTS["check"]）的局部更新。

    全部 Optional 且默认 None：只提交显式给出的键，其余保持原值。
    字段名必须与 settings_store 的键一致——**这里不做任何校验或收敛**，
    区间 clamp／类型／非法回落一律由 ``settings_store.coerce`` 负责。
    在本模型上再写一份 rules 就是同一口径的第二个出处，必然漂移。
    """

    concurrency: Optional[int] = None
    timeout: Optional[float] = None
    # 一档对一级星级（1 主页 / 2 搜索 / 3 目录 / 4 正文）。合并前这里还有一个
    # probe_search 开关，与深度是两根轴——已去掉
    probe_depth: Optional[int] = None
    verify_ssl: Optional[bool] = None
    proxy: Optional[str] = None
    cache_ttl_ok: Optional[int] = None
    cache_ttl_other: Optional[int] = None


class SettingsPatch(BaseModel):
    """按 section 分组，与 settings_store 的文件结构一一对应（不做映射层）。"""

    check: Optional[CheckSettingsPatch] = None


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


class NamePreviewIn(BaseModel):
    """名称清洗的预演请求。

    urls 为空 = 全库未删除的源。**只读**：预演不改任何数据。
    """
    urls: List[str] = Field(default_factory=list)


class NameChange(BaseModel):
    """一对待应用的改名（apply 传"新名"，undo 传"旧名"）。"""
    url: str
    name: str


class NameApplyIn(BaseModel):
    changes: List[NameChange] = Field(default_factory=list)


class NameUndoIn(BaseModel):
    prev: List[NameChange] = Field(default_factory=list)

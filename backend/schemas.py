# -*- coding: utf-8 -*-
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceOut(BaseModel):
    id: int
    source_url: str
    name: str = ""
    source_type: int = 0
    group_name: str = ""
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

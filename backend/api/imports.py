# -*- coding: utf-8 -*-
# 外部书源安全导入：解析前端回传的 JSON，按 URL + 规则指纹三分类。
#
# 语义（对齐 CLI 的 import-sources，但落地到 SQLite 管理库）：
#   - 新 URL         -> 写入管理库，按原分组推断健康状态（无信号默认待验证）
#   - 同 URL 同规则   -> 重复，跳过
#   - 同 URL 不同规则 -> 冲突，不覆盖候选库；原始内容留存 data/imports/conflicts/
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException

from backend.deps import get_store
from backend.schemas import ImportBody
from core.loader import _normalize_url, fingerprint
from core.models import BOOK_SOURCE_TYPE_NAMES
from core.organizer import STATUS_GROUP_NAMES, infer_health_from_group
from core.paths import data_path
from core.sanitize import clean_source
from core.tags import extract_user_tags_from_group, merge_group

router = APIRouter()


def _normalize_group(source):
    """为导入的新源规范系统标签：按原分组推断健康状态，无信号默认「待验证」。

    保留外部源的健康状态（可用/失效/需代理复检等），用户标签原样保留。
    """
    source_type = int(source.get("bookSourceType", 0) or 0)
    type_tag = BOOK_SOURCE_TYPE_NAMES.get(source_type, "")
    raw_group = str(source.get("bookSourceGroup", "") or "")
    user_tags = extract_user_tags_from_group(raw_group)
    status_tag = STATUS_GROUP_NAMES.get(infer_health_from_group(raw_group), "待验证")
    source["bookSourceGroup"] = merge_group([type_tag, status_tag], user_tags)
    return source


def _write_conflicts(conflicts):
    """把冲突源原样留存到文件，返回文件路径（无冲突返回空串）。"""
    if not conflicts:
        return ""
    out_dir = data_path("imports", "conflicts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "conflict_%s.json"
                        % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conflicts, f, ensure_ascii=False, indent=2)
    return path


@router.post("")
def import_sources(body: ImportBody, st=Depends(get_store)):
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, "JSON 解析失败: %s" % e)
    if not isinstance(data, list):
        raise HTTPException(400, "内容不是书源数组（应为 JSON 数组）")

    new_count = duplicate_count = conflict_count = invalid_count = 0
    conflict_urls = []
    conflict_sources = []

    for item in data:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        clean_source(item)
        url = _normalize_url(str(item.get("bookSourceUrl", "") or ""))
        if not url:
            invalid_count += 1
            continue

        existing_fp, is_deleted = st.get_source_fingerprint(url)
        if existing_fp is None:
            _normalize_group(item)
            st.upsert_sources([item], allow_new_tags=True)
            if is_deleted:
                st.restore([url])
            new_count += 1
        elif fingerprint(item) == existing_fp:
            duplicate_count += 1
        else:
            conflict_count += 1
            conflict_urls.append(url)
            conflict_sources.append(item)

    return {
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "conflict_count": conflict_count,
        "invalid_count": invalid_count,
        "total": len(data),
        "conflict_urls": conflict_urls,
        "conflict_file": _write_conflicts(conflict_sources),
    }

# -*- coding: utf-8 -*-
# 外部书源安全导入：解析前端回传的 JSON，按 URL + 规则指纹三分类。
#
# 语义（**外部源导入的唯一实现**——CLI 那条独立的台账链路已于 2026-09-19 删除，
# 它写的是另一个库、且那个库在本机根本不存在）：
#   - 新 URL         -> 写入管理库，按原分组推断健康状态（无信号默认待验证）
#   - 同 URL 同规则   -> 重复，跳过
#   - 同 URL 不同规则 -> 冲突，不覆盖候选库；原始内容留存 data/imports/conflicts/
import json
import os
import time
from typing import Any, Dict, List

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


def _normalize_group(source, allowed_user_tags):
    """为导入的新源规范系统标签：按原分组推断健康状态，无信号默认「待验证」。

    保留外部源的健康状态（可用/失效/需代理复检等）；用户标签只保留白名单里的
    （``Store.known_user_tags()``：默认 R18 / 正版 + 库里已有的用户标签），
    其余直接清掉——决策见 lessons §三十二。
    """
    source_type = int(source.get("bookSourceType", 0) or 0)
    type_tag = BOOK_SOURCE_TYPE_NAMES.get(source_type, "")
    raw_group = str(source.get("bookSourceGroup", "") or "")
    user_tags = [t for t in extract_user_tags_from_group(raw_group)
                 if t in allowed_user_tags]
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

    new_count = duplicate_count = conflict_count = invalid_count = updated_count = 0
    overwrite = str(getattr(body, "conflict_strategy", "keep") or "keep") == "overwrite"
    conflict_urls = []
    conflict_sources = []

    # 导入边界的用户标签白名单：默认标签 + 库里已有标签。
    # 同一份名单在 Store.upsert_sources 里是第二道防线。
    allowed_user_tags = st.known_user_tags()

    # **一次把在用的指纹全捞进来**，别在循环里逐条查。逐条查 + 逐条写实测是
    # O(n²)：3000 条要走 3000 次 SELECT、3000 次事务提交，60 秒（每条 20ms）。
    # 分类全在内存里做，写入合并到循环后的两次调用——那是同一个数量级里最省的
    # 改法：upsert_sources 每次调用内部还要全表扫一遍 known_user_tags()，
    # 调 3000 次就是扫 3000 遍。
    live_fp = st.live_fingerprints()
    to_create: List[Dict[str, Any]] = []
    to_overwrite: List[Dict[str, Any]] = []

    for item in data:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        clean_source(item)
        url = _normalize_url(str(item.get("bookSourceUrl", "") or ""))
        if not url:
            invalid_count += 1
            continue

        # 只比「在用」的那行。回收站里同 URL 的历史版本**不参与**——它们不在用，
        # 不构成「已存在」，于是「删了再导入」会直接新建一行在用的（旧版留在回收站
        # 可回滚），而不是被判冲突挡在门外。
        existing_fp = live_fp.get(url) or None
        if existing_fp is None:
            _normalize_group(item, allowed_user_tags)
            to_create.append(item)
            new_count += 1
            # 同一份文件里出现两次同一个 URL 时，第二次要算「重复」而不是又建一遍——
            # 批量化之后循环里不再落库，所以得自己把状态补上
            live_fp[url] = fingerprint(item)
        elif fingerprint(item) == existing_fp:
            duplicate_count += 1
        elif overwrite:
            # 用户选了「用导入的覆盖」。用户标签不受影响：upsert 的 DO UPDATE
            # 不含 user_tags（那是用户在界面上勾的，不该被外部源改写）
            _normalize_group(item, allowed_user_tags)
            to_overwrite.append(item)
            updated_count += 1
            live_fp[url] = fingerprint(item)
        else:
            conflict_count += 1
            conflict_urls.append(url)
            conflict_sources.append(item)

    # 落库合并成两次（每次一个事务）。要覆盖的那批走 upsert；新建的那批里
    # 不可能有还留在回收站里的同 URL——有的话它也不在 live_fp 里，会落到这里，
    # 而部分唯一索引只约束在用的行，所以照样能插进去（旧版仍留在回收站）
    if to_overwrite:
        st.upsert_sources(to_overwrite)
    if to_create:
        st.upsert_sources(to_create)

    return {
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "updated_count": updated_count,
        "conflict_count": conflict_count,
        "invalid_count": invalid_count,
        "total": len(data),
        "conflict_urls": conflict_urls,
        "conflict_file": _write_conflicts(conflict_sources),
    }

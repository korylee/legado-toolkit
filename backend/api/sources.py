# -*- coding: utf-8 -*-
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_store
from backend.schemas import SourcePage, SourceSave, TagDelete, TagMerge, TagPatch, TagRename

router = APIRouter()


@router.get("", response_model=SourcePage)
def list_sources(
    type: Optional[int] = Query(None, description="0小说 1听书 2漫画 3视频"),
    health: str = Query("", description="ok/dead/auth/gfw"),
    group: str = "",
    tag: str = "",
    q: str = "",
    only_enabled: bool = False,
    include_deleted: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("id", description="id/name/stars/checked_at，前缀 - 表示倒序"),
    st=Depends(get_store),
):
    # 服务端筛选 + 排序 + 分页：不要把 3700 行全量传给浏览器
    total = st.count_query(source_type=type, group=group, health=health, q=q,
                           only_enabled=only_enabled, include_deleted=include_deleted,
                           user_tag=tag)
    items = st.query(source_type=type, group=group, health=health, q=q,
                     only_enabled=only_enabled, limit=limit, offset=offset, order=order,
                     include_deleted=include_deleted, user_tag=tag)
    return {"total": total, "items": items}


@router.get("/groups")
def list_groups(st=Depends(get_store)):
    return [{"group": g, "count": n} for g, n in st.groups()]


@router.get("/tags")
def list_tags(st=Depends(get_store)):
    return st.tags_overview()


@router.get("/tags/meta")
def tags_meta():
    """系统标签枚举的定义（唯一定义源是 core/tags.py 与 core/models.py）。

    前端不再自行维护这些枚举——两边各存一份必然漂移，历史上已经对不上过
    （书源类型一度把 3 当成视频、还编出了 Legado 不存在的 4）。
    """
    from core.models import BOOK_SOURCE_TYPE_NAMES
    from core.tags import (
        SYSTEM_QUALITY_TAG_ORDER,
        SYSTEM_STATUS_TAG_ORDER,
        USER_TAG_ALIASES,
    )

    return {
        "source_types": [{"value": v, "tag": t}
                         for v, t in sorted(BOOK_SOURCE_TYPE_NAMES.items())],
        "status_tags": list(SYSTEM_STATUS_TAG_ORDER),
        "quality_tags": list(SYSTEM_QUALITY_TAG_ORDER),
        # 用户标签别名表。不下发的话，用户手输「精品排版」时界面显示原文，
        # 保存后被后端归一成「精排」，下次打开标签就"变了"——静默不一致
        "user_tag_aliases": dict(USER_TAG_ALIASES),
    }


@router.get("/stats")
def stats(st=Depends(get_store)):
    return st.stats()


@router.get("/detail")
def get_detail(url: str, st=Depends(get_store)):
    src = st.get_source(url)
    if not src:
        raise HTTPException(404, "源不存在: %s" % url)
    return {"source": src, "last_check": st.last_check(url),
            "system_tags_locked": st.is_system_tags_locked(url)}


@router.get("/exists")
def source_exists(url: str, st=Depends(get_store)):
    """查域名是否已存在。新建书源保存前调用，用于阻止静默覆盖。

    比复用 /detail 再解析 404 错误串更稳（api/client.js 抛的是字符串错误）。
    """
    # get_source 内部会做 _normalize_url（去空白、去尾部斜杠、转小写），
    # 所以这里直接传原始 url 即可，https://a.com 与 https://A.com/ 会命中同一源。
    src = st.get_source(url)
    if not src:
        return {"exists": False, "name": ""}
    return {"exists": True, "name": str(src.get("bookSourceName", "") or "")}


@router.post("/save")
def save_source(body: SourceSave, st=Depends(get_store)):
    from core.sanitize import clean_source

    src = dict(body.source or {})
    url = str(src.get("bookSourceUrl", "") or "").strip()
    if not url:
        raise HTTPException(400, "bookSourceUrl 不能为空")
    clean_source(src)
    was_locked = st.is_system_tags_locked(url)
    st.upsert_sources([src])
    if body.lock_system_tags:
        st.set_system_tags_override([url], src.get("bookSourceGroup", "") or "")
    elif was_locked:
        st.clear_system_tags_override([url])
    st.set_user_tags([url], body.user_tags or [])
    return {"ok": True, "url": url}


@router.post("/tags")
def patch_tags(body: TagPatch, st=Depends(get_store)):
    # 加标签 / 去标签，绝不覆盖其他标签。
    added = st.add_user_tags(body.urls, body.add)
    removed = st.remove_user_tags(body.urls, body.remove)
    return {"added": added, "removed": removed}


@router.post("/tags/rename")
def rename_tag(body: TagRename, st=Depends(get_store)):
    n = st.rename_user_tag(body.old, body.new)
    if not n:
        raise HTTPException(400, "标签不存在、为空或不是用户标签")
    return {"updated": n}


@router.post("/tags/merge")
def merge_tags(body: TagMerge, st=Depends(get_store)):
    n = st.merge_user_tags(body.sources, body.target)
    if not n:
        raise HTTPException(400, "没有可合并的用户标签")
    return {"updated": n}


@router.post("/tags/delete")
def delete_tag(body: TagDelete, st=Depends(get_store)):
    n = st.delete_user_tag(body.tag)
    if not n:
        raise HTTPException(400, "标签不存在或不是用户标签")
    return {"updated": n}


@router.post("/tags/normalize")
def normalize_tags(st=Depends(get_store)):
    return {"updated": st.normalize_user_tags()}


@router.delete("")
def soft_delete_sources(urls: str, reason: str = "", st=Depends(get_store)):
    # 软删除：UI 永不硬删。软删的源不再进导出，可随时恢复；
    # 整条 raw_json 会快照到 data/backups/deleted_<时间戳>.json，
    # 彻底删除由使用者在该文件层面处理。
    keys = [u.strip() for u in urls.split(",") if u.strip()]
    n, snapshot = st.soft_delete(keys, reason)
    return {
        "deleted": n,
        "snapshot": snapshot,
        "hint": "已软删除（不会再导出到 App）。彻底删除请处理上面这个快照文件。",
    }


@router.post("/restore")
def restore_sources(body: dict, st=Depends(get_store)):
    return {"restored": st.restore(body.get("urls") or [])}


@router.get("/deleted")
def list_deleted(limit: int = Query(200, ge=1, le=1000),
                 offset: int = Query(0, ge=0), st=Depends(get_store)):
    return {"total": st.count_deleted(), "items": st.list_deleted(limit, offset)}

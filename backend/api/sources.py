# -*- coding: utf-8 -*-
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_store
from backend.schemas import GroupPatch, SourcePage

router = APIRouter()


@router.get("", response_model=SourcePage)
def list_sources(
    type: Optional[int] = Query(None, description="0小说 1听书 2漫画 3视频"),
    health: str = Query("", description="ok/dead/auth/gfw"),
    group: str = "",
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
                           only_enabled=only_enabled, include_deleted=include_deleted)
    items = st.query(source_type=type, group=group, health=health, q=q,
                     only_enabled=only_enabled, limit=limit, offset=offset, order=order,
                     include_deleted=include_deleted)
    return {"total": total, "items": items}


@router.get("/groups")
def list_groups(st=Depends(get_store)):
    return [{"group": g, "count": n} for g, n in st.groups()]


@router.get("/stats")
def stats(st=Depends(get_store)):
    return st.stats()


@router.get("/detail")
def get_detail(url: str, st=Depends(get_store)):
    src = st.get_source(url)
    if not src:
        raise HTTPException(404, "源不存在: %s" % url)
    return {"source": src, "last_check": st.last_check(url)}


@router.patch("/group")
def patch_group(body: GroupPatch, st=Depends(get_store)):
    # 同时更新列与 raw_json，保证 export 不失真
    if not st.set_group(body.url, body.group):
        raise HTTPException(404, "源不存在: %s" % body.url)
    return {"ok": True}


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

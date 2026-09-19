# -*- coding: utf-8 -*-
import collections
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_store
from backend.schemas import (MergeIn, MergeUndoIn, NameApplyIn, NamePreviewIn, NameUndoIn,
                             SourceDeleteIn, SourcePage, SourceSave, TagDelete, TagMerge,
                             TagPatch, TagRename)

router = APIRouter()


@router.get("", response_model=SourcePage)
def list_sources(
    type: Optional[int] = Query(None, description="0小说 1听书 2漫画 3下载"),
    health: str = Query("", description="ok/dead/auth/gfw/cert/pending/none"),
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


@router.get("/urls")
def list_source_urls(
    # 这里**不用 Query(...) 包默认值**：那样默认值是个 Query 对象，直接调端点函数
    # （本仓库的测试惯例，见 test_settings_api）时会被原样传进 SQL，报
    # 「int() argument must be ... not 'Query'」。校验需求（ge/le）也没有
    type: Optional[int] = None,     # 0小说 1听书 2漫画 3下载；None = 不筛
    health: str = "",               # ok/dead/auth/gfw/cert/pending/none；空 = 不筛
    group: str = "",
    tag: str = "",
    q: str = "",
    only_enabled: bool = False,
    st=Depends(get_store),
):
    """当前筛选下的**全部** URL，供「选中全部 N 条筛选结果」。

    参数与 ``list_sources`` 一致（因此共用同一套 ``Store._where`` 口径），但
    **不分页**——调用方的语义就是要全部，上限由库本身兜底（本地单用户，量级即全库）。

    **不接 include_deleted**：全选只能落在未删除范围内。留着那个口子，将来某个
    调用点顺手透传，回收站里的源就会被一起选进来——而这一步的下一步是删除。
    """
    urls = st.query_urls(source_type=type, group=group, health=health, q=q,
                         only_enabled=only_enabled, user_tag=tag)
    # total 与 urls 同源：前端拿它写「选中全部 N 条」。分头算的话两边可能对不上，
    # 而那个数字正是用户按下删除确认的依据
    return {"urls": urls, "total": len(urls)}


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
    from core.constants import TYPE_MAP
    from core.models import BOOK_SOURCE_TYPE_NAMES
    from core.tags import (
        SYSTEM_QUALITY_TAG_ORDER,
        SYSTEM_STATUS_TAG_ORDER,
        USER_TAG_ALIASES,
    )

    # 数值 → 键名（`TYPE_MAP` 是键名 → 数值，这是它的反向）。键名是
    # `services/add_source.py` 的入参，前端提交「快速生成」任务时要给——它原来
    # 自己抄了一份 `TYPE_KEYS`，与 `core/constants.py` 那份是同一事实的两处定义，
    # 而书源类型这个枚举前端已经抄错过一次（把 3 当成视频、编出过 Legado 不存在的 4）
    key_of_type = {v: k for k, v in TYPE_MAP.items()}

    return {
        "source_types": [{"value": v, "tag": t, "key": key_of_type.get(v, "")}
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


@router.post("/delete")
def soft_delete_sources(body: SourceDeleteIn, st=Depends(get_store)):
    # 软删除：UI 永不硬删。软删的源不再进导出，可随时恢复；
    # 整条 raw_json 会追加到 data/backups/deleted.jsonl（一行一次删除操作），
    # 彻底删除由使用者在该文件层面处理。
    #
    # urls 从查询串换成 body：实测约 1600 条 / 57KB 通过、2000 条 / 72KB 被 400 拒绝，
    # 而全库 3850 条约 139KB——「全选全部」正好会撞上。批量数据本来也不该塞进 URL。
    # 旧的 DELETE /sources 直接替换掉、不留兼容层——唯一调用方是前端，同一次改动里一起改
    keys = [u.strip() for u in (body.urls or []) if u and u.strip()]
    n, snapshot = st.soft_delete(keys, body.reason)
    return {
        "deleted": n,
        "snapshot": snapshot,
        "hint": "已软删除（不会再导出到 App）。彻底删除请处理 deleted.jsonl 里对应的那条记录。",
    }


@router.post("/restore")
def restore_sources(body: dict, st=Depends(get_store)):
    """按**行 id** 恢复回收站里的行。

    粒度是行 id 而不是 URL：同一个 URL 在回收站里可以有多份历史版本（见
    `Store.migrate_sources_url_scope_once`），按 URL 恢复会含糊——恢复哪一份？

    返回体带 `blocked`：那些行对应的 URL 已经有**在用**的版本，得先删掉在用的
    那条才能恢复。不静默顶替。
    """
    return st.restore(body.get("ids") or [])


@router.post("/purge")
def purge_deleted(st=Depends(get_store)):
    """清空回收站——把里面的行**彻底删掉**（全仓唯一的硬删除路径）。

    删之前由 `Store.purge_deleted` 先落一份快照（含 group_name / user_tags 等，
    只存 raw_json 会丢这两样）。快照路径随返回体给出，界面上要显示出来——
    **这是不可逆操作，用户得知道回滚点在哪**。
    """
    return st.purge_deleted()


@router.get("/deleted")
def list_deleted(limit: int = Query(200, ge=1, le=1000),
                 offset: int = Query(0, ge=0), st=Depends(get_store)):
    return {"total": st.count_deleted(), "items": st.list_deleted(limit, offset)}


# ---------------------------------------------------------------- 名称清洗
# 三步走：preview（只读建议）→ apply（改名，返回旧名）→ undo（回写旧名）。
# 写路径**不共用 `save_source`**：那条会 `set_user_tags` 整组覆盖，漏传就把标签
# 清空（lessons §三十五 记过）。改名会同时更新 raw_json 与 fingerprint 列，
# 见 `Store.set_source_name`。


@router.post("/names/preview")
def preview_names(body: NamePreviewIn, st=Depends(get_store)):
    """名称清洗预演：**只读**，返回「会变成什么」的清单。

    一次算全库而不是逐条问后端：`clean_source_name` 要拿「库里已有哪些名字」判
    「去掉后缀后会不会与已有名字撞车」，逐条问的话每条的判据都不一样。
    """
    from core.name_clean import REASON_LABELS, clean_sources

    rows = st.name_pairs(body.urls)
    items = clean_sources([{"bookSourceUrl": r["url"], "bookSourceName": r["name"]}
                           for r in rows])
    changed = [i for i in items if i["changed"]]
    # 「撞车」提示：改完之后这个新名字是否与**别的源**重名（用清洗后的名字算）。
    # 那正是下一步「同名档」要摊开的东西，提前标出来，免得到时候一片同名还看不出因果。
    final = collections.Counter(str(i["new_name"]).strip() for i in items)
    for i in changed:
        i["collides"] = final[str(i["new_name"]).strip()] > 1
    return {"items": changed, "total": len(rows), "changed": len(changed),
            "reason_labels": REASON_LABELS}


@router.post("/names/apply")
def apply_names(body: NameApplyIn, st=Depends(get_store)):
    """应用改名，返回 `prev`（旧名 + 地址）供撤销原样回写。

    名称为空的一律跳过：那会让列表上出现一条没有名字的源，而这是不可逆的观感损失。
    """
    prev, applied, missing = [], 0, 0
    for ch in body.changes:
        name = str(ch.name or "").strip()
        if not name:
            continue
        old = st.set_source_name(ch.url, name)
        if old is None:
            missing += 1
            continue
        prev.append({"url": ch.url, "name": old})
        applied += 1
    return {"applied": applied, "missing": missing, "prev": prev}


@router.post("/names/undo")
def undo_names(body: NameUndoIn, st=Depends(get_store)):
    """撤销改名：拿 `apply` 返回的 `prev` 回写旧名。只还原名字，不碰标签/备注。"""
    restored, missing = 0, 0
    for ch in body.prev:
        old = st.set_source_name(ch.url, str(ch.name or ""))
        if old is None:
            missing += 1
        else:
            restored += 1
    return {"restored": restored, "missing": missing}


# ---------------------------------------------------------------- 合并重复源
# 判据（同站点 + 行为指纹相同）在 services/merge_sources 里**重算**，不信前端传来的
# 分组：前端只是入口，而这一步是删源。跨站点 / 规则不同 → 400 并说明原因。


@router.post("/merge")
def merge_sources(body: MergeIn, st=Depends(get_store)):
    """合并一组重复源。`dry_run=true` 时只返回「将发生的三件事」，不写库。"""
    from services.merge_sources import MergeRejected, merge

    try:
        return merge(st, body.keep, body.drop, merge_tags=body.merge_tags,
                     merge_comment=body.merge_comment, dry_run=body.dry_run)
    except MergeRejected as e:
        raise HTTPException(400, str(e))


@router.post("/merge/undo")
def merge_undo(body: MergeUndoIn, st=Depends(get_store)):
    """撤销一次合并。四个字段全部来自 merge 的返回体，别让调用方自己推算。"""
    from services.merge_sources import undo

    return undo(st, body.keep, body.restore_urls, body.tags_added, body.prev_comment)


@router.get("/dups")
def list_dups(
    kinds: str = Query("mergeable", description="rules/mergeable/mirror/host/name，逗号分隔"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    st=Depends(get_store),
):
    """重复源分组（**只读**）。

    只有 `mergeable`（同站点 + 行为指纹相同）带动作，其余四档是给人判断的线索。
    分组与判据全部来自 `core.dups`——CLI 的 `dups` 命令用同一份，**不在前端重算**。

    每次请求都重算全库（实测 mergeable 档约 0.1s，加读库共半秒上下）：这类清单是
    「翻一遍做决定」用的，不值得为它加缓存层，而缓存一旦与实际库不同步，用户会照着
    过期的分组删源。
    """
    from core.dups import DUP_KINDS, find_groups, summarize_by_kind

    want = tuple(k.strip() for k in str(kinds or "").split(",") if k.strip())
    if not want:
        want = ("mergeable",)
    unknown = [k for k in want if k not in DUP_KINDS]
    if unknown:
        raise HTTPException(400, "未知的分组类型：%s（可选 %s）"
                            % ("、".join(unknown), "、".join(DUP_KINDS)))
    groups = find_groups(st.export_sources(), checks=st.checks_map(), kinds=want)
    return {"summary": summarize_by_kind(groups), "total": len(groups),
            "offset": offset, "groups": groups[offset:offset + limit]}

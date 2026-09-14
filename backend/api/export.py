# -*- coding: utf-8 -*-
# 导出为「临时文件」：每次导出生成一个 uid，链接形如 /api/export/<uid>.json
#
# 为什么不用固定链接：多选生成不同源文件是核心需求。
# 固定 latest.json 会被下一次导出覆盖，之前发出去的二维码/链接内容就变了。
#
# 过期与清理：
#   ttl_days 默认 7 天；pinned=1 永不过期（适合长期存在手机里的链接）
#   每次创建导出时顺手 sweep 一次；也可手动 POST /api/export/cleanup
#
# hits 计数：手机每拉取一次 +1。hits=0 说明手机根本没连上（IP 错/防火墙），
# 而不是 Legado 解析失败 —— 这是排查扫码导入的唯一有效信号。
import json
import pathlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.deps import get_store
from core.paths import data_path
from core.store import now

router = APIRouter()
EXPORT_DIR = pathlib.Path(data_path("out", "exports"))


def _file_of(uid: str) -> pathlib.Path:
    return EXPORT_DIR / (uid + ".json")


@router.post("", status_code=201)
def create_export(body: dict, st=Depends(get_store)):
    urls = [str(u) for u in (body.get("urls") or []) if u]
    name = str(body.get("name") or "").strip()
    ttl = int(body.get("ttl_days") or 7)
    pinned = bool(body.get("pinned"))
    filt = body.get("filter") or {}

    if urls:
        # 模式一：导出勾选的源
        srcs = [s for s in (st.get_source(u) for u in urls) if s]
    elif filt:
        # 模式二：导出当前筛选结果（复用列表页的筛选条件，不受分页限制）
        srcs = st.export_by_filter(
            source_type=(int(filt["type"]) if filt.get("type") not in (None, "") else None),
            group=str(filt.get("group") or ""),
            health=str(filt.get("health") or ""),
            q=str(filt.get("q") or ""),
            only_enabled=bool(filt.get("only_enabled")),
        )
    else:
        # 模式三：全量
        srcs = st.export_sources()
    if not srcs:
        raise HTTPException(400, "没有可导出的源")

    uid = uuid.uuid4().hex[:12]
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = _file_of(uid)
    path.write_text(json.dumps(srcs, ensure_ascii=False, indent=2), encoding="utf-8")

    info = st.create_export(uid, name, [s.get("bookSourceUrl", "") for s in srcs],
                            uid + ".json", ttl)
    if pinned:
        st.set_export_pinned(uid, True)

    # 顺手清理过期文件
    for old in st.sweep_exports():
        try:
            _file_of(old).unlink()
        except Exception:
            pass

    return {
        "uid": uid,
        "name": name,
        "count": len(srcs),
        "bytes": path.stat().st_size,
        "expires_at": info["expires_at"],
        "pinned": pinned,
        "url": "/api/export/%s.json" % uid,
    }


@router.get("")
def list_exports(st=Depends(get_store)):
    return st.list_exports()


@router.post("/cleanup")
def cleanup(st=Depends(get_store)):
    uids = st.sweep_exports()
    for u in uids:
        try:
            _file_of(u).unlink()
        except Exception:
            pass
    return {"removed": len(uids), "uids": uids}


@router.get("/{uid}.json")
def fetch_export(uid: str, st=Depends(get_store)):
    # Legado 的 importonline 会直接 GET 这个地址，必须返回纯 JSON 数组
    d = st.get_export(uid, bump=True)
    if not d:
        raise HTTPException(404, "导出不存在或已清理")
    if d.get("expired"):
        raise HTTPException(410, "导出已过期（ttl 到期），请重新生成")
    p = _file_of(uid)
    if not p.exists():
        raise HTTPException(404, "导出文件已被清理")
    return Response(content=p.read_bytes(), media_type="application/json")


@router.get("/{uid}")
def export_meta(uid: str, st=Depends(get_store)):
    d = st.get_export(uid)
    if not d:
        raise HTTPException(404, "导出不存在")
    return d


@router.post("/{uid}/pin")
def pin_export(uid: str, pinned: bool = True, st=Depends(get_store)):
    if not st.set_export_pinned(uid, pinned):
        raise HTTPException(404, "导出不存在")
    return {"uid": uid, "pinned": pinned}


@router.delete("/{uid}")
def delete_export(uid: str, st=Depends(get_store)):
    ok = st.delete_export(uid)
    try:
        _file_of(uid).unlink()
    except Exception:
        pass
    return {"deleted": ok}

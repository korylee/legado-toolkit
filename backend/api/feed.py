# -*- coding: utf-8 -*-
"""固定订阅接口。

和 /api/export 的临时快照不同：
- 这里的 URL 固定不变，不会过期；
- 每次 GET 都从 SQLite 当前候选库动态生成 JSON；
- 默认只输出未删除、enabled=1 的源，ok.json 额外要求 health=ok。
"""
import hashlib
import json

from fastapi import APIRouter, Depends, Response

from backend.deps import get_store

router = APIRouter()


def _json_response(srcs):
    # 紧凑 JSON，减少手机拉取体积；ensure_ascii=False 保留中文。
    body = json.dumps(srcs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = '"%s"' % hashlib.sha256(body).hexdigest()
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "ETag": etag,
            "X-Source-Count": str(len(srcs)),
        },
    )


@router.get("/ok.json")
def feed_ok(st=Depends(get_store)):
    """可用源固定订阅（health=ok 且 enabled=1）。"""
    return _json_response(st.export_by_filter(health="ok", only_enabled=True))


@router.get("/all.json")
def feed_all(st=Depends(get_store)):
    """完整固定订阅（未删除且 enabled=1，含未校验/待验证）。"""
    return _json_response(st.export_by_filter(only_enabled=True))

# -*- coding: utf-8 -*-
# 耗时操作的任务入口：把 CLI 的能力暴露成 job。
from typing import Any, Dict

from core.store import Store

from backend.jobs import runner


@runner.register("check")
async def run_check_job(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 校验一批源。payload:
    #   urls: [书源URL]  为空则全量
    #   limit: 限制条数
    #   concurrency / probe_depth 等透传给 run_check
    from core.checker import run_check
    from core.models import build_record

    urls = payload.get("urls") or []
    limit = int(payload.get("limit", 0) or 0)
    if urls:
        srcs = [s for s in (st.get_source(u) for u in urls) if s]
    else:
        srcs = st.export_sources()
    if limit:
        srcs = srcs[:limit]
    st.update_job(job_id, total=len(srcs))

    records = [build_record(s, i) for i, s in enumerate(srcs)]
    # 进度回调：run_check 内部按批推进，这里用近似值
    results = run_check(records,
                        concurrency=int(payload.get("concurrency", 20) or 20),
                        probe_depth=int(payload.get("probe_depth", 1) or 1),
                        use_store=True)
    st.update_job(job_id, progress=len(results))
    items = [{
        "url": r.url, "name": r.name, "health": r.health,
        "stars": r.quality_stars, "error": r.error,
        "toc_complete": r.toc_complete, "content_ok": r.content_ok,
    } for r in results]
    return {"checked": len(items), "items": items[:500]}

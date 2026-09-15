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
    #   concurrency / probe_depth / timeout 等透传给 AsyncChecker
    from core.checker import AsyncChecker
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
    checker = AsyncChecker(
        concurrency=int(payload.get("concurrency", 20) or 20),
        timeout=float(payload.get("timeout", 8.0) or 8.0),
        probe_search=bool(payload.get("probe_search", True)),
        keyword=str(payload.get("keyword") or "我"),
        proxy=payload.get("proxy") or None,
        probe_depth=int(payload.get("probe_depth", 1) or 1),
        use_store=True,
    )
    checker.refresh_cache = bool(payload.get("refresh_cache"))
    try:
        results = await checker.run(records)
    finally:
        checker.close()

    st.update_job(job_id, progress=len(results))
    st.rebuild_system_tags()
    items = [{
        "url": r.url, "name": r.name, "health": r.health,
        "stars": r.quality_stars, "error": r.error,
        "toc_complete": r.toc_complete, "content_ok": r.content_ok,
    } for r in results]
    return {
        "checked": len(items),
        # 缓存命中多少、真发了多少：不分出来的话，「点校验 → 一条请求都没发」
        # 和「真跑了一遍」在界面上长得一模一样
        "cached": checker.cached_count,
        "fetched": len(items) - checker.cached_count,
        # 写库失败多少：>0 说明状态不会变，必须让用户看见（不是我们抛错，是写不进去）
        "save_failures": checker.save_failures,
        "hit_downgrades": len(checker.hit_downgrades),
        "items": items[:500],
    }


@runner.register("add")
async def run_add_job(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 快速添加源：URL -> 自动推断 -> 生成候选源 -> 验证链。
    # 注意：这里只生成候选，不直接写候选主库；前端预览确认后再调 /api/sources/save。
    import asyncio
    import os

    from core.build import load_sources
    from core.fetch import extract_keyword
    from core.paths import data_path
    from core.verify import strip_evidence, verify_chain
    from services.add_source import run_add

    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("url 不能为空")
    name = str(payload.get("name") or "")
    source_type = str(payload.get("type") or "novel")
    group = str(payload.get("group") or "")
    detail_url = str(payload.get("detail_url") or "")
    probe = bool(payload.get("probe", True))
    discover = bool(payload.get("discover", False))
    verify = bool(payload.get("verify", True))
    pick = int(payload.get("pick", 1) or 1)

    out_dir = data_path("out", "quick_add")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, job_id + ".json")
    st.update_job(job_id, total=4, progress=1)

    def _generate():
        return run_add(
            url,
            name=name,
            source_type=source_type,
            group=group,
            output=out_path,
            no_ask=True,
            probe=probe,
            detail_url=detail_url,
            verify=False,
            pick=pick,
            interactive=False,
            to_merge="",
            discover=discover,
        )

    try:
        rc = await asyncio.to_thread(_generate)
        st.update_job(job_id, progress=2)
        if rc not in (0, 2):
            return {"ok": False, "return_code": rc, "error": "生成失败"}
        sources = load_sources(out_path)
        if not sources:
            return {"ok": False, "return_code": rc, "error": "未生成书源（可能已存在）"}
        source = sources[0]
        from core.tags import merge_group, normalize_tags
        tags = [tag for tag in normalize_tags(source.get("bookSourceGroup", ""))
                if tag != "📖新增源"]
        source["bookSourceGroup"] = merge_group([], tags)
        keyword = extract_keyword(url) or str(payload.get("keyword") or "我")
        if verify:
            v = await asyncio.to_thread(verify_chain, source, keyword, detail_url, pick)
            # 快速生成的结果会写进 jobs 表（jobs/runner.py:51）并走 SSE 推送
            # （api/jobs.py:47），整页 HTML + 正文全文会撑爆 jobs 表与推送流；
            # 剥掉证据原文，判定结论（ok/detail/all_ok）完整保留，前端预览不受影响
            v = strip_evidence(v)
        else:
            v = {"steps": [], "all_ok": None, "skipped": True}
        st.update_job(job_id, progress=3)
        return {"ok": True, "source": source, "verify": v, "keyword": keyword,
                "return_code": rc}
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

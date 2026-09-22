# -*- coding: utf-8 -*-
# 耗时操作的任务入口：把 CLI 的能力暴露成 job。
from typing import Any, Dict, List

from core.loader import _normalize_url
from core.store import Store

from backend.jobs import runner


@runner.register("add")
async def run_add_job(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 快速添加源：URL -> 自动推断 -> 生成候选源 -> 验证链。
    # 注意：这里只生成候选，不直接写候选主库；前端预览确认后再调 /api/sources/save。
    import asyncio
    import os

    from core.build import load_sources
    from core.fetch import extract_keyword
    from core.paths import data_path
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
        out = await asyncio.to_thread(_generate)
        st.update_job(job_id, progress=2)
        rc = out.rc
        if rc not in (0, 2):
            # **原因原样带出去**（`AddResult.error`）：压成「生成失败」等于把
            # 「工具认不出这个页面」说成「源坏了」（lessons §七十七、AGENTS #4）
            return {"ok": False, "return_code": rc, "error": out.error}
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
            # **跑一次本机引擎**（十-5）。原来这里跑的是 `verify_chain`（本地回放器）：
            # 它跑不了 JS、没有登录态，对 L2–L4 的页面看的是**另一份材料**——给出的
            # 「通过」与 App 的实际行为无关（正是 lessons §七十七 那一类）。引擎是同一段
            # App 代码 + 同一套环境，结论与「手动调一次」逐段一致。
            #
            # 结果体与设备/引擎调试**同形状**（steps / pages / all_ok），所以前端那套
            # 三态渲染与「有疑点」引导零改动就能吃。
            from core.jvm_debug import verify_generated
            v = await asyncio.to_thread(verify_generated, source, keyword, detail_url)
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

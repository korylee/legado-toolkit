# -*- coding: utf-8 -*-
# 耗时操作的任务入口：把 CLI 的能力暴露成 job。
from typing import Any, Dict, List

from core import settings_store
from core.loader import _normalize_url
from core.store import Store

from backend.jobs import runner

# 任务结果的形状（items / transitions）由 `check_summary` **一处**给出：
# 本机引擎那条跑批（`jvm.run_jvm_job`）用的是同一份——两条路的结果体必须同形，
# 前端读的是同一段代码（`parseCheckResult` / `applyCheckResults`）。
from backend.api.check_summary import (ITEMS_LIMIT, check_items_from_records,
                                       summarize_transitions)


def _verify_generated(source: Dict[str, Any], keyword: str,
                      detail_url: str) -> Dict[str, Any]:
    """生成完的验证：**跑一次本机引擎**（十-5）。

    两条边界：

    - **引擎不可用不推翻生成结果**：没配 App 源码目录 / 另一个 JVM 任务在跑 / 零事件时，
      源照样生成，只是把「这次没验成 + 为什么」原样带回去（AGENTS #4：原因要走到用户眼前）。
      生成的验证是**附加证据**，不是生成的必要条件。
    - **证据原文要剥掉**：结果会写进 jobs 表并走 SSE（`jobs/runner.py` / `api/jobs.py`），
      整页 HTML + 逐段原文会撑爆它——判定结论（verdict/detail/all_ok）完整保留，
      前端预览不受影响（同一件事在旧路上也做过）。
    """
    from core.jvm_debug import run_jvm_debug
    from core.paths import data_path
    from core.verify import strip_evidence

    # 调试目标：有搜索规则就用关键词（搜索 → 详情 → 目录 → 正文整条链）；没有搜索规则的
    # 源（「仅发现」模式生成的）拿详情页当入口。**App 固定取第一条候选**，没有 pick 这个口
    key = keyword if str(source.get("searchUrl") or "").strip()         else (detail_url or str(source.get("bookSourceUrl") or ""))
    out = run_jvm_debug(source, key=key, timeout=60,
                        out_path=data_path("app_probe", "quick_add_verify.ndjson"))
    if out.get("error") and not out.get("steps"):
        return {"steps": [], "pages": [], "all_ok": None, "skipped": True,
                "engine": "jvm", "error": out["error"]}
    # 事件流对「生成预览」没用，且它是这里最占体积的一块（判定要看的是 steps）
    out.pop("events", None)
    return strip_evidence(out)


@runner.register("check")
async def run_check_job(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 校验一批源。payload:
    #   urls: [书源URL]  为空则全量
    #   limit: 限制条数
    #   refresh_cache: 忽略有效期内的缓存。它是**每次动作**而非默认值，所以不进
    #                  设置，由前端直接传
    #   check: {concurrency/timeout/probe_depth/verify_ssl/proxy}
    #          本次临时覆盖，只作用于这一个 job，**不写回全局设置**
    #          （单独一层是为了不和 urls/limit/refresh_cache/total 挤在一个命名空间）
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
    # 上一版结论的快照，**必须在 run() 之前读**：跑完之后新结论就落库了，
    # 那时再读，每条源都是 old == new，摘要会永远报「无状态变化」——看起来一切
    # 正常，却把这次改动要回答的问题答错了。checks_map 的键是规范化的，
    # 比对时两侧都要归一（见 summarize_transitions）
    prev_checks = st.checks_map()
    # 取值顺序：本次覆盖 > 全局设置 > 内置默认。三级都在 resolve_check 里完成，
    # 这里**不要**再出现 payload.get("concurrency", 20) 这类写法——默认值散落在
    # 调用点是漂移的源头（ops.py 曾写 20、CLI 写 50、AsyncChecker 写 50）
    cfg = settings_store.resolve_check(payload.get("check"))
    checker = AsyncChecker(
        concurrency=cfg["concurrency"],
        timeout=cfg["timeout"],
        # 以前根本没读 payload 的 verify_ssl，前端给了也不生效
        verify_ssl=cfg["verify_ssl"],
        probe_depth=cfg["probe_depth"],
        # keyword 不在全局设置里（那是「测哪个书名」，不是随环境变的参数），保持原样
        keyword=str(payload.get("keyword") or "我"),
        # 设置里空串 = 直连；AsyncChecker 认的是 None，"" 会被原样递给 aiohttp。
        # 转换只在这一处，别在存储层也存成 None（那样「空串=直连」就没法显式表达了）
        proxy=cfg["proxy"] or None,
        cache_ttl_ok=cfg["cache_ttl_ok"],
        cache_ttl_other=cfg["cache_ttl_other"],
        cache_ttl_auth=cfg["cache_ttl_auth"],
        use_store=True,
    )
    checker.refresh_cache = bool(payload.get("refresh_cache"))
    try:
        results = await checker.run(
            records,
            # **长任务必须报进度**：不报的话 `jobs.progress` 全程是 0，而全量 3800 条
            # 要跑十几分钟——用户看到的就是「点了没反应」，只能靠猜还在不在跑。
            # run() 每完成一条报一次（含缓存命中那部分的起步值，见那边的注释）
            on_progress=lambda done, _total: st.update_job(job_id, progress=done),
        )
    finally:
        checker.close()

    st.update_job(job_id, progress=len(results))
    # 只重建**这次校验过**的那些源的分组标签。分组只由该源自身的
    # (类型, 健康度, 星级) 决定，没被重算的源不可能变——而全库重建实测 0.55 秒，
    # 只校验一条源时那 0.55 秒全是白花的
    st.rebuild_system_tags([r.url for r in results])
    items = check_items_from_records(results)
    return {
        "checked": len(items),
        # 相对上一次的变化。与上面「命中多少」同一动机：把看不见的事实报出来。
        # 「首次有结论」与「变成 X」分开——绝大多数源从未校验过，混在一起
        # 「新增可用 2000 条」就会被读成「比上次好」
        "transitions": summarize_transitions(prev_checks, items),
        # 本次实际用的参数。和下面三项同一动机：把看不见的事实报出来——
        # 否则「为什么这次慢得多」「设置改了到底生效没有」在界面上无从回答
        "params": cfg,
        # 缓存命中多少、真发了多少：不分出来的话，「点校验 → 一条请求都没发」
        # 和「真跑了一遍」在界面上长得一模一样
        "cached": checker.cached_count,
        "fetched": len(items) - checker.cached_count,
        # 写库失败多少：>0 说明状态不会变，必须让用户看见（不是我们抛错，是写不进去）
        "save_failures": checker.save_failures,
        "hit_downgrades": len(checker.hit_downgrades),
        "items": items[:ITEMS_LIMIT],
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
            v = await asyncio.to_thread(_verify_generated, source, keyword, detail_url)
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

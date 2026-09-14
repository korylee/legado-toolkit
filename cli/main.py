# -*- coding: utf-8 -*-
"""
Legado 书源整理工具 CLI。

子命令：
  check    高并发联网校验书源可用性/稳定性
  organize 按内容类型 + 健康状态重建清晰分组
  report   生成 Markdown 诊断报告
  merge    合并/更新多份书源文件（URL 去重）
  dedupe   按 URL/名称去重
  add      快捷新增书源：给搜索 URL 自动推断规则生成书源
  import-sources 安全导入外部书源，生成待校验/待审记录
  review-imports 查看或批准外部书源规则冲突

示例：
  python main.py report -i bookSource.json -o report.md
  python main.py check -i bookSource.json -c 50 -o check_cache/
  python main.py organize -i bookSource.json -r check_cache/ -o organized.json
  python main.py merge -i a.json -i b.json -o merged.json --mode replace
  echo 'https://host/search?q=%E7%BB%8D%E5%AE%8B' | python main.py add - --type manga --no-ask
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import glob
import json
import os
import sys
import time

# 确保本文件所在目录在 sys.path 中（Windows 下直接运行需要）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.loader import load_json_file, dump_json_file, dedupe_sources, merge_sources, fingerprint  # noqa: E402
from core.models import build_record, Health  # noqa: E402


def _load_records(path: str, limit: int = 0):
    """加载书源文件并转为诊断记录。"""
    sources = load_json_file(path)
    if not isinstance(sources, list):
        print(f"[错误] {path} 不是书源列表（应为 JSON 数组）", file=sys.stderr)
        sys.exit(1)
    if limit:
        sources = sources[:limit]
    return [build_record(s, i) for i, s in enumerate(sources)], sources


# 默认输出文件名（各子命令未指定 -o 时使用）
DEFAULT_OUTPUTS = {
    "check": "checked.json",
    "organize": "organized.json",
    "report": "report.md",
    "merge": "merged.json",
    "dedupe": "deduped.json",
}
DEFAULT_CACHE_DIR = "check_cache"


def _resolve_check_cache(args: argparse.Namespace) -> tuple[str, bool]:
    """解析校验缓存策略。

    --no-cache 关闭缓存读写，优先级高于 --refresh-cache；
    --refresh-cache 跳过旧缓存读取，但保留成功结果写入缓存。
    """
    if getattr(args, "no_cache", False):
        return "", False
    cache_dir = getattr(args, "cache_dir", "") or DEFAULT_CACHE_DIR
    return cache_dir, bool(getattr(args, "refresh_cache", False))


def _auto_detect_input() -> str:
    """自动探测书源文件：优先当前目录中的主要书源，其次上级目录 bookSource_* 主文件。

    规则：从已知候选（final_deduped / output_checked_stars / 当前目录最大的书源类 JSON /
    上级目录 bookSource_*.json）中选体积最大的书源列表文件。
    """
    candidates: list[tuple[int, str]] = []
    search_dirs = [os.getcwd(), os.path.dirname(os.getcwd())]
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.json")):
            base = os.path.basename(f)
            # 跳过输出产物与索引/新增源等非书源库文件
            if base in ("auto_added.json", "index.json", "checked.json",
                        "organized.json", "merged.json", "deduped.json"):
                continue
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            candidates.append((size, f))
    if not candidates:
        return ""
    # 按体积降序，取最大者
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def _resolve_input(args, required: bool = True) -> str:
    """解析输入文件：显式指定优先，否则自动探测。"""
    path = getattr(args, "input", "") or ""
    if not path:
        path = _auto_detect_input()
        if path:
            print(f"ℹ️ 未指定输入，自动探测: {path}")
    if required and not path:
        print("[错误] 未找到书源文件，请用 -i 指定", file=sys.stderr)
        sys.exit(1)
    return path


def _safe_input(prompt: str, default: str = "") -> str:
    """读取一行输入（兼容 stdin 管道/向导混用）。"""
    try:
        if prompt:
            print(prompt, end="", flush=True)
        line = sys.stdin.readline().rstrip("\r\n")
        return line if line else default
    except Exception:
        return default


def _ask_confirm(prompt: str = "确认？[y/N] ") -> bool:
    ans = _safe_input(prompt).strip().lower()
    return ans in ("y", "yes", "是")


# ---------------------------------------------------------------- check
def cmd_check(args: argparse.Namespace) -> int:
    from core.checker import run_check
    input_path = _resolve_input(args)
    records, sources = _load_records(input_path, args.limit)
    cache_dir, refresh_cache = _resolve_check_cache(args)
    if getattr(args, "no_cache", False):
        print("缓存策略：不读取、不写入缓存")
    elif refresh_cache:
        print(f"缓存策略：跳过旧缓存，成功结果写入 {cache_dir}")
    print(f"加载书源 {len(records)} 个，开始校验（并发 {args.concurrency}，超时 {args.timeout}s）...")
    testset = None
    if getattr(args, "testset", ""):
        # 自定义测试集 JSON：{"novel": ["书名..."], "manga": ["漫画名..."]}
        try:
            with open(args.testset, "r", encoding="utf-8") as f:
                testset = json.load(f)
            print(f"已加载自定义测试集: {testset}")
        except Exception as e:
            print(f"警告: 测试集加载失败（使用内置）：{e}")
    results = run_check(
        records,
        concurrency=args.concurrency,
        timeout=args.timeout,
        probe_search=not args.no_search_probe,
        keyword=args.keyword,
        verify_ssl=not args.insecure,
        cache_dir=cache_dir,
        testset=testset,
        max_keywords=getattr(args, "max_keywords", 2),
        proxy=getattr(args, "proxy", None),
        probe_depth=getattr(args, "probe_depth", 1),
        refresh_cache=refresh_cache,
    )
    # 统计
    from collections import Counter
    health_counter = Counter(r.health for r in results)
    from core.models import HEALTH_NAMES
    print("\n===== 校验结果 =====")
    for h in sorted(health_counter, key=lambda x: -health_counter[x]):
        print(f"  {HEALTH_NAMES.get(h, h):<8} {health_counter[h]}")
    ok_count = health_counter.get(Health.OK, 0)
    print(f"\n可用 {ok_count}/{len(results)}")

    # 结果保存
    output = getattr(args, "output", "") or DEFAULT_OUTPUTS["check"]
    if output:
        from core.organizer import organize_sources
        data = organize_sources(results, skip_disabled=not args.keep_disabled)
        dump_json_file(output, data)
        print(f"已保存整理结果: {output}")
    return 0


# ---------------------------------------------------------------- organize
def cmd_organize(args: argparse.Namespace) -> int:
    input_path = _resolve_input(args)
    records, sources = _load_records(input_path, args.limit)
    # 若提供 -r 校验缓存，则合并缓存结果
    if getattr(args, "check_dir", ""):
        from core.checker import AsyncChecker, is_cache_item_valid, restore_from_cache
        cache = AsyncChecker(cache_dir=args.check_dir).load_cache()
        for rec in records:
            if rec.url in cache and is_cache_item_valid(rec, cache[rec.url]):
                # 统一走公共恢复逻辑：星级由原始量重算 + 标签清洗（剔除命中《》、保留原创、补齐规则完整）
                restore_from_cache(rec, cache[rec.url])
        n_cached = sum(1 for r in records if r.health != Health.SKIPPED)
        print(f"合并缓存结果: {n_cached}/{len(records)}")
    else:
        # 无缓存时从旧分组迁移明确状态；无法确认的源必须保守标为待验证
        from core.organizer import infer_health_from_group
        for rec in records:
            if rec.health == Health.SKIPPED:
                rec.health = infer_health_from_group(rec.group)

    from core.organizer import organize_sources
    output = getattr(args, "output", "") or DEFAULT_OUTPUTS["organize"]
    drop_dead = bool(getattr(args, "drop_dead", False))
    keep_only_ok = bool(getattr(args, "keep_only_ok", False))
    before = sum(1 for r in records if r.enabled)
    data = organize_sources(records, skip_disabled=not args.keep_disabled,
                            drop_dead=drop_dead, keep_only_ok=keep_only_ok)
    if drop_dead:
        dropped = before - len(data)
        print(f"已剔除失效源: {dropped} 个")
    if keep_only_ok:
        print(f"已精简为仅可用源: {len(data)} 个")
    dump_json_file(output, data)
    print(f"已整理 {len(data)} 个源 → {output}")
    return 0


# ---------------------------------------------------------------- report
def cmd_report(args: argparse.Namespace) -> int:
    from core.reporter import build_report
    input_path = _resolve_input(args)
    records, sources = _load_records(input_path, args.limit)
    # 合并缓存
    if getattr(args, "check_dir", ""):
        from core.checker import AsyncChecker, is_cache_item_valid, restore_from_cache
        cache = AsyncChecker(cache_dir=args.check_dir).load_cache()
        for rec in records:
            if rec.url in cache and is_cache_item_valid(rec, cache[rec.url]):
                # 统一走公共恢复逻辑：星级由原始量重算 + 标签清洗（剔除命中《》、保留原创、补齐规则完整）
                restore_from_cache(rec, cache[rec.url])
        n_cached = sum(1 for r in records if r.health != Health.SKIPPED)
        print(f"合并缓存结果: {n_cached}/{len(records)}")
    report = build_report(records, source_path=input_path)
    output = getattr(args, "output", "") or DEFAULT_OUTPUTS["report"]
    with open(output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存: {output}")
    return 0


# ---------------------------------------------------------------- merge
def cmd_merge(args: argparse.Namespace) -> int:
    all_sources = []
    for p in args.input:
        data = load_json_file(p)
        if not isinstance(data, list):
            print(f"[警告] 跳过非列表文件: {p}", file=sys.stderr)
            continue
        all_sources.append(data)
        print(f"加载 {p}: {len(data)} 个源")
    if not all_sources:
        print("[错误] 没有可用的书源文件", file=sys.stderr)
        return 1
    merged = all_sources[0]
    merge_mode = args.mode
    if getattr(args, "prefer_input", "") == "first":
        merge_mode = "keep"
    elif getattr(args, "prefer_input", "") == "last":
        merge_mode = "replace"
    for incoming in all_sources[1:]:
        merged = merge_sources(merged, incoming, mode=merge_mode)
    if not args.no_dedupe:
        merged = dedupe_sources(merged, prefer_keep="last")
    output = args.output or DEFAULT_OUTPUTS["merge"]
    dump_json_file(output, merged)
    print(f"合并完成: {len(merged)} 个源 → {output}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    """按输入优先级合并并生成完整候选版、快速使用版。"""
    all_sources = []
    for path in args.input:
        data = load_json_file(path)
        if not isinstance(data, list):
            print(f"[警告] 跳过非列表文件: {path}", file=sys.stderr)
            continue
        all_sources.append(data)
        print(f"加载 {path}: {len(data)} 个源")
    if not all_sources:
        print("[错误] 没有可用的书源文件", file=sys.stderr)
        return 1
    merge_mode = "keep" if args.prefer_input == "first" else "replace"
    merged = all_sources[0]
    for incoming in all_sources[1:]:
        merged = merge_sources(merged, incoming, mode=merge_mode)
    merged = dedupe_sources(merged, prefer_keep="first" if args.prefer_input == "first" else "last")
    dump_json_file(args.merged_output, merged)

    from core.organizer import infer_health_from_group, organize_sources
    records = [build_record(source, index) for index, source in enumerate(merged)]
    for record in records:
        if record.health == Health.SKIPPED:
            record.health = infer_health_from_group(record.group)
    full = organize_sources(records, skip_disabled=True)
    fast = organize_sources(records, skip_disabled=True, keep_only_ok=True)
    dump_json_file(args.full_output, full)
    dump_json_file(args.fast_output, fast)
    print(f"合并去重: {len(merged)} 个")
    print(f"完整候选版: {len(full)} 个 → {args.full_output}")
    print(f"快速使用版: {len(fast)} 个 → {args.fast_output}")
    return 0


# ---------------------------------------------------------------- import-sources / review-imports
def cmd_import_sources(args: argparse.Namespace) -> int:
    """导入外部书源，但不直接修改候选库。"""
    from core.registry import import_sources

    try:
        summary = import_sources(
            args.candidate, args.input, args.registry, args.raw_dir,
        )
    except (OSError, ValueError) as error:
        print(f"[错误] 导入失败：{error}", file=sys.stderr)
        return 1

    print(f"导入批次 #{summary.batch_id}")
    print(f"  新增待校验: {summary.new_count}")
    print(f"  重复忽略: {summary.duplicate_count}")
    print(f"  规则冲突待审: {summary.conflict_count}")
    print(f"  当前批次待校验: {summary.pending_count}")
    return 0


def cmd_review_imports(args: argparse.Namespace) -> int:
    """列出待审冲突，或批准指定 URL 的外部规则。"""
    from core.registry import (
        approve_pending_source,
        approve_review,
        list_pending_reviews,
        list_pending_sources,
    )

    if args.approve:
        try:
            approved = approve_review(args.registry, args.candidate, args.approve)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"[错误] 审批失败：{error}", file=sys.stderr)
            return 1
        if not approved:
            print(f"[错误] 未找到待审冲突或候选书源：{args.approve}", file=sys.stderr)
            return 1
        print(f"已批准并更新候选库: {args.approve}")
        return 0

    if args.approve_new:
        try:
            approved = approve_pending_source(args.registry, args.candidate, args.approve_new)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"[错误] 批准待校验书源失败：{error}", file=sys.stderr)
            return 1
        if not approved:
            print(f"[错误] 未找到待校验新源或候选库已有该 URL：{args.approve_new}", file=sys.stderr)
            return 1
        print(f"已批准待校验新源并更新候选库: {args.approve_new}")
        return 0

    reviews = list_pending_reviews(args.registry)
    pending_sources = list_pending_sources(args.registry)
    print(f"待审规则冲突: {len(reviews)} 个")
    for review in reviews:
        print(
            f"- {review.url}（批次 #{review.batch_id}，"
            f"候选 {review.candidate_fingerprint[:8]} → 外部 {review.incoming_fingerprint[:8]}，"
            f"{review.created_at}）"
        )
    print(f"待校验新源: {len(pending_sources)} 个")
    for source in pending_sources:
        print(f"- {source.url}（批次 #{source.batch_id}，{source.created_at}）")
    return 0


# ---------------------------------------------------------------- dedupe
def cmd_dedupe(args: argparse.Namespace) -> int:
    input_path = _resolve_input(args)
    sources = load_json_file(input_path)
    if not isinstance(sources, list):
        print("[错误] 输入不是书源列表", file=sys.stderr)
        return 1
    before = len(sources)
    after_list = dedupe_sources(sources, prefer_keep=args.keep)
    output = args.output or DEFAULT_OUTPUTS["dedupe"]
    dump_json_file(output, after_list)
    print(f"去重完成: {before} → {len(after_list)} → {output}")
    return 0


# ---------------------------------------------------------------- sanitize
def cmd_sanitize(args: argparse.Namespace) -> int:
    """清洗字段类型违规（Legado/Gson 导入兼容）。

    Gson 严格校验类型：customButton/eventListener 期望 bool、
    concurrentRate/lastUpdateTime 期望 int、ruleExplore 期望 dict、
    variableComment 期望 str。外部合并文件常出现 ''/[] 等脏值导致导入报错
    （如 IllegalStateException: Expected a boolean but was BEGIN_ARRAY）。
    """
    from core.sanitize import clean_source
    input_path = _resolve_input(args)
    sources = load_json_file(input_path)
    if not isinstance(sources, list):
        print("[错误] 输入不是书源列表", file=sys.stderr)
        return 1
    fixed = 0
    for s in sources:
        before = json.dumps([s.get(f) for f in
                             ("customButton", "eventListener", "concurrentRate",
                              "ruleExplore", "variableComment", "lastUpdateTime")])
        clean_source(s)
        after = json.dumps([s.get(f) for f in
                            ("customButton", "eventListener", "concurrentRate",
                             "ruleExplore", "variableComment", "lastUpdateTime")])
        if before != after:
            fixed += 1
    output = args.output or input_path  # 缺省就地覆盖
    dump_json_file(output, sources)
    print(f"清洗完成: {len(sources)} 个源，修复 {fixed} 个 → {output}")
    return 0


# ---------------------------------------------------------------- add
def cmd_add(args: argparse.Namespace) -> int:
    """快捷新增书源：给定一个带真实关键词的搜索 URL，自动推断规则生成书源。"""
    from services.add_source import run_add
    url = args.url
    if url == "-" or url is None:
        # 从 stdin 读 URL：
        #  - interactive 模式只取第一行（其余行留给向导问答 input()）
        #  - 非交互模式读全部（可能含换行尾巴）
        if args.interactive:
            url = sys.stdin.readline().strip()
        else:
            url = sys.stdin.read().strip()
        if not url:
            print("[错误] 未提供 URL（可从命令行参数或 stdin 传入）", file=sys.stderr)
            return 1
    return run_add(
        url,
        name=args.name,
        source_type=args.type,
        group=args.group,
        output=args.output,
        no_ask=args.no_ask,
        probe=not args.no_probe,
        detail_url=args.detail_url,
        verify=not args.no_verify,
        pick=args.pick,
        interactive=args.interactive,
        to_merge=args.to,
        discover=getattr(args, "discover", False),
    )


# ---------------------------------------------------------------- run（一条龙）
def cmd_run(args: argparse.Namespace) -> int:
    """一条龙：check → organize → report，产出 checked.json / organized.json / report.md。"""
    from core.checker import run_check

    input_path = _resolve_input(args)
    records, sources = _load_records(input_path, args.limit)
    cache_dir, refresh_cache = _resolve_check_cache(args)
    if getattr(args, "no_cache", False):
        print("缓存策略：不读取、不写入缓存")
    elif refresh_cache:
        print(f"缓存策略：跳过旧缓存，成功结果写入 {cache_dir}")

    # 1) check
    print(f"\n===== [1/3] 联网校验（{len(records)} 个源）=====")
    results = run_check(
        records,
        concurrency=args.concurrency,
        timeout=args.timeout,
        probe_search=not args.no_search_probe,
        keyword=args.keyword,
        verify_ssl=not args.insecure,
        cache_dir=cache_dir,
        max_keywords=getattr(args, "max_keywords", 2),
        proxy=getattr(args, "proxy", None),
        probe_depth=getattr(args, "probe_depth", 1),
        refresh_cache=refresh_cache,
    )
    from collections import Counter
    from core.models import HEALTH_NAMES
    health_counter = Counter(r.health for r in results)
    for h in sorted(health_counter, key=lambda x: -health_counter[x]):
        print(f"  {HEALTH_NAMES.get(h, h):<8} {health_counter[h]}")
    print(f"  可用 {health_counter.get(Health.OK, 0)}/{len(results)}")

    # 2) organize（直接保存带分组的整理结果）
    print("\n===== [2/3] 整理分组 =====")
    from core.organizer import organize_sources
    drop_dead = bool(getattr(args, "drop_dead", False))
    keep_only_ok = bool(getattr(args, "keep_only_ok", False))
    if drop_dead:
        print("（剔除失效源模式已开启）")
    if keep_only_ok:
        print("（仅保留可用源模式已开启）")
    data = organize_sources(results, skip_disabled=not args.keep_disabled,
                            drop_dead=drop_dead, keep_only_ok=keep_only_ok)
    checked_path = getattr(args, "output", "") or "checked.json"
    dump_json_file(checked_path, data)
    print(f"  已保存: {checked_path}（{len(data)} 个源）")

    # 3) report
    print("\n===== [3/3] 生成报告 =====")
    from core.reporter import build_report
    report = build_report(results, source_path=input_path)
    report_path = "report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  已保存: {report_path}")
    print("\n✅ 完成。可将 checked.json 导入 Legado，或查看 report.md 诊断。")
    return 0


# ---------------------------------------------------------------- menu（交互菜单）
MENU_ITEMS = [
    ("check",    "联网校验书源（可用性/星级/被墙检测）"),
    ("organize", "整理分组（按类型+健康度重建）"),
    ("report",   "生成诊断报告（Markdown）"),
    ("run",      "一条龙：校验+整理+报告"),
    ("merge",    "合并多份书源文件"),
    ("dedupe",   "去重"),
    ("add",      "快捷新增书源（URL→自动推断规则）"),
]


def cmd_menu(args: argparse.Namespace) -> int:
    """无参数时的交互菜单：选择操作并引导填写必要参数。"""
    print("\n===== Legado 书源整理工具 =====")
    # 自动探测书源文件，作为后续默认输入
    default_input = _auto_detect_input()
    if default_input:
        print(f"📄 检测到书源文件: {default_input}")
    print()
    for i, (name, desc) in enumerate(MENU_ITEMS, 1):
        print(f"  {i}. {name:<9} {desc}")
    print(f"  {len(MENU_ITEMS) + 1}. 退出")

    choice = _safe_input("\n请选择 [1-8]，回车默认 4：", default="4").strip()
    if not choice.isdigit():
        print("已退出。")
        return 0
    idx = int(choice)
    if idx == len(MENU_ITEMS) + 1:
        print("已退出。")
        return 0
    if idx < 1 or idx > len(MENU_ITEMS):
        print("无效选择。")
        return 0

    name, _ = MENU_ITEMS[idx - 1]
    if name == "add":
        # add 需要搜索 URL，交互引导
        url = _safe_input("输入搜索 URL（含真实关键词）：")
        if not url:
            print("未提供 URL，退出。")
            return 0
        sname = _safe_input("书源名称（回车用域名）：").strip()
        stype = _safe_input("类型 [novel/manga/audio/video]（回车 novel）：").strip() or "novel"
        sgroup = _safe_input("分组（回车 📖新增源）：").strip() or "📖新增源"
        out = _safe_input("输出文件（回车 auto_added.json）：").strip() or "auto_added.json"
        from services.add_source import run_add
        return run_add(url, name=sname, source_type=stype, group=sgroup,
                       output=out, no_ask=True, probe=True, interactive=False)

    # 其余命令：确认输入文件（默认自动探测）
    if name == "merge":
        # merge 需要至少两个文件，走原生 CLI 更合适
        print("合并需要手动指定多份书源文件，请用命令行：")
        print("  python main.py merge -i 文件1.json -i 文件2.json -o merged.json")
        return 0

    # 构造 Namespace，用默认输入文件
    from types import SimpleNamespace
    ns = SimpleNamespace(input=default_input, limit=0)
    if name == "check":
        ns.concurrency = 50
        ns.timeout = 8.0
        ns.keyword = "我"
        ns.testset = ""
        ns.max_keywords = 2
        ns.probe_depth = 1
        ns.proxy = ""
        ns.no_search_probe = False
        ns.insecure = False
        ns.keep_disabled = False
        ns.cache_dir = DEFAULT_CACHE_DIR
        ns.no_cache = False
        ns.refresh_cache = False
        ns.output = ""
    elif name == "organize":
        ns.check_dir = ""
        ns.keep_disabled = False
        ns.output = ""
        # 引导：询问是否剔除失效源
        drop_in = _safe_input("是否剔除失效源（❌ 分组）？[y/N] ").strip().lower()
        ns.drop_dead = drop_in in ("y", "yes", "是")
    elif name == "report":
        ns.check_dir = ""
        ns.output = ""
    elif name == "run":
        ns.concurrency = 50
        ns.timeout = 8.0
        ns.keyword = "我"
        ns.max_keywords = 2
        ns.probe_depth = 1
        ns.proxy = ""
        ns.no_search_probe = False
        ns.insecure = False
        ns.keep_disabled = False
        ns.cache_dir = DEFAULT_CACHE_DIR
        ns.no_cache = False
        ns.refresh_cache = False
        ns.output = ""
        # 引导：询问是否剔除失效源
        drop_in = _safe_input("是否剔除失效源（❌ 分组）？[y/N] ").strip().lower()
        ns.drop_dead = drop_in in ("y", "yes", "是")
    elif name == "dedupe":
        ns.keep = "last"
        ns.output = ""
    else:
        print(f"暂不支持菜单调用: {name}")
        return 0

    funcs = {
        "check": cmd_check, "organize": cmd_organize, "report": cmd_report,
        "run": cmd_run, "dedupe": cmd_dedupe,
    }
    return funcs[name](ns)


# ---------------------------------------------------------------- main
def cmd_reclassify(args: argparse.Namespace) -> int:
    from core.reclassify import cmd_reclassify as _impl
    return _impl(args)


def cmd_diagnose(args: argparse.Namespace) -> int:
    from core.reclassify import cmd_diagnose as _impl
    return _impl(args)


def cmd_repair(args: argparse.Namespace) -> int:
    from core.repair.loop import cmd_repair as _impl
    return _impl(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legado-tools",
        description="Legado 书源整理工具：校验可用性、重建分组、诊断报告、合并去重",
    )
    sub = parser.add_subparsers(dest="command")

    # check
    p_check = sub.add_parser("check", help="高并发联网校验书源可用性/稳定性")
    p_check.add_argument("-i", "--input", default="", help="书源 JSON 文件（缺省自动探测）")
    p_check.add_argument("-o", "--output", default="", help=f"校验后整理输出 JSON（缺省 {DEFAULT_OUTPUTS['check']}）")
    p_check.add_argument("-c", "--concurrency", type=int, default=50, help="并发数（默认 50）")
    p_check.add_argument("-t", "--timeout", type=float, default=8.0, help="单请求超时秒数（默认 8）")
    p_check.add_argument("-k", "--keyword", default="我", help="搜索探测关键词（默认「我」）")
    p_check.add_argument("--testset", default="", help="自定义测试集 JSON 文件：{\"novel\": [书名], \"manga\": [漫画名]}（默认内置）")
    p_check.add_argument("--max-keywords", type=int, default=2, help="每个源最多尝试测试集关键词数（默认 2）")
    p_check.add_argument("--probe-depth", type=int, choices=[1, 2, 3], default=1,
                         help="探测深度：1=浅探测(静态规则判星级，默认) 2=命中后验证目录完整度 3=再抽样一章验证正文可用性")
    p_check.add_argument("--proxy", default="", help="代理地址（如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890），用于对疑似被墙源复检")
    p_check.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（测试用）")
    p_check.add_argument("--no-search-probe", action="store_true", help="跳过搜索探测，只测域名连通")
    p_check.add_argument("--insecure", action="store_true", help="不校验证书（规避 SSL 报错）")
    p_check.add_argument("--keep-disabled", action="store_true", help="输出时保留 enabled=false 的源")
    p_check.add_argument("--cache-dir", default="", help=f"校验缓存目录（缺省 {DEFAULT_CACHE_DIR}）")
    p_check.add_argument("--no-cache", action="store_true",
                         help="完全禁用缓存：不读取旧缓存，也不写入本次结果（优先级高于 --refresh-cache）")
    p_check.add_argument("--refresh-cache", action="store_true",
                         help="跳过旧缓存，重新联网校验；成功结果仍写入缓存")
    p_check.set_defaults(func=cmd_check)

    # organize
    p_org = sub.add_parser("organize", help="按内容类型 + 健康状态重建清晰分组")
    p_org.add_argument("-i", "--input", default="", help="书源 JSON 文件（缺省自动探测）")
    p_org.add_argument("-o", "--output", default="", help=f"输出 JSON 文件（缺省 {DEFAULT_OUTPUTS['organize']}）")
    p_org.add_argument("-r", "--check-dir", default="", help="校验缓存目录（可选，用于健康分组）")
    p_org.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（测试用）")
    p_org.add_argument("--keep-disabled", action="store_true", help="输出时保留 enabled=false 的源")
    p_org.add_argument("--drop-dead", action="store_true",
                       help="剔除失效源（Health.DEAD；需验证/被墙源保留）")
    p_org.add_argument("--keep-only-ok", action="store_true",
                       help="只保留 ✅可用 源（精简导入版）")
    p_org.set_defaults(func=cmd_organize)

    # report
    p_rep = sub.add_parser("report", help="生成 Markdown 诊断报告")
    p_rep.add_argument("-i", "--input", default="", help="书源 JSON 文件（缺省自动探测）")
    p_rep.add_argument("-o", "--output", default="", help=f"输出 Markdown 文件（缺省 {DEFAULT_OUTPUTS['report']}）")
    p_rep.add_argument("-r", "--check-dir", default="", help="校验缓存目录（可选）")
    p_rep.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（测试用）")
    p_rep.set_defaults(func=cmd_report)

    # merge
    p_mrg = sub.add_parser("merge", help="合并/更新多份书源文件")
    p_mrg.add_argument("-i", "--input", action="append", required=True, help="输入书源 JSON（可多次）")
    p_mrg.add_argument("-o", "--output", default="", help=f"输出 JSON 文件（缺省 {DEFAULT_OUTPUTS['merge']}）")
    p_mrg.add_argument("--mode", choices=["replace", "keep", "both"], default="replace",
                       help="同名/同URL冲突处理：replace=新覆盖旧(默认) keep=保留旧 both=都保留")
    p_mrg.add_argument("--no-dedupe", action="store_true", help="合并后不去重")
    p_mrg.add_argument("--prefer-input", choices=["first", "last"], default="",
                       help="同 URL 冲突时优先第一个或最后一个输入")
    p_mrg.set_defaults(func=cmd_merge)

    # prepare —— 一键生成附件优先的候选导出
    p_prep = sub.add_parser("prepare", help="附件优先合并并生成完整候选版/快速使用版")
    p_prep.add_argument("-i", "--input", action="append", required=True, help="输入书源 JSON（按优先级顺序，可多次）")
    p_prep.add_argument("--prefer-input", choices=["first", "last"], default="first",
                        help="冲突时优先第一个或最后一个输入（默认 first）")
    p_prep.add_argument("-o", "--merged-output", default="candidates-merged.json", help="去重合并输出")
    p_prep.add_argument("--full-output", default="candidates-full.json", help="完整候选导出")
    p_prep.add_argument("--fast-output", default="candidates-fast.json", help="仅可用源导出")
    p_prep.set_defaults(func=cmd_prepare)

    # import-sources —— 外部书源安全导入
    p_imp = sub.add_parser("import-sources", help="安全导入外部书源（新增待校验，冲突待审）")
    p_imp.add_argument("--candidate", required=True, help="候选书源 JSON（只读，不会被导入命令改写）")
    p_imp.add_argument("-i", "--input", required=True, help="外部书源 JSON")
    p_imp.add_argument("--registry", default="book_sources.sqlite3", help="导入台账 SQLite 文件")
    p_imp.add_argument("--raw-dir", default="imports/raw", help="外部原始文件留存目录")
    p_imp.set_defaults(func=cmd_import_sources)

    # review-imports —— 待审冲突查看与审批
    p_review = sub.add_parser("review-imports", help="查看或批准外部书源规则冲突")
    p_review.add_argument("--candidate", required=True, help="候选书源 JSON（批准时写入对应条目）")
    p_review.add_argument("--registry", default="book_sources.sqlite3", help="导入台账 SQLite 文件")
    p_review.add_argument("--approve", default="", help="批准指定 URL 的外部规则并更新候选库")
    p_review.add_argument("--approve-new", default="", help="确认校验通过后，将指定新源加入候选库")
    p_review.add_argument("--list", action="store_true", help="列出待审冲突（缺省也会列出）")
    p_review.set_defaults(func=cmd_review_imports)

    # dedupe
    p_ded = sub.add_parser("dedupe", help="按 URL/名称去重")
    p_ded.add_argument("-i", "--input", default="", help="输入书源 JSON（缺省自动探测）")
    p_ded.add_argument("-o", "--output", default="", help=f"输出 JSON 文件（缺省 {DEFAULT_OUTPUTS['dedupe']}）")
    p_ded.add_argument("--keep", choices=["first", "last"], default="last",
                       help="保留第一个还是最后一个（默认 last，保留较新）")
    p_ded.set_defaults(func=cmd_dedupe)

    # sanitize —— 清洗字段类型（Gson 导入兼容）
    p_san = sub.add_parser("sanitize", help="清洗字段类型违规（Legado/Gson 导入兼容）")
    p_san.add_argument("-i", "--input", default="", help="输入书源 JSON（缺省自动探测）")
    p_san.add_argument("-o", "--output", default="", help="输出 JSON 文件（缺省就地覆盖）")
    p_san.set_defaults(func=cmd_sanitize)

    # run —— 一条龙
    p_run = sub.add_parser("run", help="一条龙：校验 → 整理 → 报告")
    p_run.add_argument("-i", "--input", default="", help="书源 JSON 文件（缺省自动探测）")
    p_run.add_argument("-o", "--output", default="", help="校验整理输出 JSON（缺省 checked.json）")
    p_run.add_argument("-c", "--concurrency", type=int, default=50, help="并发数（默认 50）")
    p_run.add_argument("-t", "--timeout", type=float, default=8.0, help="单请求超时秒数（默认 8）")
    p_run.add_argument("-k", "--keyword", default="我", help="搜索探测关键词（默认「我」）")
    p_run.add_argument("--max-keywords", type=int, default=2, help="每个源最多尝试测试集关键词数（默认 2）")
    p_run.add_argument("--probe-depth", type=int, choices=[1, 2, 3], default=1,
                       help="探测深度：1=浅探测(静态规则判星级，默认) 2=命中后验证目录完整度 3=再抽样一章验证正文可用性")
    p_run.add_argument("--proxy", default="", help="代理地址")
    p_run.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（测试用）")
    p_run.add_argument("--no-search-probe", action="store_true", help="跳过搜索探测")
    p_run.add_argument("--insecure", action="store_true", help="不校验证书")
    p_run.add_argument("--keep-disabled", action="store_true", help="输出时保留 enabled=false 的源")
    p_run.add_argument("--drop-dead", action="store_true",
                       help="剔除失效源（Health.DEAD；需验证/被墙源保留）")
    p_run.add_argument("--keep-only-ok", action="store_true",
                       help="只保留 ✅可用 源（精简导入版）")
    p_run.add_argument("--cache-dir", default="", help=f"校验缓存目录（缺省 {DEFAULT_CACHE_DIR}）")
    p_run.add_argument("--no-cache", action="store_true",
                       help="完全禁用缓存：不读取旧缓存，也不写入本次结果（优先级高于 --refresh-cache）")
    p_run.add_argument("--refresh-cache", action="store_true",
                       help="跳过旧缓存，重新联网校验；成功结果仍写入缓存")
    p_run.set_defaults(func=cmd_run)

    # add —— 快捷新增书源（自动推断规则）
    p_add = sub.add_parser("add", help="快捷新增书源：URL→自动推断搜索规则→生成书源")
    p_add.add_argument("url", nargs="?", help="带真实关键词的搜索 URL，如 https://host/search?q=绍宋；传 - 从 stdin 读取")
    p_add.add_argument("--name", default="", help="书源名称（默认取域名）")
    p_add.add_argument("--type", choices=["novel", "manga", "audio", "video"], default="novel",
                       help="内容类型（默认 novel 小说）")
    p_add.add_argument("--group", default="📖新增源", help="分组名（默认 📖新增源）")
    p_add.add_argument("--output", default="auto_added.json", help="输出书源文件（默认 auto_added.json）")
    p_add.add_argument("--no-ask", action="store_true", help="不确认直接保存")
    p_add.add_argument("--no-probe", action="store_true", help="不自动探测常见搜索端点（keyboard/key/wd...）")
    p_add.add_argument("--detail-url", default="", help="详情页样例 URL（自动推断目录/正文规则；缺省用搜索结果第一条当样例）")
    p_add.add_argument("--pick", type=int, default=1, help="用搜索结果第 N 条当详情页样例（默认 1）")
    p_add.add_argument("--no-verify", action="store_true", help="跳过全链路验证（搜索→详情→目录→正文）")
    p_add.add_argument("--interactive", action="store_true", help="半自动向导模式：逐步确认")
    p_add.add_argument("--to", default="", help="生成后自动 merge 进该书源 JSON（一键打通主流程）")
    p_add.add_argument("--discover", action="store_true",
                       help="仅发现模式：跳过搜索探测，直接生成无搜索规则的书源（分组标记「仅发现」，适合搜索被限流的站）")
    p_add.set_defaults(func=cmd_add)

    # reclassify —— 按实测信号重判书源类型（漫画被标成小说）
    p_rc = sub.add_parser("reclassify", help="按实测信号重判书源类型（修正漫画/小说错标）")
    p_rc.add_argument("-i", "--input", required=True, help="书源 JSON 数组")
    p_rc.add_argument("-o", "--output", help="输出文件（默认覆盖输入）")
    p_rc.add_argument("--write", action="store_true", help="回写 bookSourceType")
    p_rc.add_argument("--limit", type=int, default=30, help="控制台明细条数")
    p_rc.set_defaults(func=cmd_reclassify)

    # diagnose —— 失效源归因
    p_dg = sub.add_parser("diagnose", help="失效源归因：死站/规则漂移/站点转型/需验证")
    p_dg.add_argument("-i", "--input", required=True, help="书源 JSON 数组")
    p_dg.add_argument("-o", "--output", help="Markdown 报告输出路径")
    p_dg.add_argument("--only-dead", action="store_true", help="只探测非 OK 的源")
    p_dg.add_argument("-c", "--concurrency", type=int, default=20)
    p_dg.add_argument("-t", "--timeout", type=float, default=8.0)
    p_dg.add_argument("--keywords", nargs="*", default=None, help="搜索探测关键词")
    p_dg.set_defaults(func=cmd_diagnose)

    # repair —— AI 规则修复循环（提议 -> 回放验证 -> 重试）
    p_rp = sub.add_parser("repair", help="AI 修复失效规则：抓证据 -> 模型提议 -> 回放验证 -> 重试")
    p_rp.add_argument("-i", "--input", required=True, help="书源 JSON 数组（通常是 diagnose 后的批次）")
    p_rp.add_argument("-o", "--output", help="修复结果写回的文件（默认覆盖输入）")
    p_rp.add_argument("--report", help="Markdown 修复报告输出路径")
    p_rp.add_argument("--write", action="store_true", help="把修复成功的源回写")
    p_rp.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    p_rp.add_argument("--rounds", type=int, default=3, help="每个源最多重试轮数")
    p_rp.add_argument("--keyword", default="我的", help="用于取样的搜索关键词")
    p_rp.add_argument("-c", "--concurrency", type=int, default=4)
    p_rp.add_argument("-t", "--timeout", type=float, default=15.0)
    p_rp.set_defaults(func=cmd_repair)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # Windows 控制台 UTF-8
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if getattr(args, "command", None) is None:
        # 无子命令 → 进入交互菜单
        return cmd_menu(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

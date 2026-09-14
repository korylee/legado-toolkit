# -*- coding: utf-8 -*-
"""缓存后端双跑对比：SQLite 管理库 vs 旧 NDJSON。"""

# 目标：证明第 4 步切换后，两种后端对同一批源给出**等价**的缓存判定与记录。
# 刻意不联网——纯比对缓存层，否则网络抖动会掩盖真实差异。
#
# 用法：
#     python -m core.cache_parity --sample 300
#     python -m core.cache_parity --sample 300 --show 30

from __future__ import annotations

import argparse
from collections import Counter
from typing import Any, Dict, List, Optional

from core.checker import AsyncChecker, is_cache_item_valid, restore_from_cache
from core.loader import _normalize_url, load_json_file
from core.models import build_record
from core.paths import data_path

#: restore_from_cache 会填的字段，逐个比对
FIELDS = (
    "health", "status_code", "response_time_ms", "error", "checked_at",
    "search_hit", "search_response_ms", "probe_depth", "chapter_count",
    "toc_complete", "toc_fail_reason", "content_ok", "content_fail_reason",
    "content_response_ms", "quality_stars", "quality_tags",
)


def load_store_cache(store_path: Optional[str] = None) -> Dict[str, dict]:
    """走 SQLite 后端（use_store=True），复用 AsyncChecker.load_cache()。"""
    ck = AsyncChecker(store_path=store_path, use_store=True)
    try:
        return ck.load_cache()
    finally:
        ck.close()


def load_legacy_cache(cache_dirs=None):
    # 迁移读的是 check_cache + check_cache_current 两个目录，
    # 对比必须同口径，否则会把 check_cache_current 独有的记录误判为差异。
    dirs = list(cache_dirs or (data_path("check_cache"),
                              data_path("check_cache_current")))
    raw_n = 0
    merged = {}
    for d in dirs:
        ck = AsyncChecker(cache_dir=d, use_store=False)
        items = ck.load_cache() or {}
        raw_n += len(items)
        for k, v in items.items():
            nk = _normalize_url(k)
            cur = merged.get(nk)
            if not cur or str(v.get("checked_at", "")) >= str(cur.get("checked_at", "")):
                merged[nk] = v
    return raw_n, merged


def _snapshot(raw: Dict[str, Any], index: int, item: Dict[str, Any]) -> Dict[str, Any]:
    rec = build_record(raw, index)
    restore_from_cache(rec, item)
    return {f: getattr(rec, f, None) for f in FIELDS}


def compare(sample: int = 0, candidates: str = "", cache_dirs=None,
            store_path: Optional[str] = None, show: int = 20) -> Dict[str, Any]:
    path = candidates or data_path("candidates.json")
    srcs = load_json_file(path)
    if not isinstance(srcs, list):
        raise SystemExit("候选库不是数组: %s" % path)
    total = len(srcs)
    if sample and sample < total:
        step = max(1, total // sample)
        srcs = srcs[::step][:sample]

    raw_store = load_store_cache(store_path)
    legacy_raw_n, legacy = load_legacy_cache(cache_dirs)


    stats: Counter = Counter()
    diffs: Counter = Counter()
    details: List[Dict[str, Any]] = []
    only_store: List[str] = []
    only_legacy: List[str] = []

    for idx, src in enumerate(srcs):
        rec = build_record(src, idx)
        u = _normalize_url(rec.url)
        if not u:
            stats["无 URL"] += 1
            continue
        ia = raw_store.get(u)
        ib = legacy.get(u)
        va = bool(ia) and is_cache_item_valid(rec, ia)
        vb = bool(ib) and is_cache_item_valid(rec, ib)
        stats[("命中" if va else "未命中") + " / " + ("命中" if vb else "未命中")] += 1

        if va and vb:
            fa = _snapshot(rec.raw, idx, ia)
            fb = _snapshot(rec.raw, idx, ib)
            bad = [f for f in FIELDS if fa.get(f) != fb.get(f)]
            if bad:
                diffs.update(bad)
                if len(details) < show:
                    details.append({
                        "name": rec.name, "url": rec.url, "fields": bad,
                        "sqlite": {f: fa.get(f) for f in bad},
                        "ndjson": {f: fb.get(f) for f in bad},
                    })
        elif va and not vb:
            only_store.append(rec.url)
        elif vb and not va:
            only_legacy.append(rec.url)

    return {
        "candidates_total": total,
        "sample": len(srcs),
        "store_entries": len(raw_store),
        "legacy_raw_entries": legacy_raw_n,
        "legacy_normalized_entries": len(legacy),
        "legacy_deduped": legacy_raw_n - len(legacy),
        "hit_matrix": dict(stats),
        "field_diffs": dict(diffs),
        "details": details,
        "only_store": only_store[:show],
        "only_legacy": only_legacy[:show],
        "only_store_count": len(only_store),
        "only_legacy_count": len(only_legacy),
        "parity": (not diffs) and len(only_store) == 0 and len(only_legacy) == 0,
    }



def print_report(res: Dict[str, Any]) -> None:
    bar = "=" * 64
    print(bar)
    print("缓存后端双跑对比（不联网，纯比对缓存层）")
    print(bar)
    print("候选库总源数       : %d" % res["candidates_total"])
    print("本次抽样           : %d" % res["sample"])
    print("SQLite 缓存条数    : %d" % res["store_entries"])
    print("NDJSON 原始条数    : %d" % res["legacy_raw_entries"])
    print("NDJSON 归一后条数  : %d   （URL 变体合并掉 %d 条）"
          % (res["legacy_normalized_entries"], res["legacy_deduped"]))
    print()
    print("命中矩阵（SQLite / NDJSON）:")
    for k in sorted(res["hit_matrix"]):
        print("  %-18s %d" % (k, res["hit_matrix"][k]))
    print()
    if res["field_diffs"]:
        print("字段差异统计:")
        for f, n in sorted(res["field_diffs"].items(), key=lambda kv: -kv[1]):
            print("  %-22s %d" % (f, n))
    else:
        print("字段差异: 无（两边记录完全一致）")
    print("仅 SQLite 有记录   : %d" % res["only_store_count"])
    print("仅 NDJSON 有记录   : %d" % res["only_legacy_count"])
    if res["only_store"]:
        print("  SQLite 独有样例: " + " | ".join(res["only_store"][:3]))
    if res["only_legacy"]:
        print("  NDJSON 独有样例: " + " | ".join(res["only_legacy"][:3]))
    for d in res["details"][:10]:
        print()
        print("  [差异] %s" % d["name"])
        print("    url: %s" % d["url"])
        for f in d["fields"]:
            print("    %-22s sqlite=%r  ndjson=%r"
                  % (f, d["sqlite"].get(f), d["ndjson"].get(f)))
    print()
    print(bar)
    print("结论: " + ("两种后端等价" if res["parity"] else "存在差异，需逐条核查"))
    print(bar)


def main() -> int:
    ap = argparse.ArgumentParser(prog="cache_parity", description="缓存后端双跑对比（不联网）")
    ap.add_argument("--sample", type=int, default=300, help="抽样源数（0=全部）")
    ap.add_argument("--candidates", default="", help="候选库 JSON 路径")
    ap.add_argument("--cache-dir", default="", help="NDJSON 缓存目录")
    ap.add_argument("--store", default="", help="SQLite 库路径")
    ap.add_argument("--show", type=int, default=20, help="明细条数上限")
    ap.add_argument("--json", default="", help="把结果写成 JSON 文件")
    args = ap.parse_args()
    res = compare(args.sample, args.candidates, args.cache_dir or None,
                  args.store or None, args.show)
    print_report(res)
    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("结果已写入 %s" % args.json)
    return 0 if res["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


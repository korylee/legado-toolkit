# -*- coding: utf-8 -*-
"""把现有 JSON / NDJSON 资产迁进 SQLite 管理库，并做一致性校验。"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional

from core.paths import data_path
from core.store import Store, now

CANDIDATES = "candidates.json"
CACHE_DIRS = ("check_cache", "check_cache_current")


def load_candidates(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or data_path(CANDIDATES)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def iter_cache_items(cache_dirs=None) -> List[Dict[str, Any]]:
    # 同一 url 只保留 checked_at 最新的一条（与旧 load_cache 语义一致）
    from core.loader import _normalize_url

    dirs = list(cache_dirs or [data_path(d) for d in CACHE_DIRS])
    best: Dict[str, Dict[str, Any]] = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not (fn.startswith("check_") and fn.endswith(".ndjson")):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        url = _normalize_url(item.get("url") or "")
                        if not url:
                            continue
                        cur = best.get(url)
                        if not cur or str(item.get("checked_at", "")) >= str(cur.get("checked_at", "")):
                            best[url] = item
            except Exception:
                continue
    return list(best.values())


def _file_side(srcs, items) -> Dict[str, Any]:
    """按「文件」口径统计，校验列与 v_sources 对齐。"""
    from core.loader import _normalize_url

    cache = {_normalize_url(i.get("url", "")): i for i in items}
    health, stars = Counter(), Counter()
    for s in srcs:
        it = cache.get(_normalize_url(str(s.get("bookSourceUrl", "") or "")))
        health[it.get("health") if it else None] += 1
        stars[it.get("quality_stars") if it else None] += 1
    return {
        "sources": len(srcs),
        "types": Counter(int(s.get("bookSourceType", 0) or 0) for s in srcs),
        "health": health,
        "stars": stars,
        "cache_entries": len(items),
    }


def _norm(d: Any) -> Dict[str, int]:
    return {("未校验" if k is None else str(k)): int(v) for k, v in dict(d).items()}


def migrate(db_path: Optional[str] = None, candidates: Optional[str] = None,
            cache_dirs=None, reset: bool = False, verbose: bool = True) -> Dict[str, Any]:
    from core.store import resolve_db_path

    srcs = load_candidates(candidates)
    items = iter_cache_items(cache_dirs)
    db_path = resolve_db_path(db_path)
    if reset:
        for _suf in ("", "-wal", "-shm"):
            _f = db_path + _suf
            if os.path.exists(_f):
                os.remove(_f)
    with Store(db_path) as st:
        n_src = st.upsert_sources(srcs)
        n_chk = st.save_checks(items)
        st.set_meta("migrated_at", now())
        st.set_meta("source_candidates", os.path.basename(candidates or CANDIDATES))
        stats = st.stats()
    out = {"sources_written": n_src, "checks_written": n_chk, "db_stats": stats}
    if verbose:
        print("迁移完成: 源 %d，校验缓存 %d" % (n_src, n_chk))
        print("库内统计:", stats)
    return out


def verify(db_path: Optional[str] = None, candidates: Optional[str] = None,
           cache_dirs=None) -> Dict[str, Any]:
    """对比文件口径与库口径的四项分布。"""
    srcs = load_candidates(candidates)
    items = iter_cache_items(cache_dirs)
    fs = _file_side(srcs, items)
    with Store(db_path, readonly=True) as st:
        db_types = Counter({int(k): v for k, v in st.stats()["types"].items()})
        rows = st.conn.execute("SELECT health, stars FROM v_sources").fetchall()
        db_health = Counter(r["health"] for r in rows)
        db_stars = Counter(r["stars"] for r in rows)
        db_sources = st.count_sources()
        db_checks = st.count_checks()
    def cmp_(a, b):
        return _norm(a) == _norm(b)
    res = {
        "sources": {"files": fs["sources"], "db": db_sources,
                    "match": fs["sources"] == db_sources},
        "checks": {"files": fs["cache_entries"], "db": db_checks,
                   "match": fs["cache_entries"] == db_checks},
        "types": {"files": _norm(fs["types"]), "db": _norm(db_types),
                  "match": cmp_(fs["types"], db_types)},
        "health": {"files": _norm(fs["health"]), "db": _norm(db_health),
                   "match": cmp_(fs["health"], db_health)},
        "stars": {"files": _norm(fs["stars"]), "db": _norm(db_stars),
                  "match": cmp_(fs["stars"], db_stars)},
    }
    res["all_match"] = all(v["match"] for v in res.values())
    return res


def print_verify(res: Dict[str, Any]) -> None:
    for key in ("sources", "checks"):
        v = res[key]
        print("  %-8s 文件=%-6s 库=%-6s %s" % (key, v["files"], v["db"],
                                              "OK" if v["match"] else "不一致"))
    for key in ("types", "health", "stars"):
        v = res[key]
        print("  %-8s %s" % (key, "OK" if v["match"] else "不一致"))
        if not v["match"]:
            print("      文件:", v["files"])
            print("      库  :", v["db"])
    print("总体:", "全部一致" if res["all_match"] else "存在差异")


def main() -> int:
    ap = argparse.ArgumentParser(prog="store_migrate")
    ap.add_argument("action", choices=["migrate", "verify"], help="migrate=导入, verify=校验")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 data/sources.sqlite3）")
    ap.add_argument("--candidates", default=None, help="候选库 JSON 路径")
    ap.add_argument("--cache-dirs", nargs="*", default=None, help="缓存目录（可多个）")
    ap.add_argument("--reset", action="store_true", help="迁移前删除旧库")
    args = ap.parse_args()
    if args.action == "migrate":
        migrate(args.db, args.candidates, args.cache_dirs, reset=args.reset)
        print("一致性校验:")
        print_verify(verify(args.db, args.candidates, args.cache_dirs))
    else:
        print_verify(verify(args.db, args.candidates, args.cache_dirs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


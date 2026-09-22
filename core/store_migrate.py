# -*- coding: utf-8 -*-
"""把 candidates.json 迁进 SQLite 管理库，并做一致性校验。"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional

from core.paths import data_path
from core.store import Store, now

CANDIDATES = "candidates.json"


def load_candidates(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or data_path(CANDIDATES)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _file_side(srcs) -> Dict[str, Any]:
    """按「文件」口径统计源数与类型分布。"""
    return {
        "sources": len(srcs),
        "types": Counter(int(s.get("bookSourceType", 0) or 0) for s in srcs),
    }


def _norm(d: Any) -> Dict[str, int]:
    return {("未校验" if k is None else str(k)): int(v) for k, v in dict(d).items()}


def migrate(db_path: Optional[str] = None, candidates: Optional[str] = None,
            reset: bool = False, verbose: bool = True) -> Dict[str, Any]:
    from core.store import resolve_db_path

    srcs = load_candidates(candidates)
    db_path = resolve_db_path(db_path)
    if reset:
        for _suf in ("", "-wal", "-shm"):
            _f = db_path + _suf
            if os.path.exists(_f):
                os.remove(_f)
    with Store(db_path) as st:
        n_src = st.upsert_sources(srcs)
        st.set_meta("migrated_at", now())
        st.set_meta("source_candidates", os.path.basename(candidates or CANDIDATES))
        stats = st.stats()
    out = {"sources_written": n_src, "db_stats": stats}
    if verbose:
        print("迁移完成：源 %d" % n_src)
        print("库内统计：", stats)
    return out


def verify(db_path: Optional[str] = None, candidates: Optional[str] = None) -> Dict[str, Any]:
    """对比文件口径与库口径的源数与类型分布。"""
    srcs = load_candidates(candidates)
    fs = _file_side(srcs)
    with Store(db_path, readonly=True) as st:
        db_types = Counter({int(k): v for k, v in st.stats()["types"].items()})
        db_sources = st.count_sources()
    res = {
        "sources": {"files": fs["sources"], "db": db_sources,
                    "match": fs["sources"] == db_sources},
        "types": {"files": _norm(fs["types"]), "db": _norm(db_types),
                  "match": _norm(fs["types"]) == _norm(db_types)},
    }
    res["all_match"] = all(v["match"] for v in res.values())
    return res


def print_verify(res: Dict[str, Any]) -> None:
    for key in ("sources", "types"):
        v = res[key]
        print("  %-8s 文件=%-6s 库=%-6s %s" % (key, v["files"], v["db"],
                                              "OK" if v["match"] else "不一致"))
    print("总体：", "全部一致" if res["all_match"] else "存在差异")


def main() -> int:
    ap = argparse.ArgumentParser(prog="store_migrate")
    ap.add_argument("action", choices=["migrate", "verify"], help="migrate=导入, verify=校验")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 data/sources.sqlite3）")
    ap.add_argument("--candidates", default=None, help="候选库 JSON 路径")
    ap.add_argument("--reset", action="store_true", help="迁移前删除旧库")
    args = ap.parse_args()
    if args.action == "migrate":
        migrate(args.db, args.candidates, reset=args.reset)
        print("一致性校验：")
        print_verify(verify(args.db, args.candidates))
    else:
        print_verify(verify(args.db, args.candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

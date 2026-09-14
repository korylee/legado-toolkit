# -*- coding: utf-8 -*-
"""SQLite 管理库。"""

# 分工：
#   SQLite = 管理库（查询 / 校验缓存 / 归因 / 修复历史 / 任务）
#   JSON   = 交付格式（Legado 导入 / 论坛分享），由 export_json() 重新生成
#
# 设计要点：
#   - raw_json 保存完整书源原文 -> 导出无损、迁移零风险
#   - 只把需要 WHERE / ORDER BY 的字段抽成列，规则本身不拆表
#   - fingerprint 复用 loader.fingerprint()，规则一变缓存即失效

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

SCHEMA_VERSION = 2
DB_NAME = "sources.sqlite3"

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
)

DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS sources (
        id           INTEGER PRIMARY KEY,
        source_url   TEXT NOT NULL UNIQUE,
        name         TEXT NOT NULL DEFAULT '',
        source_type  INTEGER NOT NULL DEFAULT 0,
        group_name   TEXT NOT NULL DEFAULT '',
        enabled      INTEGER NOT NULL DEFAULT 1,
        raw_json     TEXT NOT NULL,
        fingerprint  TEXT NOT NULL DEFAULT '',
        deleted_at   TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sources_type  ON sources(source_type)",
    "CREATE INDEX IF NOT EXISTS idx_sources_group ON sources(group_name)",
    "CREATE INDEX IF NOT EXISTS idx_sources_name  ON sources(name)",
    """CREATE TABLE IF NOT EXISTS checks (
        id                  INTEGER PRIMARY KEY,
        source_url          TEXT NOT NULL,
        fingerprint         TEXT NOT NULL DEFAULT '',
        cache_version       INTEGER NOT NULL DEFAULT 0,
        health              TEXT NOT NULL DEFAULT '',
        status_code         INTEGER,
        response_time_ms    INTEGER,
        search_hit          TEXT DEFAULT '',
        search_response_ms  INTEGER,
        stars               INTEGER DEFAULT 0,
        quality_tags        TEXT DEFAULT '',
        probe_depth         INTEGER DEFAULT 1,
        chapter_count       INTEGER DEFAULT 0,
        toc_complete        INTEGER,
        toc_fail_reason     TEXT DEFAULT '',
        content_fail_reason TEXT DEFAULT '',
        content_response_ms INTEGER,
        content_ok          INTEGER,
        error               TEXT DEFAULT '',
        checked_at          TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_checks_url ON checks(source_url, checked_at DESC)",
    """CREATE TABLE IF NOT EXISTS diagnosis (
        id           INTEGER PRIMARY KEY,
        source_url   TEXT NOT NULL,
        bucket       TEXT NOT NULL DEFAULT '',
        attribution  TEXT DEFAULT '',
        type_guess   INTEGER,
        evidence_json TEXT DEFAULT '',
        diagnosed_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_diag_url ON diagnosis(source_url, diagnosed_at DESC)",
    """CREATE TABLE IF NOT EXISTS repairs (
        id            INTEGER PRIMARY KEY,
        source_url    TEXT NOT NULL,
        round         INTEGER DEFAULT 0,
        status        TEXT NOT NULL DEFAULT '',
        model         TEXT DEFAULT '',
        before_rules  TEXT DEFAULT '',
        proposal_json TEXT DEFAULT '',
        verify_json   TEXT DEFAULT '',
        created_at    TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_repairs_url ON repairs(source_url, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS exports (
        uid         TEXT PRIMARY KEY,
        name        TEXT DEFAULT "",
        count       INTEGER DEFAULT 0,
        urls_json   TEXT DEFAULT "",
        filename    TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        hits        INTEGER DEFAULT 0,
        pinned      INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_exports_exp ON exports(expires_at)",
    """CREATE TABLE IF NOT EXISTS jobs (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT '',
        progress    INTEGER DEFAULT 0,
        total       INTEGER DEFAULT 0,
        payload     TEXT DEFAULT '',
        result_json TEXT DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
    # 源 + 最近一次校验：前端列表页就查这个视图
]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def resolve_db_path(path: Optional[str] = None) -> str:
    from core.paths import data_path

    return path or data_path(DB_NAME)


class Store:
    """SQLite 管理库。支持 with 语句。"""

    def __init__(self, path: Optional[str] = None, readonly: bool = False):
        self.path = resolve_db_path(path)
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not readonly:
            os.makedirs(d, exist_ok=True)
        if readonly:
            self.conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                                        timeout=5.0)
        else:
            self.conn = sqlite3.connect(self.path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        for p in PRAGMAS:
            try:
                self.conn.execute(p)
            except sqlite3.Error:
                pass
        if not readonly:
            self._init_schema()

    #: 需要幂等补加的新列 {表: [(列名, 定义)]}
    #: CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，必须显式 ALTER
    NEW_COLUMNS = {"sources": [("deleted_at", "TEXT NOT NULL DEFAULT ''")]}

    #: v_sources 视图每次重建：CREATE VIEW IF NOT EXISTS 不会更新已存在的视图定义
    VIEW_DDL = """CREATE VIEW v_sources AS
        SELECT s.id, s.source_url, s.name, s.source_type, s.group_name, s.enabled,
               s.fingerprint, s.deleted_at, s.updated_at,
               c.health, c.stars, c.checked_at, c.probe_depth,
               c.toc_complete, c.content_ok, c.search_hit
        FROM sources s
        LEFT JOIN checks c ON c.id = (
            SELECT id FROM checks WHERE source_url = s.source_url
            ORDER BY checked_at DESC LIMIT 1)"""

    def _init_schema(self) -> None:
        for stmt in DDL:
            self.conn.execute(stmt)
        for table, cols in self.NEW_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
        self.conn.execute("DROP VIEW IF EXISTS v_sources")
        self.conn.execute(self.VIEW_DDL)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", ("schema_version", "1"))
        self.conn.execute("UPDATE meta SET value = ? WHERE key = ?",
                          (str(SCHEMA_VERSION), "schema_version"))
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        self.conn.commit()

# sources ------------------------------------------------------------
    def upsert_sources(self, sources, with_fingerprint: bool = True) -> int:
        """批量写入/更新书源。同一 source_url 覆盖，raw_json 全量替换。"""
        from core.loader import _normalize_url, fingerprint as fp_of

        ts = now()
        rows = []
        for src in sources or []:
            if not isinstance(src, dict):
                continue
            url = _normalize_url(str(src.get("bookSourceUrl", "") or ""))
            if not url:
                continue
            rows.append((
                url,
                str(src.get("bookSourceName", "") or ""),
                int(src.get("bookSourceType", 0) or 0),
                str(src.get("bookSourceGroup", "") or ""),
                1 if src.get("enabled", True) else 0,
                json.dumps(src, ensure_ascii=False),
                fp_of(src) if with_fingerprint else "",
                ts, ts,
            ))
        if not rows:
            return 0
        sql = (
            "INSERT INTO sources(source_url,name,source_type,group_name,enabled,"
            "raw_json,fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_url) DO UPDATE SET name=excluded.name, "
            "source_type=excluded.source_type, group_name=excluded.group_name, "
            "enabled=excluded.enabled, raw_json=excluded.raw_json, "
            "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at")
        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def get_source(self, url: str) -> Optional[Dict[str, Any]]:
        from core.loader import _normalize_url

        row = self.conn.execute(
            "SELECT raw_json FROM sources WHERE source_url = ?",
            (_normalize_url(url),)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None

    def export_sources(self) -> List[Dict[str, Any]]:
        """导出全部书源（保持入库顺序），用于重新生成给 Legado 的 JSON。"""
        out = []
        for row in self.conn.execute(
                "SELECT raw_json FROM sources WHERE deleted_at = '' ORDER BY id"):
            try:
                out.append(json.loads(row["raw_json"]))
            except Exception:
                continue
        return out

    def export_json(self, path: str) -> int:
        from core.loader import dump_json_file

        data = self.export_sources()
        dump_json_file(path, data)
        return len(data)

    def count_sources(self, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) AS c FROM sources"
        if not include_deleted:
            sql += " WHERE deleted_at = ''"
        return self.conn.execute(sql).fetchone()["c"]

    def query(self, source_type: Optional[int] = None, group: str = "",
              health: str = "", q: str = "", only_enabled: bool = False,
              limit: int = 50, offset: int = 0, order: str = "id",
              include_deleted: bool = False) -> List[Dict[str, Any]]:
        """前端列表页用：服务端筛选 + 排序 + 分页（不要全量传给浏览器）。"""
        where, args = self._where(source_type, group, health, q, only_enabled,
                                 include_deleted)
        allowed = ("id", "name", "source_type", "group_name", "stars",
                   "checked_at", "updated_at")
        key = (order or "id").lstrip("-")
        if key not in allowed:
            key, order = "id", "id"
        direction = "DESC" if str(order).startswith("-") else "ASC"
        sql = ("SELECT * FROM v_sources %s ORDER BY %s %s LIMIT ? OFFSET ?"
               % (where, key, direction))
        return [dict(r) for r in self.conn.execute(sql, args + [int(limit), int(offset)])]

    def count_query(self, source_type: Optional[int] = None, group: str = "",
                    health: str = "", q: str = "", only_enabled: bool = False,
                    include_deleted: bool = False) -> int:
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  include_deleted)
        sql = "SELECT COUNT(*) AS c FROM v_sources %s" % where
        return self.conn.execute(sql, args).fetchone()["c"]

    def _where(self, source_type, group, health, q, only_enabled,
               include_deleted: bool = False):
        sql, args = ["WHERE 1=1"], []
        if not include_deleted:
            sql.append("AND deleted_at = ''")
        if source_type is not None:
            sql.append("AND source_type = ?")
            args.append(int(source_type))
        if group:
            sql.append("AND group_name = ?")
            args.append(group)
        if health:
            sql.append("AND health = ?")
            args.append(health)
        if only_enabled:
            sql.append("AND enabled = 1")
        if q:
            sql.append("AND (name LIKE ? OR source_url LIKE ?)")
            args += ["%" + q + "%", "%" + q + "%"]
        return " ".join(sql), args

    def set_group(self, url: str, group: str) -> bool:
        """改分组：同时更新列与 raw_json，保证导出不失真。"""
        from core.loader import _normalize_url

        key = _normalize_url(url)
        row = self.conn.execute(
            "SELECT raw_json FROM sources WHERE source_url = ?", (key,)).fetchone()
        if not row:
            return False
        src = json.loads(row["raw_json"])
        src["bookSourceGroup"] = group
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET group_name = ?, raw_json = ?, updated_at = ? "
                "WHERE source_url = ?",
                (group, json.dumps(src, ensure_ascii=False), now(), key))
        return True

    def delete_sources(self, urls: Sequence[str]) -> int:
        from core.loader import _normalize_url

        keys = [_normalize_url(u) for u in urls or [] if u]
        if not keys:
            return 0
        n = 0
        with self.conn:
            for k in keys:
                cur = self.conn.execute("DELETE FROM sources WHERE source_url = ?", (k,))
                n += cur.rowcount or 0
        return n

    def groups(self) -> List[tuple]:
        return [(r["group_name"], r["c"]) for r in self.conn.execute(
            "SELECT group_name, COUNT(*) AS c FROM sources GROUP BY group_name ORDER BY c DESC")]

    def stats(self) -> Dict[str, Any]:
        c = self.conn.execute
        out: Dict[str, Any] = {"sources": self.count_sources(),
                                "deleted": self.count_deleted()}
        out["checks"] = c("SELECT COUNT(*) AS c FROM checks").fetchone()["c"]
        out["types"] = {str(r["source_type"]): r["c"] for r in c(
            "SELECT source_type, COUNT(*) AS c FROM sources GROUP BY source_type")}
        out["health"] = {str(r["health"]): r["c"] for r in c(
            "SELECT health, COUNT(*) AS c FROM v_sources GROUP BY health")}
        out["stars"] = {str(r["stars"]): r["c"] for r in c(
            "SELECT stars, COUNT(*) AS c FROM v_sources GROUP BY stars")}
        return out

    def backup(self, path: str) -> str:
        """一致性备份（不能用裸拷文件）。"""
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        self.conn.execute("VACUUM INTO ?", (path,))
        return path

    # ---------------------------------------------------------------- checks
    def save_checks(self, rows) -> int:
        # rows 为 check_cache 的 NDJSON 条目（dict）列表
        # 注意：url 必须与 sources.source_url 用同一套规范化，否则 v_sources 关联不上
        from core.loader import _normalize_url

        ts = now()
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            url = _normalize_url(str(r.get("url", "") or ""))
            if not url:
                continue
            tags = r.get("quality_tags")
            if isinstance(tags, list):
                tags = ",".join(str(t) for t in tags)
            out.append((
                url,
                str(r.get("fingerprint", "") or ""),
                int(r.get("v", 0) or 0),
                str(r.get("health", "") or ""),
                r.get("status_code"),
                r.get("response_time_ms"),
                str(r.get("search_hit", "") or ""),
                r.get("search_response_ms"),
                int(r.get("quality_stars", 0) or 0),
                tags or "",
                int(r.get("probe_depth", 1) or 1),
                int(r.get("chapter_count", 0) or 0),
                _tri(r.get("toc_complete")),
                _tri(r.get("content_ok")),
                str(r.get("toc_fail_reason", "") or ""),
                str(r.get("content_fail_reason", "") or ""),
                r.get("content_response_ms"),
                str(r.get("error", "") or ""),
                str(r.get("checked_at", "") or ts),
            ))
        if not out:
            return 0
        sql = (
            "INSERT INTO checks(source_url,fingerprint,cache_version,health,"
            "status_code,response_time_ms,search_hit,search_response_ms,stars,"
            "quality_tags,probe_depth,chapter_count,toc_complete,content_ok,"
            "toc_fail_reason,content_fail_reason,content_response_ms,error,checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        with self.conn:
            self.conn.executemany(sql, out)
        return len(out)

    def last_check(self, url: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM checks WHERE source_url = ? ORDER BY checked_at DESC LIMIT 1",
            (url,)).fetchone()
        return dict(row) if row else None

    def checks_map(self) -> Dict[str, Dict[str, Any]]:
        # 返回 {url: 最近一条缓存}；字段与旧 check_cache 的 NDJSON 完全兼容，
        # 可直接顶替 AsyncChecker.load_cache() 的返回值。
        out: Dict[str, Dict[str, Any]] = {}
        sql = ("SELECT * FROM checks c WHERE c.id = ("
               "SELECT id FROM checks WHERE source_url = c.source_url "
               "ORDER BY checked_at DESC LIMIT 1)")
        for r in self.conn.execute(sql):
            d = dict(r)
            d["url"] = d.pop("source_url", "")
            d["v"] = d.pop("cache_version", 0)
            d["quality_stars"] = d.pop("stars", 0)
            d["quality_tags"] = [t for t in (d.pop("quality_tags", "") or "").split(",") if t]
            d["toc_complete"] = _untri(d.get("toc_complete"))
            d["content_ok"] = _untri(d.get("content_ok"))
            out[d["url"]] = d
        return out

    def count_checks(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM checks").fetchone()["c"]

    # ---------------------------------------------------------------- jobs
    def create_job(self, job_id: str, kind: str, total: int = 0, payload=None) -> None:
        ts = now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO jobs(id,kind,status,progress,total,payload,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, "pending", 0, int(total),
                 json.dumps(payload or {}, ensure_ascii=False), ts, ts))

    def update_job(self, job_id: str, status=None, progress=None, total=None,
                   result=None) -> None:
        sets, args = ["updated_at = ?"], [now()]
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if progress is not None:
            sets.append("progress = ?")
            args.append(int(progress))
        if total is not None:
            sets.append("total = ?")
            args.append(int(total))
        if result is not None:
            sets.append("result_json = ?")
            args.append(json.dumps(result, ensure_ascii=False))
        args.append(job_id)
        with self.conn:
            self.conn.execute("UPDATE jobs SET %s WHERE id = ?" % ", ".join(sets), args)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, kind, status, progress, total, created_at, updated_at"
            " FROM jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))]


    # ---------------------------------------------------------------- exports
    def create_export(self, uid: str, name: str, urls, filename: str,
                      ttl_days: int = 7) -> Dict[str, Any]:
        # 临时导出：uid 唯一，到期由 sweep_exports 清理；pinned=1 永不过期
        ts = now()
        exp = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() + max(1, int(ttl_days)) * 86400))
        with self.conn:
            self.conn.execute(
                "INSERT INTO exports(uid,name,count,urls_json,filename,created_at,"
                "expires_at,hits,pinned) VALUES (?,?,?,?,?,?,?,0,0)",
                (uid, name or "", len(urls or []),
                 json.dumps(urls or [], ensure_ascii=False), filename, ts, exp))
        return {"uid": uid, "expires_at": exp}

    def get_export(self, uid: str, bump: bool = False):
        row = self.conn.execute("SELECT * FROM exports WHERE uid = ?", (uid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if bump:
            with self.conn:
                self.conn.execute("UPDATE exports SET hits = hits + 1 WHERE uid = ?", (uid,))
            d["hits"] = (d.get("hits") or 0) + 1
        d["expired"] = (not d.get("pinned")) and str(d.get("expires_at", "")) < now()
        return d

    def list_exports(self, include_expired: bool = False):
        sql = "SELECT * FROM exports"
        args = ()
        if not include_expired:
            sql += " WHERE pinned = 1 OR expires_at >= ?"
            args = (now(),)
        sql += " ORDER BY created_at DESC LIMIT 200"
        cur = now()
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["expired"] = (not d.get("pinned")) and str(d.get("expires_at", "")) < cur
            out.append(d)
        return out

    def set_export_pinned(self, uid: str, pinned: bool) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE exports SET pinned = ? WHERE uid = ?",
                                    (1 if pinned else 0, uid))
        return bool(cur.rowcount)

    def delete_export(self, uid: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM exports WHERE uid = ?", (uid,))
        return bool(cur.rowcount)

    def sweep_exports(self):
        # 清理过期导出，返回被清掉的 uid（调用方负责删对应文件）
        args = (now(),)
        rows = self.conn.execute(
            "SELECT uid FROM exports WHERE pinned = 0 AND expires_at < ?", args).fetchall()
        uids = [r["uid"] for r in rows]
        if uids:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM exports WHERE pinned = 0 AND expires_at < ?", args)
        return uids


    # ---------------------------------------------------------------- 回收站
    # 设计：UI 永不硬删除。软删时把整条 raw_json 快照到
    # data/backups/deleted_<时间戳>.json，彻底删除由使用者在该文件层面处理。
    def soft_delete(self, urls, reason: str = ""):
        # 返回 (删除条数, 快照路径)
        from core.loader import _normalize_url
        from core.paths import data_path

        keys = [_normalize_url(u) for u in (urls or []) if u]
        if not keys:
            return 0, ""
        marks = ",".join("?" * len(keys))
        rows = list(self.conn.execute(
            "SELECT source_url, raw_json FROM sources "
            "WHERE deleted_at = '' AND source_url IN (%s)" % marks, keys))
        if not rows:
            return 0, ""
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = data_path("backups", "deleted_%s.json" % ts)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "deleted_at": now(),
            "reason": reason or "",
            "count": len(rows),
            "sources": [json.loads(r["raw_json"]) for r in rows],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET deleted_at = ?, updated_at = ? "
                "WHERE deleted_at = '' AND source_url IN (%s)" % marks,
                [now(), now()] + keys)
        return len(rows), path

    def restore(self, urls) -> int:
        from core.loader import _normalize_url

        keys = [_normalize_url(u) for u in (urls or []) if u]
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sources SET deleted_at = '', updated_at = ? "
                "WHERE deleted_at <> '' AND source_url IN (%s)" % marks,
                [now()] + keys)
        return cur.rowcount or 0

    def list_deleted(self, limit: int = 200, offset: int = 0):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, source_url, name, source_type, group_name, deleted_at "
            "FROM sources WHERE deleted_at <> '' "
            "ORDER BY deleted_at DESC LIMIT ? OFFSET ?", (int(limit), int(offset)))]

    def count_deleted(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM sources WHERE deleted_at <> ''").fetchone()["c"]


def _tri(v: Any) -> Optional[int]:
    """三态：None 保持 None，True->1，False->0。"""
    if v is None:
        return None
    return 1 if v else 0


def _untri(v: Any) -> Optional[bool]:
    return None if v is None else bool(v)

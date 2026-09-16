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

from core.tags import (
    SYSTEM_QUALITY_TAGS as _SYSTEM_QUALITY_TAGS,
    SYSTEM_STATUS_TAGS as _SYSTEM_STATUS_TAGS,
    SYSTEM_TYPE_TAGS as _SYSTEM_TYPE_TAGS,
    canonical_tag as _canonical_tag,
    canonical_tags as _canonical_tags,
    extract_user_tags_from_group as _extract_user_tags,
    is_system_tag as _is_system_tag,
    merge_group as _merge_group,
    normalize_tags as _normalize_tags,
    parse_group_tags as _parse_group,
    split_system_user as _split_group,
)

SCHEMA_VERSION = 4
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
        user_tags    TEXT NOT NULL DEFAULT '',
        system_tags_locked INTEGER NOT NULL DEFAULT 0,
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
        search_probed       INTEGER DEFAULT 0,
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
    NEW_COLUMNS = {"sources": [
        ("deleted_at", "TEXT NOT NULL DEFAULT ''"),
        ("user_tags", "TEXT NOT NULL DEFAULT ''"),
        ("system_tags_locked", "INTEGER NOT NULL DEFAULT 0"),
    ], "checks": [
        # 缓存版本 8 起：这条缓存是"带着搜索探测"写下的吗？默认 0 = 没验过搜索，
        # 于是开着搜索探测的用户会把旧缓存整体重验一遍（正是 v8 想要的）。判定见
        # checker.is_cache_item_valid 的 min_search。**不落这一列等于没修**：
        # item 里写了但读回来恒为 None，所有 OK 源的缓存永远不复用
        ("search_probed", "INTEGER DEFAULT 0"),
    ]}

    #: v_sources 视图每次重建：CREATE VIEW IF NOT EXISTS 不会更新已存在的视图定义
    VIEW_DDL = """CREATE VIEW v_sources AS
        SELECT s.id, s.source_url, s.name, s.source_type, s.group_name, s.enabled,
               s.user_tags, s.system_tags_locked, s.fingerprint, s.deleted_at, s.updated_at,
               c.health, c.stars, c.checked_at, c.probe_depth,
               c.toc_complete, c.content_ok, c.search_hit, c.quality_tags
        FROM sources s
        LEFT JOIN checks c ON c.id = (
            SELECT id FROM checks WHERE source_url = s.source_url
            ORDER BY checked_at DESC, id DESC LIMIT 1)"""

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
        self.migrate_user_tags_once()
        self.cleanup_system_tags_once()
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
    def _system_group_for(self, source_type: int, raw_group: str) -> str:
        """按 source_type 重建类型标签，保留健康状态和规则完整标签。"""
        from core.models import BOOK_SOURCE_TYPE_NAMES
        from core.organizer import group_title, infer_health_from_group

        system, _user = _split_group(raw_group)
        # 兜底口径同 models.type_name：未定义类型（如 4）留空，由 _merge_group 丢弃空段
        type_tag = BOOK_SOURCE_TYPE_NAMES.get(int(source_type), "")
        status_tags = [t for t in system if t in _SYSTEM_STATUS_TAGS]
        quality_tags = [t for t in system if t in _SYSTEM_QUALITY_TAGS]
        has_old_type = any(t in _SYSTEM_TYPE_TAGS for t in system)
        if has_old_type or status_tags:
            # 已有显式系统标签：以表单类型为准，保留状态/规则完整
            base = [type_tag] + status_tags[:1]
        else:
            # 没有系统类型/状态时，按旧分组推断健康度
            base = _parse_group(group_title(int(source_type),
                                            infer_health_from_group(raw_group)))
        return _merge_group(base, quality_tags)

    def _source_view(self, raw_json: str, group_name: str, user_tags: str):
        """把 raw_json、系统标签、用户标签合并成对外的书源对象。"""
        try:
            src = json.loads(raw_json)
        except Exception:
            return None
        src["bookSourceGroup"] = _merge_group(_parse_group(group_name), _normalize_tags(user_tags))
        return src

    def _known_user_tags(self) -> set:
        """当前库里已经存在的用户标签集合，用于过滤新源标签。"""
        out = set()
        for row in self.conn.execute(
                "SELECT user_tags FROM sources WHERE deleted_at = ''"):
            out.update(t for t in _normalize_tags(row["user_tags"]) if not _is_system_tag(t))
        return out

    def upsert_sources(self, sources, with_fingerprint: bool = True, allow_new_tags: bool = False) -> int:
        """批量写入/更新书源。同 URL 更新规则和系统标签，用户标签永久保留。"""
        from core.loader import _normalize_url, fingerprint as fp_of

        ts = now()
        rows = []
        known_tags = self._known_user_tags()
        allow_unknown = allow_new_tags or self.count_sources(include_deleted=True) == 0
        for src in sources or []:
            if not isinstance(src, dict):
                continue
            url = _normalize_url(str(src.get("bookSourceUrl", "") or ""))
            if not url:
                continue
            source_type = int(src.get("bookSourceType", 0) or 0)
            raw_group = str(src.get("bookSourceGroup", "") or "")
            system_group = self._system_group_for(source_type, raw_group)
            incoming_tags = _extract_user_tags(raw_group)
            if allow_unknown:
                selected_tags = incoming_tags
                known_tags.update(incoming_tags)
            else:
                selected_tags = [t for t in incoming_tags if t in known_tags]
            user_tags = _merge_group([], selected_tags)
            rows.append((
                url,
                str(src.get("bookSourceName", "") or ""),
                source_type,
                system_group,
                user_tags,
                1 if src.get("enabled", True) else 0,
                json.dumps(src, ensure_ascii=False),
                fp_of(src) if with_fingerprint else "",
                ts, ts,
            ))
        if not rows:
            return 0
        sql = (
            "INSERT INTO sources(source_url,name,source_type,group_name,user_tags,enabled,"
            "raw_json,fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_url) DO UPDATE SET name=excluded.name, "
            "source_type=excluded.source_type, "
            "group_name=CASE WHEN system_tags_locked=1 THEN group_name "
            "ELSE excluded.group_name END, "
            "enabled=excluded.enabled, raw_json=excluded.raw_json, "
            "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at")
        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def get_source(self, url: str) -> Optional[Dict[str, Any]]:
        from core.loader import _normalize_url

        row = self.conn.execute(
            "SELECT raw_json, group_name, user_tags FROM sources WHERE source_url = ?",
            (_normalize_url(url),)).fetchone()
        if not row:
            return None
        return self._source_view(row["raw_json"], row["group_name"], row["user_tags"])

    def get_source_fingerprint(self, url: str):
        """返回 (fingerprint, is_deleted)，供导入比对。

        fingerprint 为 None 表示 URL 不存在；is_deleted 表示该 URL 是否在回收站。
        """
        from core.loader import _normalize_url

        row = self.conn.execute(
            "SELECT fingerprint, deleted_at FROM sources WHERE source_url = ?",
            (_normalize_url(url),)).fetchone()
        if not row:
            return None, False
        fp = str(row["fingerprint"] or "").strip()
        return (fp or None), bool(row["deleted_at"])

    def export_sources(self) -> List[Dict[str, Any]]:
        """导出全部书源（保持入库顺序），用于重新生成给 Legado 的 JSON。"""
        out = []
        for row in self.conn.execute(
                "SELECT raw_json, group_name, user_tags "
                "FROM sources WHERE deleted_at = '' ORDER BY id"):
            src = self._source_view(row["raw_json"], row["group_name"], row["user_tags"])
            if src:
                out.append(src)
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
              user_tag: str = "",
              limit: int = 50, offset: int = 0, order: str = "id",
              include_deleted: bool = False) -> List[Dict[str, Any]]:
        """前端列表页用：服务端筛选 + 排序 + 分页（不要全量传给浏览器）。"""
        where, args = self._where(source_type, group, health, q, only_enabled,
                                 include_deleted, user_tag)
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
                    include_deleted: bool = False, user_tag: str = "") -> int:
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  include_deleted, user_tag)
        sql = "SELECT COUNT(*) AS c FROM v_sources %s" % where
        return self.conn.execute(sql, args).fetchone()["c"]

    def _where(self, source_type, group, health, q, only_enabled,
               include_deleted: bool = False, user_tag: str = ""):
        sql, args = ["WHERE 1=1"], []
        if not include_deleted:
            sql.append("AND deleted_at = ''")
        if source_type is not None:
            sql.append("AND source_type = ?")
            args.append(int(source_type))
        if group:
            sql.append("AND group_name = ?")
            args.append(group)
        if user_tag:
            sql.append("AND (',' || user_tags || ',') LIKE ?")
            args.append("%," + user_tag + ",%")
        if health:
            if health == "none":
                # 「未校验」= checks 表里没有对应行，LEFT JOIN 出来是 NULL。
                # 等值过滤（health = ?）表达不出这个条件——传 "none" 会变成
                # `health = 'none'`，一个恒空的查询。
                sql.append("AND health IS NULL")
            else:
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

    # ------------------------------------------------------------ tags
    def migrate_user_tags_once(self) -> bool:
        """把旧 group_name / raw_json 分组拆成系统标签和用户标签。"""
        if self.get_meta("user_tags_migrated_at"):
            return False
        rows = list(self.conn.execute(
            "SELECT id, source_type, group_name, raw_json FROM sources"))
        if not rows:
            self.set_meta("user_tags_migrated_at", now())
            return False
        from core.organizer import group_title, infer_health_from_group
        updates = []
        for row in rows:
            try:
                raw_group = str(json.loads(row["raw_json"]).get("bookSourceGroup", "") or "")
            except Exception:
                raw_group = ""
            old_group = str(row["group_name"] or "")
            system, user = _split_group(_parse_group(raw_group) + _parse_group(old_group))
            if system:
                system_group = _merge_group(system, [])
            else:
                health_group = raw_group or old_group
                system_group = group_title(row["source_type"], infer_health_from_group(health_group))
            updates.append((system_group, _merge_group([], _canonical_tags(user)), row["id"]))
        with self.conn:
            self.conn.executemany(
                "UPDATE sources SET group_name=?, user_tags=? WHERE id=?", updates)
        self.set_meta("user_tags_migrated_at", now())
        return True

    def cleanup_system_tags_once(self) -> bool:
        """Once-off cleanup: remove system tags that leaked into user_tags."""
        if self.get_meta("system_tags_cleaned_at"):
            return False
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources"))
        updates = []
        for row in rows:
            old = row["user_tags"] or ""
            new = _merge_group(
                [], [t for t in _canonical_tags(old) if not _is_system_tag(t)])
            if new != old:
                updates.append((new, now(), row["source_url"]))
        if updates:
            with self.conn:
                self.conn.executemany(
                    "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                    updates)
        self.set_meta("system_tags_cleaned_at", now())
        return bool(updates)

    def is_system_tags_locked(self, url: str) -> bool:
        from core.loader import _normalize_url
        row = self.conn.execute(
            "SELECT system_tags_locked FROM sources WHERE source_url = ?",
            (_normalize_url(url),)).fetchone()
        return bool(row and row["system_tags_locked"])

    def set_system_tags_override(self, urls, tags) -> int:
        """把系统标签设为人工校正结果，并锁定，后续 rebuild 不覆盖。"""
        value = _merge_group(
            [], [t for t in _canonical_tags(tags) if _is_system_tag(t)])
        keys = self._tag_urls(urls)
        if not value or not keys:
            return 0
        n = 0
        with self.conn:
            for key in keys:
                cur = self.conn.execute(
                    "UPDATE sources SET group_name=?, system_tags_locked=1, updated_at=? "
                    "WHERE source_url=?", (value, now(), key))
                n += cur.rowcount or 0
        return n

    def clear_system_tags_override(self, urls) -> int:
        """解除人工锁定，并按最近校验结果重建系统标签。"""
        keys = self._tag_urls(urls)
        if not keys:
            return 0
        n = 0
        with self.conn:
            for key in keys:
                cur = self.conn.execute(
                    "UPDATE sources SET system_tags_locked=0 WHERE source_url=?", (key,))
                n += cur.rowcount or 0
        self.rebuild_system_tags(keys)
        return n

    def _tag_urls(self, urls):
        from core.loader import _normalize_url
        return [_normalize_url(u) for u in (urls or []) if u]

    def add_user_tags(self, urls, tags) -> int:
        """批量追加用户标签；系统标签会被忽略。"""
        add = [t for t in _canonical_tags(tags) if not _is_system_tag(t)]
        keys = self._tag_urls(urls)
        if not add or not keys:
            return 0
        n = 0
        with self.conn:
            for key in keys:
                row = self.conn.execute(
                    "SELECT user_tags FROM sources WHERE source_url = ?", (key,)).fetchone()
                if not row:
                    continue
                current = _normalize_tags(row["user_tags"])
                merged = _merge_group(current, add)
                if merged != (row["user_tags"] or ""):
                    self.conn.execute(
                        "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                        (merged, now(), key))
                    n += 1
        return n

    def set_user_tags(self, urls, tags) -> int:
        """覆盖一组源的完整用户标签集合。"""
        value = _merge_group([], [t for t in _canonical_tags(tags) if not _is_system_tag(t)])
        keys = self._tag_urls(urls)
        if not keys:
            return 0
        n = 0
        with self.conn:
            for key in keys:
                cur = self.conn.execute(
                    "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                    (value, now(), key))
                n += cur.rowcount or 0
        return n

    def remove_user_tags(self, urls, tags) -> int:
        """批量移除用户标签。"""
        remove = set(_canonical_tags(tags))
        keys = self._tag_urls(urls)
        if not remove or not keys:
            return 0
        n = 0
        with self.conn:
            for key in keys:
                row = self.conn.execute(
                    "SELECT user_tags FROM sources WHERE source_url = ?", (key,)).fetchone()
                if not row:
                    continue
                current = _normalize_tags(row["user_tags"])
                merged = _merge_group([], [t for t in current if t not in remove])
                if merged != (row["user_tags"] or ""):
                    self.conn.execute(
                        "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                        (merged, now(), key))
                    n += 1
        return n

    def rename_user_tag(self, old: str, new: str) -> int:
        """全局重命名一个用户标签。"""
        old_tags = _canonical_tags(old)
        new_tags = [t for t in _canonical_tags(new) if not _is_system_tag(t)]
        if len(old_tags) != 1 or len(new_tags) != 1:
            return 0
        old_tag, new_tag = old_tags[0], new_tags[0]
        if _is_system_tag(old_tag):
            return 0
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources WHERE user_tags LIKE ?",
            ("%" + old_tag + "%",)))
        n = 0
        with self.conn:
            for row in rows:
                current = _normalize_tags(row["user_tags"])
                merged = _merge_group([], [new_tag if t == old_tag else t for t in current])
                if merged != (row["user_tags"] or ""):
                    self.conn.execute(
                        "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                        (merged, now(), row["source_url"]))
                    n += 1
        return n

    def merge_user_tags(self, sources, target: str) -> int:
        """把多个用户标签合并成 target。"""
        src_tags = [t for t in _canonical_tags(sources) if not _is_system_tag(t)]
        dst_tags = [t for t in _canonical_tags(target) if not _is_system_tag(t)]
        if not src_tags or len(dst_tags) != 1:
            return 0
        target_tag = dst_tags[0]
        rows = list(self.conn.execute("SELECT source_url, user_tags FROM sources"))
        n = 0
        with self.conn:
            for row in rows:
                current = _normalize_tags(row["user_tags"])
                if not any(t in src_tags for t in current):
                    continue
                merged = _merge_group([], [target_tag if t in src_tags else t for t in current])
                if merged != (row["user_tags"] or ""):
                    self.conn.execute(
                        "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                        (merged, now(), row["source_url"]))
                    n += 1
        return n

    def delete_user_tag(self, tag: str) -> int:
        """从所有源移除一个用户标签。"""
        tags = _canonical_tags(tag)
        if len(tags) != 1 or _is_system_tag(tags[0]):
            return 0
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources"))
        n = 0
        with self.conn:
            for row in rows:
                current = _normalize_tags(row["user_tags"])
                if tags[0] not in current:
                    continue
                merged = _merge_group([], [t for t in current if t != tags[0]])
                self.conn.execute(
                    "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                    (merged, now(), row["source_url"]))
                n += 1
        return n

    def normalize_user_tags(self) -> int:
        """全库用户标签规范化：trim、统一分隔、去重、保序。"""
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources"))
        n = 0
        with self.conn:
            for row in rows:
                old = row["user_tags"] or ""
                new = _merge_group(
                    [], [t for t in _canonical_tags(old) if not _is_system_tag(t)])
                if new != old:
                    self.conn.execute(
                        "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url=?",
                        (new, now(), row["source_url"]))
                    n += 1
        return n

    def tags_overview(self) -> List[Dict[str, Any]]:
        """返回所有系统/用户标签及计数。"""
        system_counts: Dict[str, int] = {}
        user_counts: Dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT group_name, user_tags FROM sources WHERE deleted_at = ''"):
            for tag in _parse_group(row["group_name"]):
                if _is_system_tag(tag):
                    system_counts[tag] = system_counts.get(tag, 0) + 1
            for tag in _normalize_tags(row["user_tags"]):
                if _is_system_tag(tag):
                    continue
                user_counts[tag] = user_counts.get(tag, 0) + 1
        out: List[Dict[str, Any]] = []
        for tag, count in system_counts.items():
            out.append({"tag": tag, "count": count, "kind": "system", "editable": False})
        for tag, count in user_counts.items():
            out.append({"tag": tag, "count": count, "kind": "user", "editable": True})
        out.sort(key=lambda x: (0 if x["kind"] == "system" else 1, -x["count"], x["tag"]))
        return out

    def rebuild_system_tags(self, urls=None) -> int:
        """按类型 + 最近校验结果 + 规则完整度重建系统标签；锁定行跳过。"""
        from core.models import Health
        from core.organizer import group_title
        where, args = "", []
        if urls is not None:
            keys = self._tag_urls(urls)
            if not keys:
                return 0
            where = " WHERE v.source_url IN (%s)" % ",".join("?" * len(keys))
            args = keys
        rows = list(self.conn.execute(
            "SELECT v.id, v.source_type, v.group_name, v.health, v.stars, "
            "v.quality_tags, v.system_tags_locked "
            "FROM v_sources v" + where, args))
        updates = []
        for row in rows:
            if row["system_tags_locked"]:
                continue
            health = row["health"] or Health.SKIPPED
            base = group_title(int(row["source_type"] or 0), health, int(row["stars"] or 0))
            quality = _normalize_tags(row["quality_tags"] or "")
            if "规则完整" in quality:
                base = _merge_group(_parse_group(base), ["规则完整"])
            if base != (row["group_name"] or ""):
                updates.append((base, row["id"]))
        if not updates:
            return 0
        with self.conn:
            self.conn.executemany("UPDATE sources SET group_name=? WHERE id=?", updates)
        return len(updates)

    def stats(self) -> Dict[str, Any]:
        c = self.conn.execute
        out: Dict[str, Any] = {"sources": self.count_sources(),
                                "deleted": self.count_deleted()}
        out["checks"] = c("SELECT COUNT(*) AS c FROM checks").fetchone()["c"]
        # 分布统计必须与 count_sources() 同口径（都只看未删除的源）。
        # 不过滤的话 chip 相加会比「源总数」多出回收站里那些——用户一眼就会看到对不上。
        out["types"] = {str(r["source_type"]): r["c"] for r in c(
            "SELECT source_type, COUNT(*) AS c FROM sources "
            "WHERE deleted_at = '' GROUP BY source_type")}
        out["health"] = {str(r["health"]): r["c"] for r in c(
            "SELECT health, COUNT(*) AS c FROM v_sources "
            "WHERE deleted_at = '' GROUP BY health")}
        out["stars"] = {str(r["stars"]): r["c"] for r in c(
            "SELECT stars, COUNT(*) AS c FROM v_sources "
            "WHERE deleted_at = '' GROUP BY stars")}
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
                # 缺失按 0 处理（= 没验过搜索）：方向是保守重验，不是错误复用
                1 if r.get("search_probed") else 0,
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
            "status_code,response_time_ms,search_hit,search_response_ms,search_probed,"
            "stars,quality_tags,probe_depth,chapter_count,toc_complete,content_ok,"
            "toc_fail_reason,content_fail_reason,content_response_ms,error,checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        with self.conn:
            self.conn.executemany(sql, out)
        return len(out)

    def last_check(self, url: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            # id DESC 的兜底理由同 checks_map()
            "SELECT * FROM checks WHERE source_url = ? "
            "ORDER BY checked_at DESC, id DESC LIMIT 1",
            (url,)).fetchone()
        return dict(row) if row else None

    def checks_map(self) -> Dict[str, Dict[str, Any]]:
        # 返回 {url: 最近一条缓存}；字段与旧 check_cache 的 NDJSON 完全兼容，
        # 可直接顶替 AsyncChecker.load_cache() 的返回值。
        out: Dict[str, Dict[str, Any]] = {}
        # id DESC 是**必须的兜底**：checked_at 只到秒，同一秒里写了两条时，
        # 只按时间排的话顺序不确定（实测走索引返回先写入的那条），「最新结论」
        # 会变成上一轮的——列表显示旧 health，而缓存判定会拿旧的 search_probed
        # 判成「没验过搜索」，每次校验都白打请求
        sql = ("SELECT * FROM checks c WHERE c.id = ("
               "SELECT id FROM checks WHERE source_url = c.source_url "
               "ORDER BY checked_at DESC, id DESC LIMIT 1)")
        for r in self.conn.execute(sql):
            d = dict(r)
            d["url"] = d.pop("source_url", "")
            d["v"] = d.pop("cache_version", 0)
            d["quality_stars"] = d.pop("stars", 0)
            d["quality_tags"] = [t for t in (d.pop("quality_tags", "") or "").split(",") if t]
            d["toc_complete"] = _untri(d.get("toc_complete"))
            d["content_ok"] = _untri(d.get("content_ok"))
            # 库里存的是 0/1，转成 bool 与 ndjson 后端返回同样的类型。
            # 老库没有这一列时（补列前落下的行）默认 0 → 保守重验，方向安全
            d["search_probed"] = bool(d.get("search_probed"))
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


    def export_by_filter(self, source_type=None, group: str = "", health: str = "",
                         q: str = "", only_enabled: bool = False, user_tag: str = ""):
        # 按筛选条件导出全部命中源（不分页）。
        # 走 v_sources 视图，这样 health 等只有视图才有的列也能筛。
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  user_tag=user_tag)
        # 不能 JOIN sources：两表都有 deleted_at/name/source_url 等列，
        # _where 生成的是不带表名的条件，SQLite 会报 ambiguous column name。
        # 用子查询取 raw_json，_where 里的列在 v_sources 里全都有。
        sql = ("SELECT (SELECT raw_json FROM sources WHERE id = v.id) AS raw_json, "
               "v.group_name, v.user_tags "
               "FROM v_sources v %s ORDER BY v.id" % where)
        out, bad = [], 0
        for row in self.conn.execute(sql, args):
            src = self._source_view(row["raw_json"], row["group_name"], row["user_tags"])
            if not src:
                bad += 1
                continue
            out.append(src)
        if bad:
            print("WARN export_by_filter: %d/%d 条 raw_json 缺失或无法解析，已跳过"
                  % (bad, bad + len(out)))
        return out

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
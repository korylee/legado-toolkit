# -*- coding: utf-8 -*-
"""外部书源导入批次与待审冲突管理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from core.loader import _normalize_url, dump_json_file, fingerprint, load_json_file
from core.sanitize import clean_source


@dataclass(frozen=True)
class ImportSummary:
    """一次外部书源导入的分类统计。"""

    batch_id: int
    new_count: int
    duplicate_count: int
    conflict_count: int
    pending_count: int


@dataclass(frozen=True)
class ReviewItem:
    """等待人工确认的同 URL 规则冲突。"""

    url: str
    batch_id: int
    candidate_fingerprint: str
    incoming_fingerprint: str
    created_at: str


@dataclass(frozen=True)
class PendingSource:
    """等待校验并人工批准的新 URL 书源。"""

    url: str
    batch_id: int
    created_at: str


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect(registry_path: str) -> sqlite3.Connection:
    Path(registry_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(registry_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incoming_path TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            normalized_url TEXT NOT NULL,
            incoming_fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES import_batches(id)
        );

        CREATE TABLE IF NOT EXISTS pending_sources (
            normalized_url TEXT PRIMARY KEY,
            source_json TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            batch_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES import_batches(id)
        );

        CREATE TABLE IF NOT EXISTS review_items (
            normalized_url TEXT PRIMARY KEY,
            candidate_json TEXT NOT NULL,
            incoming_json TEXT NOT NULL,
            candidate_fingerprint TEXT NOT NULL,
            incoming_fingerprint TEXT NOT NULL,
            batch_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(batch_id) REFERENCES import_batches(id)
        );
        """
    )
    return connection


@contextmanager
def _open_registry(registry_path: str):
    """打开注册表事务，并确保 Windows 上也会及时释放数据库文件句柄。"""
    connection = _connect(registry_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_sources(path: str) -> list[dict[str, Any]]:
    data = load_json_file(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} 不是书源列表（应为 JSON 数组）")
    sources: list[dict[str, Any]] = []
    for index, source in enumerate(data):
        if not isinstance(source, dict):
            raise ValueError(f"{path} 第 {index + 1} 项不是书源对象")
        sources.append(source)
    return sources


def _copy_raw_file(incoming_path: str, raw_dir: str) -> str:
    destination_dir = Path(raw_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(incoming_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = destination_dir / f"{timestamp}_{source_path.name}"
    suffix = 1
    while destination.exists():
        destination = destination_dir / f"{timestamp}_{suffix}_{source_path.name}"
        suffix += 1
    shutil.copy2(source_path, destination)
    return str(destination)


def import_sources(
    candidate_path: str,
    incoming_path: str,
    registry_path: str,
    raw_dir: str,
) -> ImportSummary:
    """导入外部文件，仅建立待校验或待审记录，不修改候选书源文件。"""
    candidates = _read_sources(candidate_path)
    incoming_sources = _read_sources(incoming_path)
    candidate_by_url = {
        _normalize_url(str(source.get("bookSourceUrl", "") or "")): source
        for source in candidates
        if _normalize_url(str(source.get("bookSourceUrl", "") or ""))
    }
    cleaned_incoming: list[dict[str, Any]] = []
    for source in incoming_sources:
        cleaned_source = dict(source)
        clean_source(cleaned_source)
        cleaned_incoming.append(cleaned_source)

    raw_path = _copy_raw_file(incoming_path, raw_dir)
    now = _now_text()
    new_count = 0
    duplicate_count = 0
    conflict_count = 0

    with _open_registry(registry_path) as connection:
        cursor = connection.execute(
            "INSERT INTO import_batches(incoming_path, raw_path, imported_at) VALUES (?, ?, ?)",
            (os.path.abspath(incoming_path), raw_path, now),
        )
        batch_id = int(cursor.lastrowid)

        for source in cleaned_incoming:
            normalized_url = _normalize_url(str(source.get("bookSourceUrl", "") or ""))
            if not normalized_url:
                raise ValueError("外部书源缺少 bookSourceUrl，无法导入")
            incoming_fingerprint = fingerprint(source)
            candidate = candidate_by_url.get(normalized_url)
            if candidate is None:
                new_count += 1
                decision = "pending_check"
                connection.execute(
                    """
                    INSERT INTO pending_sources(
                        normalized_url, source_json, source_fingerprint, batch_id, state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(normalized_url) DO UPDATE SET
                        source_json=excluded.source_json,
                        source_fingerprint=excluded.source_fingerprint,
                        batch_id=excluded.batch_id,
                        state=excluded.state,
                        updated_at=excluded.updated_at
                    """,
                    (normalized_url, json.dumps(source, ensure_ascii=False), incoming_fingerprint,
                     batch_id, "pending_check", now),
                )
            else:
                candidate_fingerprint = fingerprint(candidate)
                if candidate_fingerprint == incoming_fingerprint:
                    duplicate_count += 1
                    decision = "duplicate"
                else:
                    conflict_count += 1
                    decision = "review"
                    connection.execute(
                        """
                        INSERT INTO review_items(
                            normalized_url, candidate_json, incoming_json,
                            candidate_fingerprint, incoming_fingerprint,
                            batch_id, state, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(normalized_url) DO UPDATE SET
                            candidate_json=excluded.candidate_json,
                            incoming_json=excluded.incoming_json,
                            candidate_fingerprint=excluded.candidate_fingerprint,
                            incoming_fingerprint=excluded.incoming_fingerprint,
                            batch_id=excluded.batch_id,
                            state=excluded.state,
                            created_at=excluded.created_at,
                            reviewed_at=NULL
                        """,
                        (normalized_url, json.dumps(candidate, ensure_ascii=False),
                         json.dumps(source, ensure_ascii=False), candidate_fingerprint,
                         incoming_fingerprint, batch_id, "pending", now),
                    )
            connection.execute(
                """
                INSERT INTO import_entries(
                    batch_id, normalized_url, incoming_fingerprint, decision, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (batch_id, normalized_url, incoming_fingerprint, decision, now),
            )

    return ImportSummary(
        batch_id=batch_id,
        new_count=new_count,
        duplicate_count=duplicate_count,
        conflict_count=conflict_count,
        pending_count=new_count,
    )


def list_pending_reviews(registry_path: str) -> list[ReviewItem]:
    """返回当前仍待人工审批的规则冲突。"""
    with _open_registry(registry_path) as connection:
        rows = connection.execute(
            """
            SELECT normalized_url, batch_id, candidate_fingerprint,
                   incoming_fingerprint, created_at
            FROM review_items
            WHERE state = 'pending'
            ORDER BY created_at, normalized_url
            """
        ).fetchall()
    return [
        ReviewItem(
            url=str(row["normalized_url"]),
            batch_id=int(row["batch_id"]),
            candidate_fingerprint=str(row["candidate_fingerprint"]),
            incoming_fingerprint=str(row["incoming_fingerprint"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


def list_pending_sources(registry_path: str) -> list[PendingSource]:
    """返回尚未写入候选库的新 URL 书源。"""
    with _open_registry(registry_path) as connection:
        rows = connection.execute(
            """
            SELECT normalized_url, batch_id, updated_at
            FROM pending_sources
            WHERE state = 'pending_check'
            ORDER BY updated_at, normalized_url
            """
        ).fetchall()
    return [
        PendingSource(
            url=str(row["normalized_url"]),
            batch_id=int(row["batch_id"]),
            created_at=str(row["updated_at"]),
        )
        for row in rows
    ]


def approve_review(registry_path: str, candidate_path: str, url: str) -> bool:
    """批准一条待审规则，以其外部版本精确替换候选库的同 URL 条目。"""
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return False
    with _open_registry(registry_path) as connection:
        row = connection.execute(
            """
            SELECT incoming_json FROM review_items
            WHERE normalized_url = ? AND state = 'pending'
            """,
            (normalized_url,),
        ).fetchone()
        if row is None:
            return False
        incoming_source = json.loads(str(row["incoming_json"]))
        candidates = _read_sources(candidate_path)
        replacement_index = next(
            (
                index for index, source in enumerate(candidates)
                if _normalize_url(str(source.get("bookSourceUrl", "") or "")) == normalized_url
            ),
            None,
        )
        if replacement_index is None:
            return False
        candidates[replacement_index] = incoming_source
        dump_json_file(candidate_path, candidates)
        connection.execute(
            "UPDATE review_items SET state = 'approved', reviewed_at = ? WHERE normalized_url = ?",
            (_now_text(), normalized_url),
        )
    return True


def approve_pending_source(registry_path: str, candidate_path: str, url: str) -> bool:
    """在外部校验通过后，将待校验新源加入候选库。"""
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return False
    with _open_registry(registry_path) as connection:
        row = connection.execute(
            """
            SELECT source_json FROM pending_sources
            WHERE normalized_url = ? AND state = 'pending_check'
            """,
            (normalized_url,),
        ).fetchone()
        if row is None:
            return False
        candidates = _read_sources(candidate_path)
        if any(
            _normalize_url(str(source.get("bookSourceUrl", "") or "")) == normalized_url
            for source in candidates
        ):
            return False
        candidates.append(json.loads(str(row["source_json"])))
        dump_json_file(candidate_path, candidates)
        connection.execute(
            "UPDATE pending_sources SET state = 'approved', updated_at = ? WHERE normalized_url = ?",
            (_now_text(), normalized_url),
        )
    return True

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
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from core.checker import CACHE_VERSION

from core.tags import (
    DEFAULT_USER_TAGS as _DEFAULT_USER_TAGS,
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

SCHEMA_VERSION = 6

#: 本进程内已经建过 schema 的库（绝对路径）。
#:
#: **建 schema 是进程级的一次性动作，不是连接级的**。Web 端每个请求都会新建一个
#: Store，而 `_init_schema` 里有 `DROP VIEW v_sources` + `CREATE VIEW`
#: （视图定义要重建才会更新，见 VIEW_DDL 的注释）——每次请求都跑的话，并发下几个
#: 连接会互相把视图删掉再建，实测表现是「view v_sources already exists」与
#: 「no such table: v_sources」交替出现，前端随机 500（20 并发复现 1 次）。
#:
#: 那几条 `*_once` 的存量修复也在 `_init_schema` 里，所以它们的口径也随之变成
#: 「**每个进程**首次打开这个库时跑一次」。加新修复时按这个口径想：进程级幂等。
#:
#: 只协调本进程：CLI 与 Web 同时开着本来就该避开同时写，而 busy_timeout 只解决
#: 锁等待，解决不了 DDL 交错。
_SCHEMA_READY: set = set()
_SCHEMA_LOCK = threading.Lock()

#: 任务保留天数。过期由 `Store.sweep_jobs` 清理，对齐 exports 的 ttl_days=7
JOBS_TTL_DAYS = 7

#: 非终态（还占着执行槽）的任务状态。**唯一一份**：任务列表的「最近 N 天」窗口
#: 只筛终态行（在跑的任务不管什么时候建的都要能看到），而「进行中」档位也是这组值——
#: 各写一份就会漂成「档位含 cancel_requested、窗口不含」。
ACTIVE_JOB_STATUSES = ("pending", "running", "cancel_requested")

#: 任务的**轻量行**：状态/进度 + 从结果里抽出来的几个标量。
#:
#: 为什么不把 ``result_json`` 取出来再在 Python 里解析：列表最多 200 行，而一次全量
#: 校验的结果带着 ``items[:500]``（一条上百 KB）——为了几个数把几十 MB 的 JSON 都解析
#: 一遍，打开任务中心、每 5 秒的刷新都要付一次。``json_extract`` 让 SQLite 只把需要的
#: 标量交出来；``result_changed`` 是 ``{档位: 条数}`` 那个小对象（只有它还需要求和）。
#:
#: 列表端点、单条轮询端点共用这一份；**详情**仍走 ``get_job``（它需要完整结果）。
#:
#: ``json_valid`` 那道守卫**不能省**：``json_extract`` 遇到无效 JSON 会直接抛
#: ``malformed JSON``，一条写坏的结果就把整张任务列表带成 500（Python 侧解析时
#: 是 try/except 兜住的，搬到 SQL 就必须显式补上）。
_VALID_RESULT = "CASE WHEN json_valid(result_json) THEN result_json END"
#: ``payload`` 同样要过这道闸门（理由同上）：提交时冻结的清单里带着运行目录，
#: 抽它的代价本该是"零解析"，一条坏 JSON 不能把整张任务列表带成 500。
_VALID_PAYLOAD = "CASE WHEN json_valid(payload) THEN payload END"

_JOB_LIGHT_COLUMNS = (
    "id, kind, status, phase, progress, total, retry_of, expires_at,"
    " created_at, updated_at,"
    " json_extract(%s, '$.checked') AS result_checked,"
    " json_extract(%s, '$.dist.ok') AS result_ok,"
    " json_extract(%s, '$.transitions.changed') AS result_changed,"
    " json_extract(%s, '$.error') AS result_error,"
    # 中止那条链的原因在 `reason`（`error` 留给异常出口）：行里要能显示它，否则一次
    # 中止在列表上就是「已完成 + 空原因」。`ok` 是「只在明确为假时才取 reason」那道
    # 闸门（JSON 的 false 经 json_extract 出来是 0）。
    " json_extract(%s, '$.ok') AS result_ok_flag,"
    " json_extract(%s, '$.reason') AS result_reason,"
    # 运行目录：跑批放在 `payload.manifest.run_dir`、调试 job 放在 `payload.run_dir`
    # （两处都是提交时冻结的）。SQL 侧一次抽好——观测端不该为了一个路径去解析
    # 上百 KB 的 result_json。
    " COALESCE(json_extract(%s, '$.run_dir'),"
    " json_extract(%s, '$.manifest.run_dir')) AS result_run_dir"
    % ((_VALID_RESULT,) * 6 + (_VALID_PAYLOAD,) * 2)
)
DB_NAME = "sources.sqlite3"

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
)

#: `sources` 的表结构。**只有这一份**：建表（`DDL` 列表）直接使用它，
#: source_url 唯一性由当前索引表达，差别见 AGENTS #9。
SOURCES_DDL = """CREATE TABLE IF NOT EXISTS sources (
        id           INTEGER PRIMARY KEY,
        source_url   TEXT NOT NULL,
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
    )"""

DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    SOURCES_DDL,
    "CREATE INDEX IF NOT EXISTS idx_sources_type  ON sources(source_type)",
    "CREATE INDEX IF NOT EXISTS idx_sources_group ON sources(group_name)",
    "CREATE INDEX IF NOT EXISTS idx_sources_name  ON sources(name)",
    # 在用 URL 的唯一性由部分索引保证，回收站可保留历史版本；规则见 AGENTS #9。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_live_url "
    "ON sources(source_url) WHERE deleted_at = ''",
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
        retry_of    TEXT NOT NULL DEFAULT '',
        expires_at  TEXT NOT NULL DEFAULT '',
        pinned      INTEGER NOT NULL DEFAULT 0,
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


def _latest_check_id_sql(correlate: str = "") -> str:
    """「每源最新一条（当前缓存版本）」的 id——**四处共用这一份判据**。

    谁在用：`VIEW_DDL`（外层别名 `s`）、`checks_map`（`c`）、`sweep_checks`（`c`）、
    `last_check`（独立形态，不给别名）。**必须共用**：口径一旦不一致，`sweep_checks`
    会删掉 `checks_map` 正要返回的那一行——表现是列表健康度莫名回退，且不报错。

    `id DESC` 是必须的兜底：`checked_at` 只到秒，同一秒里写了两条时只按时间排的顺序
    不确定（实测走索引返回先写入的那条），「最新结论」会变成上一轮的——列表显示旧
    health，而缓存判定拿旧的 `search_probed` 判成「没验过搜索」，每次校验都白打请求。
    """
    source_url = ("%s.source_url" % correlate) if correlate else "?"
    return ("SELECT id FROM checks WHERE source_url = %s AND cache_version = %d"
            " ORDER BY checked_at DESC, id DESC LIMIT 1" % (source_url, CACHE_VERSION))


class Store:
    """SQLite 管理库。支持 with 语句。"""

    def __init__(self, path: Optional[str] = None, readonly: bool = False):
        self.path = resolve_db_path(path)
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not readonly:
            os.makedirs(d, exist_ok=True)
        # `check_same_thread=False` **必须给**：FastAPI 的 sync 依赖（`get_store`）
        # 与 sync 端点各自向 anyio 线程池要线程，**不保证是同一个 worker**——
        # 于是「依赖里建连接、端点里用连接」就会撞上 sqlite3 的默认保守检查，
        # 抛 `ProgrammingError: SQLite objects created in a thread can only be
        # used in that same thread`，表现是随机 500。只读/串行请求永远不复现，
        # 只有并发到线程池扩容时才出——正是最难查的那类（实测 20 并发复现 1 次）。
        #
        # 放开是安全的：本机 `sqlite3.threadsafety == 3`（串行化），SQLite 自己会
        # 串行化同一连接上的访问；而且这条连接始终只服务**一个**请求
        # （依赖进入 → 端点执行 → 依赖退出是顺序的），不存在两个线程同处一个
        # 事务的情况
        if readonly:
            self.conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                                        timeout=5.0, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(self.path, timeout=5.0,
                                        check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        for p in PRAGMAS:
            try:
                self.conn.execute(p)
            except sqlite3.Error:
                pass
        if not readonly:
            try:
                self._ensure_schema()
            except Exception:
                # 建 schema 失败就先把连接关掉再抛。构造里抛异常时 `__exit__` 不会
                # 执行，连接会一直挂着——Windows 上表现为库文件被占住、删都删不掉
                # （实测：本仓库的并发用例失败时会因此留下临时目录）
                self.conn.close()
                raise

    #: 需要幂等补加的新列 {表: [(列名, 定义)]}
    #: CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，必须显式 ALTER
    NEW_COLUMNS = {"sources": [
        ("deleted_at", "TEXT NOT NULL DEFAULT ''"),
        ("user_tags", "TEXT NOT NULL DEFAULT ''"),
        ("system_tags_locked", "INTEGER NOT NULL DEFAULT 0"),
    ], "checks": [
        # 缓存版本 8 起的结论行轴：这行结论是"带着搜索探测"写下的吗？口径归
        # `core/checker` 的 `CACHE_VERSION`。**不落这一列等于没修**：
        # item 里写了但没建列，读回来恒为 None，新旧口径在库里分不出来
        ("search_probed", "INTEGER DEFAULT 0"),
        # 结论**是谁判的**（core/models.Engine：jvm / device）。本机引擎与真机的
        # 判据强度不同（真机有登录态和用户自己的网络出口），不记出处就是一份
        # 没有来源的结论。**本地回放那个取值随本地校验链退场（十-4）**
        ("engine", "TEXT NOT NULL DEFAULT ''"),
    ], "jobs": [
        # 任务保留：对齐 exports（expires_at + pinned + sweep），**原来完全没有**——
        # list_jobs 只是显示时 LIMIT 50，表本身无限增长
        ("expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("retry_of", "TEXT NOT NULL DEFAULT ''"),
        # 任务阶段与 status 分开：status 表示生命周期，phase 表示当前执行位置。
        # 旧库补列后默认 queued，避免历史任务被误报成正在执行。
        ("phase", "TEXT NOT NULL DEFAULT 'queued'"),
    ]}

    #: v_sources 视图每次重建：CREATE VIEW IF NOT EXISTS 不会更新已存在的视图定义
    VIEW_DDL = """CREATE VIEW v_sources AS
        SELECT s.id, s.source_url, s.name, s.source_type, s.group_name, s.enabled,
               s.user_tags, s.system_tags_locked, s.fingerprint, s.deleted_at, s.updated_at,
               c.health, c.checked_at, c.probe_depth,
               c.toc_complete, c.content_ok, c.search_hit, c.quality_tags, c.engine
        FROM sources s
        LEFT JOIN checks c ON c.id = (%s)""" % _latest_check_id_sql("s")

    def _schema_ok(self) -> bool:
        """表和视图都在，才算这个库已经建好。

        查一下 sqlite_master，而不是只信进程内那个标记：用户手删 `data/` 里的库
        之后，新建的库里连 sources 表都没有，而标记还在——那会一路报
        「no such table: sources」，看不出是库被删了。这条查询走内存里的 schema，
        代价可以忽略
        """
        try:
            rows = {r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('sources', 'v_sources')")}
        except sqlite3.Error:
            return False
        return {"sources", "v_sources"} <= rows

    def _ensure_schema(self) -> None:
        """本进程内每个库只建一次 schema。理由见 `_SCHEMA_READY` 的注释。

        先无锁查一遍（常见路径不争锁），再进锁复查一遍——两次之间可能有别的线程
        刚建完。`_init_schema` 失败时不记标记，下次请求会重试：半途失败正是需要
        重试的情况
        """
        key = os.path.abspath(self.path)
        if key in _SCHEMA_READY and self._schema_ok():
            return
        with _SCHEMA_LOCK:
            if key in _SCHEMA_READY and self._schema_ok():
                return
            self._init_schema()
            _SCHEMA_READY.add(key)

    def _init_schema(self) -> None:
        for stmt in DDL:
            self.conn.execute(stmt)
        for table, cols in self.NEW_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
        # 表结构迁移要在建视图**之前**：它会重建 sources，而重建过程必须先
        # DROP VIEW（否则 RENAME 会改写视图定义）
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

    def __del__(self):
        # 裸 `Store()` 不关连接时只能等 GC 去关，而 GC 关会打 `ResourceWarning:
        # unclosed database`（测试里成片），Windows 上还会一直占住库文件——`__init__`
        # 里「建 schema 失败先把连接关掉再抛」那条注释记的就是这个坑。
        # `close()` 自己吞异常，所以这里不用再判 `conn` 在不在。
        self.close()

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

    #: JVM 结论的深度排序（结论里的 `stage` 字段）：**越深排越后**。
    #: 与 `appservice` 的 DEPTH_* 同集合——那里是产出侧，这里是读取侧。
    _JVM_STAGE_RANK = {"search": 1, "toc": 2, "content": 3}

    def latest_jvm_conclusions(self) -> "Dict[str, Dict[str, Any]]":
        """{归一化 url: 该源**最深**的一条 JVM 结论}。

        取舍规则是**「深者胜、同深取新」**，不是「最新批次胜」：
        一次 `depth=search` 的补跑（改了个设置、只重跑几条快筛）会把上一批
        目录/正文级的结论盖掉——界面上从「目录 856 章」退回「搜索✓」，
        而用户什么都没删。反过来（deep 覆盖 shallow）永远是对的：更深的结论
        包含更浅的全部信息。

        **同一个 URL 只留一条**、两侧都过宽松归一——库里的 URL 本身有脏字符
        （前导空格/尾斜杠），实测 77/77 靠归一对上（lessons §五十一）。
        """
        import json as _json
        import re as _re
        from core.loader import _normalize_url

        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'jvm_check:%'").fetchall()
        out: "Dict[str, Dict[str, Any]]" = {}
        ranks: "Dict[str, int]" = {}
        for row in rows:
            key, value = row["key"], row["value"]
            m = _re.match(r"jvm_check:([^:]+):(.+)", key)
            if not m:
                continue
            batch, raw_url = m.group(1), m.group(2)
            url = _normalize_url(raw_url.strip()).rstrip("/")
            if not url:
                continue
            try:
                d = _json.loads(value)
            except Exception:
                continue
            # 缺 `stage` 的是 S3-1 之前那批（那时服务只会跑搜索段）——按事实记成
            # search，而不是排到 0：排 0 会让它们在任何比较里都"最浅"，界面上
            # 显示成「没跑到任何一段」，与真相不符
            stage = str(d.get("stage") or "search")
            d.setdefault("stage", stage)
            rank = self._JVM_STAGE_RANK.get(stage, 0)
            prev = ranks.get(url)
            if prev is not None:
                # 更浅的不覆盖；同深比批次号（`YYYYmmdd_HHMMSS` 字典序即时序）
                if rank < prev or (rank == prev and batch <= str(out[url].get("_batch") or "")):
                    continue
            d["_batch"] = batch
            out[url] = d
            ranks[url] = rank
        return out

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

    def known_user_tags(self) -> set:
        """**已知**用户标签 = 默认标签（`core.tags.DEFAULT_USER_TAGS`）+ 库里已有的。

        导入外部源时用它当白名单（`backend/api/imports.py` 与 `upsert_sources`
        两道，同一份名单）。默认标签**不依赖库内容就成立**：空库也要认 R18/正版，
        否则「第一次导入的源带 R18」会被当成陌生标签丢掉——而这条正是设计里
        唯一确定要保留的那类标签。

        名字是公开的（原来叫 `_known_user_tags`）：它现在有库外的调用方
        （导入接口），私有名会把「白名单到底包含什么」变成一个只有本类知道的
        事实——而 lessons §三十四/三十五 已经把它写成对外契约。
        """
        out = set(_DEFAULT_USER_TAGS)
        for row in self.conn.execute(
                "SELECT user_tags FROM sources WHERE deleted_at = ''"):
            out.update(t for t in _normalize_tags(row["user_tags"]) if not _is_system_tag(t))
        return out

    def upsert_sources(self, sources, with_fingerprint: bool = True, allow_new_tags: bool = False) -> int:
        """批量写入/更新书源。同 URL 更新规则和系统标签，用户标签永久保留。

        ``allow_new_tags`` 控制「库里还没见过的标签要不要收」。**默认不收**，
        白名单是 `known_user_tags()`（`core.tags.DEFAULT_USER_TAGS` + 库里已有的）：

          - ``backend/api/imports.py`` 用默认值——外部源带来的陌生标签一律丢掉，
            同一个白名单在导入接口里还先过一遍（`_normalize_group`），两道防线
          - ``backend/api/sources.py`` 的 `save_source` 也用默认值，但它**紧接着**就调
            `set_user_tags([url], body.user_tags)` 整组覆盖——那条链路是「用户在界面上
            明确勾的标签」，不该被这里的白名单管，所以覆盖是对的

        这条是**已决**的（2026-09-17），不再有后续动作——原来这里写着「见 TODO.md」，
        而那条待办在整理时按惯例删掉了（已完成的事项不留），指针就悬空了。
        """
        from core.loader import _normalize_url, fingerprint as fp_of

        ts = now()
        rows = []
        known_tags = self.known_user_tags()
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
            # 冲突目标必须是**部分唯一索引**（只在用的行），不能只写 (source_url)：
            # 表级 UNIQUE 已经换成 `idx_sources_live_url ... WHERE deleted_at = ''`。
            # 语义正是想要的——回收站里的同 URL 行**不构成冲突**，于是「删了再导入」
            # 会新开一行在用的（旧版仍留在回收站），而不是覆盖或报错。
            "ON CONFLICT(source_url) WHERE deleted_at = '' DO UPDATE SET name=excluded.name, "
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

    def live_fingerprints(self) -> Dict[str, str]:
        """``{source_url: fingerprint}``，只含**在用**的行。

        给批量导入用。逐条 ``SELECT`` 的代价在导入这种场景下是致命的：实测
        3000 条要走 3000 次查询 + 3000 次提交（每条一次事务），60 秒。
        一次全捞回来是 3800 行的小表，可以忽略。

        **必须过滤 ``deleted_at``**：回收站里的同 URL 行是历史版本，不是「已存在」
        ——正是这一点让「删了再导入」成立。不过滤的话，删了再导入会被判「重复」，
        既不恢复也不新建，用户看到的是一整批「重复」。
        """
        return {r["source_url"]: (str(r["fingerprint"] or "").strip())
                for r in self.conn.execute(
                    "SELECT source_url, fingerprint FROM sources "
                    "WHERE deleted_at = ''")}

    def export_sources(self) -> List[Dict[str, Any]]:
        """导出全部书源（保持入库顺序），用于重新生成给 Legado 的 JSON。

        ``respondTime`` 由最近一条**引擎 ok 结论**的实测耗时回填（App 换源排序
        吃这个字段）；没有合格结论就保持源里自带的值不动。
        """

        out = []
        for row in self.conn.execute(
                "SELECT raw_json, group_name, user_tags "
                "FROM sources WHERE deleted_at = '' ORDER BY id"):
            src = self._source_view(row["raw_json"], row["group_name"], row["user_tags"])
            if src:
                out.append(src)
        return self.finalize_export(out)

    def finalize_export(self, sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from core.loader import _normalize_url

        checks = self.checks_map()
        fingerprints = self.live_fingerprints()
        for src in sources:
            url = _normalize_url(str(src.get("bookSourceUrl") or ""))
            _attach_respond_time(src, checks.get(url), fingerprints.get(url, ""))
        return sources

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

    #: 复合排序键：**列里显示什么，就按什么排**。
    #:
    #: 「验证结果」= 验过且通过 → 验了没过 → 还没验到（同级按深度深→浅）。
    #: 不做这个的话就会出现「按深度排、列里显示结果」：数据是对的，但看起来**完全
    #: 没排序**（实测前十行是 正文✓/目录✗/目录✗/正文✗…，因为 depth=4 里既有通过的
    #: 也有没过、还有只拿到目录结论的）。
    #: 复合键里的 ``%s`` 是方向（DESC/ASC），**必须落在主键上**。拼在整串末尾
    #: 会落到末尾那个 tie-breaker 上，主键反而按默认 ASC 排——实测结果就是
    #: 「按结果排序」看起来完全没排（第一条永远是没验到的）。
    COMPOSITE_ORDERS = {
        #: **优先级必须与列里显示的完全一致**（`depthVerdict`：正文优先、其次目录）。
        #: 写成 `content_ok = 1 OR toc_complete = 1` 的话，`目录完整但正文不可用`
        #: 那种会被算成「通过」，而列里显示的是「正文 ✗」——排序与看到的又对不上。
        "verified": ("CASE WHEN content_ok = 1 THEN 2 WHEN content_ok = 0 THEN 1 "
                     "WHEN toc_complete = 1 THEN 2 WHEN toc_complete = 0 THEN 1 "
                     "ELSE 0 END %s, probe_depth DESC, id"),
    }

    def query(self, source_type: Optional[int] = None, group: str = "",
              health: str = "", q: str = "", only_enabled: bool = False,
              user_tag: str = "", urls: Optional[Sequence[str]] = None,
              limit: int = 50, offset: int = 0, order: str = "id",
              include_deleted: bool = False) -> List[Dict[str, Any]]:
        """前端列表页用：服务端筛选 + 排序 + 分页（不要全量传给浏览器）。"""
        where, args = self._where(source_type, group, health, q, only_enabled,
                                 include_deleted, user_tag, urls)
        allowed = ("id", "name", "source_type", "group_name",
                   "probe_depth", "checked_at", "updated_at")
        key = (order or "id").lstrip("-")
        direction = "DESC" if str(order).startswith("-") else "ASC"
        if key in self.COMPOSITE_ORDERS:
            order_by = self.COMPOSITE_ORDERS[key] % direction
        else:
            if key not in allowed:
                key, direction = "id", "ASC"
            order_by = "%s %s" % (key, direction)
        sql = ("SELECT * FROM v_sources %s ORDER BY %s LIMIT ? OFFSET ?"
               % (where, order_by))
        return [dict(r) for r in self.conn.execute(sql, args + [int(limit), int(offset)])]

    def count_query(self, source_type: Optional[int] = None, group: str = "",
                    health: str = "", q: str = "", only_enabled: bool = False,
                    include_deleted: bool = False, user_tag: str = "",
                    urls: Optional[Sequence[str]] = None) -> int:
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  include_deleted, user_tag, urls)
        sql = "SELECT COUNT(*) AS c FROM v_sources %s" % where
        return self.conn.execute(sql, args).fetchone()["c"]

    def query_urls(self, source_type: Optional[int] = None, group: str = "",
                   health: str = "", q: str = "", only_enabled: bool = False,
                   user_tag: str = "", urls: Optional[Sequence[str]] = None) -> List[str]:
        """只取 source_url 一列，供「选中全部 N 条筛选结果」。

        **筛选口径必须与 query/count_query 共用 _where**：这个列表的下一步通常是
        批量删除，口径一旦分叉，界面上写的「已选 N 条」就不是实际处理的那批——
        而那个数字正是用户按下确认键的依据。

        **不接 include_deleted**：全选只能在未删除范围内。留这个口子的话，将来
        有人顺手透传，回收站里的源会被一起选进来，而它们是用户特意删掉的。
        """
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  False, user_tag, urls)
        sql = "SELECT source_url FROM v_sources %s" % where
        return [r["source_url"] for r in self.conn.execute(sql, args)]

    def _where(self, source_type, group, health, q, only_enabled,
               include_deleted: bool = False, user_tag: str = "",
               urls: Optional[Sequence[str]] = None):
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
        if urls:
            # 显式 URL 子集（「选中的这几条」）。两侧都要归一：列表里的 source_url
            # 与库里的列是同一套规范化，少一侧就一条都对不上（AGENTS #5）。
            from core.loader import _normalize_url

            keys = [_normalize_url(u) for u in urls if str(u or "").strip()]
            if keys:
                sql.append("AND source_url IN (%s)" % ",".join("?" * len(keys)))
                args += keys
            else:
                # 传了非空的 urls，却一条都归一不出来（全是空串之类）→ **一条都不返回**。
                # 这里绝不能退化成「不筛」：那会把「导出这几条」悄悄变成「导出全库」，
                # 而界面上显示的还是「已生成 N 条的链接」
                sql.append("AND 1 = 0")
        return " ".join(sql), args

    def set_source_comment(self, url: str, comment: str) -> Optional[str]:
        """改备注：只更新 `raw_json["bookSourceComment"]`（没有对应的列）。

        返回**旧备注**（源不存在返回 None），给撤销用。

        与 `set_source_name` 不同，这里**不需要重算 fingerprint**：
        `loader.fingerprint` 的字段表里没有备注（见 core/loader.py），
        改它不会让校验缓存失效。
        """
        from core.loader import _normalize_url

        key = _normalize_url(url)
        row = self.conn.execute(
            "SELECT raw_json FROM sources WHERE source_url = ?", (key,)).fetchone()
        if not row:
            return None
        src = json.loads(row["raw_json"])
        old = str(src.get("bookSourceComment") or "")
        src["bookSourceComment"] = comment
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET raw_json = ?, updated_at = ? WHERE source_url = ?",
                (json.dumps(src, ensure_ascii=False), now(), key))
        return old

    def name_pairs(self, urls: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
        """`[{"url", "name"}]`——「只看名字与地址」的批量操作的输入。

        比 `export_sources()` 轻得多：不解析 raw_json、不重建分组与标签。
        筛选复用 `_where`，所以 `urls=` 的含义与其它入口一致。
        """
        where, args = self._where(None, "", "", "", False, False, "", urls)
        return [dict(r) for r in self.conn.execute(
            "SELECT source_url AS url, name FROM sources %s" % where, args)]

    def set_source_name(self, url: str, name: str) -> Optional[str]:
        """改展示名：同时更新 `sources.name` 与 `raw_json["bookSourceName"]`。

        返回**旧名**（源不存在返回 None），给撤销用。

        两处必须一起改：`name` 列是列表与筛选用，`raw_json` 是导出与校验读的
        ——只改一处的话，界面上改了名、导出的 JSON 里还是旧的。

        **顺手重算 `fingerprint` 列**：`loader.fingerprint` 把 `bookSourceName`
        算在内（见 core/loader.py 的 core 字段表），不重算的话导入去重会拿旧指纹
        比对，同一个源再导入一次会被判成「规则冲突」——而原因只是改过名字。

        **不走 `save_source`**：那条路径会 `set_user_tags([url], body.user_tags)`
        整组覆盖，漏传就把标签清空（lessons §三十五 记过）。
        """
        from core.loader import _normalize_url
        from core.loader import fingerprint as fp_of

        key = _normalize_url(url)
        row = self.conn.execute(
            "SELECT raw_json, name FROM sources WHERE source_url = ?", (key,)).fetchone()
        if not row:
            return None
        old = str(row["name"] or "")
        src = json.loads(row["raw_json"])
        src["bookSourceName"] = name
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET name = ?, raw_json = ?, fingerprint = ?, updated_at = ? "
                "WHERE source_url = ?",
                (name, json.dumps(src, ensure_ascii=False), fp_of(src), now(), key))
        return old

    def groups(self) -> List[tuple]:
        return [(r["group_name"], r["c"]) for r in self.conn.execute(
            "SELECT group_name, COUNT(*) AS c FROM sources GROUP BY group_name ORDER BY c DESC")]

    # ------------------------------------------------------------ tags
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
        return self._update_many(
            "UPDATE sources SET group_name=?, system_tags_locked=1, updated_at=?"
            " WHERE source_url IN", (value, now()), keys)

    def clear_system_tags_override(self, urls) -> int:
        """解除人工锁定，并按最近校验结果重建系统标签。"""
        keys = self._tag_urls(urls)
        if not keys:
            return 0
        n = self._update_many(
            "UPDATE sources SET system_tags_locked=0 WHERE source_url IN", (), keys)
        self.rebuild_system_tags(keys)
        return n

    def _tag_urls(self, urls):
        from core.loader import _normalize_url
        return [_normalize_url(u) for u in (urls or []) if u]

    #: `IN (...)` 的参数个数随 SQLite 版本变（3.50 实测 4000 可以、40000 报
    #: `too many SQL variables`）。分块让这件事与「用户一次选了多少条」无关。
    _SQL_VAR_CHUNK = 900

    def _update_many(self, sql: str, values: tuple, keys: Sequence[str]) -> int:
        """对一组 URL 写**同一份取值**：`sql` 写到 `... IN` 为止，其余部分由这里拼。

        值随行变的（加 / 减 / 改名 / 合并标签）不适用——那一族必须逐行读改写，
        见 `add_user_tags`。
        """
        n = 0
        with self.conn:
            for i in range(0, len(keys), self._SQL_VAR_CHUNK):
                chunk = keys[i:i + self._SQL_VAR_CHUNK]
                cur = self.conn.execute("%s (%s)" % (sql, ",".join("?" * len(chunk))),
                                        (*values, *chunk))
                n += cur.rowcount or 0
        return n

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
        return self._update_many(
            "UPDATE sources SET user_tags=?, updated_at=? WHERE source_url IN",
            (value, now()), keys)

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
        # 口径必须与 tags_overview 一致（都排除回收站）：界面按未删除源计数，
        # 操作就得只改未删除源。否则「显示 2 条、改了 3 条」，回收站里那条被
        # 静默重命名，恢复出来时已经不是原来的标签了。
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources "
            "WHERE deleted_at = '' AND user_tags LIKE ?",
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
        # 同 rename_user_tag：口径与 tags_overview 一致，不动回收站
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources WHERE deleted_at = ''"))
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
        """从未删除的源上移除一个用户标签（回收站不动，见下）。"""
        tags = _canonical_tags(tag)
        if len(tags) != 1 or _is_system_tag(tags[0]):
            return 0
        # 口径必须与 tags_overview 一致（都排除回收站）：界面按未删除源计数，
        # 操作就得只改未删除源。否则「显示 2 条、删了 3 条」，回收站里那条被
        # 静默清掉，用户恢复它时标签已经没了——而他从没在那个源上操作过。
        rows = list(self.conn.execute(
            "SELECT source_url, user_tags FROM sources WHERE deleted_at = ''"))
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
            "SELECT v.id, v.source_type, v.group_name, v.health, "
            "v.quality_tags, v.system_tags_locked "
            "FROM v_sources v" + where, args))
        updates = []
        for row in rows:
            if row["system_tags_locked"]:
                continue
            health = row["health"] or Health.PENDING
            base = group_title(int(row["source_type"] or 0), health)
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
                str(r.get("engine", "") or ""),
            ))
        if not out:
            return 0
        sql = (
            "INSERT INTO checks(source_url,fingerprint,cache_version,health,"
            "status_code,response_time_ms,search_hit,search_response_ms,search_probed,"
            "quality_tags,probe_depth,chapter_count,toc_complete,content_ok,"
            "toc_fail_reason,content_fail_reason,content_response_ms,error,checked_at,engine) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        with self.conn:
            self.conn.executemany(sql, out)
        return len(out)

    def sweep_checks(self) -> int:
        """当前版本每个源只留最近一条校验结果，旧版本行保留。

        当前版本只保留每源最近一条，旧版本行留作追溯。
        当前读者都只取当前版本每源最新一条（`checks_map`、`last_check`、`v_sources`
        视图），历史版本不参与当前状态。
        瞬时网络的结论现在也会落库（见
        `checker.save_cache_append`），所以仍需清理当前版本旧行。

        **当前版本保留的必须是 `checks_map` 会返回的那一条**（同样是
        `checked_at DESC, id DESC`）。两边口径一旦不一致，就会出现"列表上显示的是
        A 行、而它刚被这次清理删掉"——表现是健康度莫名其妙变回上一条，且不报错。

        调用点是 `AsyncChecker.run()` 存完之后（**所有写入路径的唯一收口**，
        Web 任务与 CLI 都走它），不另开定时器；`jobs`/`exports` 那两套是"建新任务
        时顺带扫"，这里没有对应的时机。
        """
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM checks WHERE cache_version = ? AND id NOT IN ("
                " SELECT id FROM checks c WHERE c.cache_version = ? AND c.id = (%s))"
                % _latest_check_id_sql("c"),
                (CACHE_VERSION, CACHE_VERSION))
        return cur.rowcount or 0

    def last_check(self, url: str) -> Optional[Dict[str, Any]]:
        # 先取「最新一条」的 id 再取行：判据只有一份（`_latest_check_id_sql`），
        # 多一次 rowid 查换「不可能与 checks_map / sweep 漂」
        row = self.conn.execute(
            "SELECT * FROM checks WHERE id = (%s)" % _latest_check_id_sql(),
            (url,)).fetchone()
        return dict(row) if row else None

    def checks_map(self) -> Dict[str, Dict[str, Any]]:
        # 返回 {url: 最近一条缓存}；字段与旧 check_cache 的 NDJSON 完全兼容，
        # 可直接顶替 AsyncChecker.load_cache() 的返回值。
        out: Dict[str, Dict[str, Any]] = {}
        # 「最新一条」的判据只有一份：见 `_latest_check_id_sql`
        sql = "SELECT * FROM checks c WHERE c.id = (%s)" % _latest_check_id_sql("c")
        for r in self.conn.execute(sql):
            d = dict(r)
            d["url"] = d.pop("source_url", "")
            d["v"] = d.pop("cache_version", 0)
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
    def fail_orphan_jobs(self) -> int:
        """把**上次进程留下的**非终态任务标成 failed，返回改了几条。

        任务活在进程内的 asyncio task 里（``backend/jobs/runner.py`` 的 ``TASKS``），
        进程一死它们必然不存在——所以「本进程启动时，库里任何非终态任务都是孤儿」
        是**确定性**的，没有假阳性。判据不能用 `updated_at` 超时来找补：进程活着
        但卡在一个慢源上（超时 8s × 最多 5 个请求），和进程死了长得一模一样。

        `sweep_jobs` 那套 TTL 最终也能收掉这些行，但要等 7 天（那条注释里管它们叫
        **僵尸行**）。这 7 天里任务抽屉一直显示它在跑、「任务」按钮的徽标也一直挂着
        ——实测崩溃那次就是这样：一条 check 永远停在 500/3861。

        **调用点只能是「服务进程启动」**，见 ``backend/jobs/runner.recover_orphans``
        （它说明了为什么不能放模块级 import、也不能放 ``backend/__main__.py``）。

        **假定一个库只有一个后端进程**（单机单用户）。真同时起两个打同一个库时，
        后起的会把先起的在跑任务标成 failed——任务本身还在跑，结束时会把真实终态
        写回去。属于短暂的显示错乱，不是数据损坏。
        """
        expiry = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() + JOBS_TTL_DAYS * 86400))
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status = 'failed', phase = 'finished', result_json = ?, updated_at = ?, expires_at = ?"
                " WHERE status IN ('running', 'pending')",
                (json.dumps({"error": "进程重启，任务没写终态（崩溃或被强杀）"},
                            ensure_ascii=False), now(), expiry))
        return cur.rowcount or 0

    def sweep_jobs(self) -> int:
        """清掉过期的任务，返回清了几条。**在建新任务时顺带扫**（对齐 export.py 的
        `sweep_exports`，不另开定时器）。

        **只清理终态任务**：运行中的任务即使超过原始期限也不能被定时器删掉。
        服务重启时先由 `fail_orphan_jobs` 把孤儿任务标成 failed，再由这里按新的
        终态保留期清理；因此进程死掉的任务也不会永久占住任务列表。
        """
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM jobs WHERE pinned = 0 AND expires_at < ?"
                " AND status IN ('done', 'failed', 'cancelled')", (now(),))
        return cur.rowcount or 0

    def create_job(self, job_id: str, kind: str, total: int = 0, payload=None,
                   retry_of: str = "") -> None:
        ts = now()
        expires = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() + JOBS_TTL_DAYS * 86400))
        self.sweep_jobs()          # 顺带清理（对齐 exports 的时机）
        with self.conn:
            self.conn.execute(
                "INSERT INTO jobs(id,kind,status,progress,total,payload,retry_of,"
                "expires_at,pinned,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                (job_id, kind, "pending", 0, int(total),
                 json.dumps(payload or {}, ensure_ascii=False), retry_of or "",
                 expires, ts, ts))

    def update_job(self, job_id: str, status=None, progress=None, total=None,
                   result=None, phase=None) -> None:
        sets, args = ["updated_at = ?"], [now()]
        if status is not None:
            sets.append("status = ?")
            args.append(status)
            if status in ("done", "failed", "cancelled"):
                terminal_expiry = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(time.time() + JOBS_TTL_DAYS * 86400))
                sets.append("expires_at = ?")
                args.append(terminal_expiry)
                if phase is None:
                    phase = "finished"
        if progress is not None:
            sets.append("progress = ?")
            args.append(int(progress))
        if total is not None:
            sets.append("total = ?")
            args.append(int(total))
        if result is not None:
            sets.append("result_json = ?")
            args.append(json.dumps(result, ensure_ascii=False))
        if phase is not None:
            sets.append("phase = ?")
            args.append(str(phase))
        args.append(job_id)
        with self.conn:
            self.conn.execute("UPDATE jobs SET %s WHERE id = ?" % ", ".join(sets), args)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 200,
                  statuses: Optional[Sequence[str]] = None,
                  kind: str = "", recent_since: str = "",
                  exclude_kind: str = "") -> List[Dict[str, Any]]:
        """按筛选条件取任务行（新的在前），最多 ``limit`` 条。

        筛选条件全部由调用方给（见 ``backend/api/jobs.py``）：这里不做口径判断——
        「最近几天算历史」这类窗口只在那一个地方定义，否则同一个数会长出两份。

        ``recent_since`` 是**终态行的窗口**：在跑的任务不受它限制。对整表一刀切
        ``created_at >=`` 会让一条建得早、还在跑的任务从列表里消失——而它正是这个
        列表最该显示的（对应 API 层的「进行中 + 最近 N 天」）。

        **返回轻量行**（``_JOB_LIGHT_COLUMNS``，不含 ``result_json``）：列表要显示结论
        摘要，但为四个数解析上百 KB 的结果不值得——那几个标量由 ``json_extract`` 抽好。
        """
        where: List[str] = []
        args: List[Any] = []
        if statuses:
            where.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args.extend(str(s) for s in statuses)
        if kind:
            where.append("kind = ?")
            args.append(str(kind))
        if exclude_kind:
            # 任务中心按**排除**过滤（默认不看调试运行）：白名单会让新增的 kind
            # 悄悄从列表里消失，而"哪一类不该出现在这里"是明确的
            where.append("kind != ?")
            args.append(str(exclude_kind))
        if recent_since:
            marks = ",".join("?" * len(ACTIVE_JOB_STATUSES))
            where.append("(created_at >= ? OR status IN (%s))" % marks)
            args.append(str(recent_since))
            args.extend(ACTIVE_JOB_STATUSES)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            "SELECT %s FROM jobs%s ORDER BY created_at DESC LIMIT ?"
            % (_JOB_LIGHT_COLUMNS, clause),
            args + [int(limit)]).fetchall()
        return [dict(r) for r in rows]

    def get_job_summary(self, job_id: str) -> Optional[Dict[str, Any]]:
        """单条任务的**轻量行**（列同 ``list_jobs``，不含 ``result_json``）。

        明细弹窗在任务运行期间每 2 秒拉一次它。用 ``get_job`` 就会把完整结果
        （``items[:500]``，上百 KB）每 2 秒传一遍——去看详情才需要那份。
        """
        row = self.conn.execute(
            "SELECT %s FROM jobs WHERE id = ?" % _JOB_LIGHT_COLUMNS,
            (job_id,)).fetchone()
        return dict(row) if row else None

    def delete_job(self, job_id: str) -> bool:
        """删除一条已结束的任务，运行中的任务必须先取消。"""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM jobs WHERE id = ? AND status IN ('done', 'failed', 'cancelled')",
                (job_id,),
            )
        return bool(cur.rowcount)

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
                         q: str = "", only_enabled: bool = False, user_tag: str = "",
                         urls: Optional[Sequence[str]] = None):
        # 按筛选条件导出全部命中源（不分页）。
        # 走 v_sources 视图，这样 health 等只有视图才有的列也能筛。
        where, args = self._where(source_type, group, health, q, only_enabled,
                                  user_tag=user_tag, urls=urls)
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
        return self.finalize_export(out)

    # ---------------------------------------------------------------- 回收站
    # 设计：UI 永不硬删除。软删时把整条 raw_json 快照到
    # data/backups/deleted_<时间戳>.json，彻底删除由使用者在该文件层面处理。
    def soft_delete(self, urls, reason: str = ""):
        """软删除 + 往**同一个**快照文件追加一条记录。返回 (删除条数, 快照路径)。

        快照是纯追加的 JSONL（`data/backups/deleted.jsonl`，一行一次删除操作）：
        `{deleted_at, reason, count, sources:[...]}`。

        **为什么是一个追加文件而不是一次操作一个文件**（原来是
        `deleted_<时间戳>.json`）：

          - 这里记的 `reason` **只存在于快照里**（`sources` 表没有这一列），
            所以它是审计记录、不是副本，不能省。
          - 但按操作切文件是拿文件系统当日志用：实测 198 个文件 / 273 KB，
            读一次要列目录再逐个打开；而且文件名只到秒，同一秒内两次删除会
            **静默互相覆盖**。
          - 追加写（`open(path, "a")` 整行一次 write）不怕中断：最多留下最后半行，
            前面所有记录完好。换成「一个 JSON 数组 + 读-改-写」反而危险——
            重写途中崩掉会把全部历史一起写坏，而这是唯一的一份。

        快照先写、再改库：写失败就不删（宁可记了一条没删成的，也不要删了没记上）。
        """
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
        path = data_path("backups", "deleted.jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "deleted_at": now(),
            "reason": reason or "",
            "count": len(rows),
            "sources": [json.loads(r["raw_json"]) for r in rows],
        }
        with open(path, "a", encoding="utf-8") as f:
            # json.dumps 会把记录内的换行转义成 \n，所以一条记录必然只占一行——
            # 这是 JSONL 能被逐行读的前提
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET deleted_at = ?, updated_at = ? "
                "WHERE deleted_at = '' AND source_url IN (%s)" % marks,
                [now(), now()] + keys)
        return len(rows), path

    def purge_deleted(self) -> Dict[str, Any]:
        """清空回收站：把 ``deleted_at != ''`` 的行**彻底删掉**。

        返回 ``{"purged": n, "snapshot": path}``。这是全仓**唯一**的硬删除路径。

        为什么要有它：界面上写着"UI 永不硬删"，但**没有出口同样不行**——回收站会
        无限增长，而且用户想"删干净重来"时没有干净状态可用（实测踩过：3861 条全在
        回收站，既不能清、又不能靠导入覆盖）。

        **先落快照，再删**（沿用 ``soft_delete`` 的纪律：写失败就不删）。快照必须
        **连 group_name / user_tags / system_tags_locked / fingerprint 一起存**：
        只存 ``raw_json`` 会丢掉用户标签与分组——外发时 ``bookSourceGroup`` 是由
        ``group_name`` + ``user_tags`` **合成**的（见 ``_source_view``），
        raw_json 里那份会被覆盖掉，而这两样在表之外没有第二份。
        """
        from core.paths import data_path

        rows = list(self.conn.execute(
            "SELECT source_url, raw_json, group_name, user_tags, system_tags_locked, "
            "enabled, fingerprint, deleted_at FROM sources WHERE deleted_at <> ''"))
        if not rows:
            return {"purged": 0, "snapshot": ""}
        path = data_path("backups", "purged_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "purged_at": now(),
            "reason": "清空回收站",
            "count": len(rows),
            "sources": [{
                "raw": json.loads(r["raw_json"]),
                "group_name": r["group_name"], "user_tags": r["user_tags"],
                "system_tags_locked": r["system_tags_locked"], "enabled": r["enabled"],
                "fingerprint": r["fingerprint"], "deleted_at": r["deleted_at"],
            } for r in rows],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        with self.conn:
            cur = self.conn.execute("DELETE FROM sources WHERE deleted_at <> ''")
        return {"purged": cur.rowcount or 0, "snapshot": path}

    def trashed_ids(self, urls) -> List[int]:
        """回收站里这些 URL 的行 id。供「按 URL 恢复」的调用方用（如合并撤销）。

        同一个 URL 在回收站里可能有多份历史版本，这里取**最近删除的那份**——
        调用方的语义都是"把我刚删的那条放回来"。
        （`MAX(deleted_at)` 配裸列是 SQLite 的既定行为：返回取到最大值的**那一行**。）
        """
        from core.loader import _normalize_url

        keys = [_normalize_url(u) for u in (urls or []) if u]
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        return [int(r["id"]) for r in self.conn.execute(
            "SELECT id, MAX(deleted_at) FROM sources "
            "WHERE deleted_at <> '' AND source_url IN (%s) "
            "GROUP BY source_url" % marks, keys)]

    def restore(self, ids) -> Dict[str, Any]:
        """按**行 id** 把回收站里的行恢复成在用。

        返回 ``{"restored": n, "blocked": [{id, url, name}, ...]}``。

        粒度必须是行 id：同一个 URL 在回收站里可以有多份历史版本，按 URL 恢复会含糊——恢复哪一份？

        **已有在用版本时拒绝，而不是顶替**：在用唯一性由部分唯一索引保证，
        硬恢复会撞约束；更重要的是顶替会把用户看不见的那行悄悄换掉。被挡下的行
        连名字一起返回，让用户知道要先删哪一条。
        """
        wanted = []
        for i in (ids or []):
            try:
                wanted.append(int(i))
            except (TypeError, ValueError):
                continue
        if not wanted:
            return {"restored": 0, "blocked": []}
        marks = ",".join("?" * len(wanted))
        rows = list(self.conn.execute(
            "SELECT id, source_url, name FROM sources "
            "WHERE deleted_at <> '' AND id IN (%s)" % marks, wanted))
        restored, blocked = 0, []
        with self.conn:
            for r in rows:
                live = self.conn.execute(
                    "SELECT id FROM sources WHERE source_url = ? AND deleted_at = ''",
                    (r["source_url"],)).fetchone()
                if live:
                    blocked.append({"id": int(r["id"]), "url": r["source_url"],
                                    "name": r["name"]})
                    continue
                self.conn.execute(
                    "UPDATE sources SET deleted_at = '', updated_at = ? WHERE id = ?",
                    (now(), r["id"]))
                restored += 1
        return {"restored": restored, "blocked": blocked}

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


def _attach_respond_time(src: Dict[str, Any],
                          check: Optional[Dict[str, Any]],
                          source_fingerprint: str) -> None:
    """把本机引擎实测耗时写进 ``respondTime``（App 端换源排序吃这个字段）。

    只写**引擎结论为 ok** 的：失败行的耗时是被截断的数据（timeout 行 cost≈预算），
    写回去会污染排序；没有引擎结论的源不动——导入时源里自带的 respondTime
    是 App 自己测的，原样保留。``check`` 是 ``checks_map()`` 里该 URL 的最近一条
    （键已归一；调用侧对 ``bookSourceUrl`` 归一后才查，两侧同归一，AGENTS #5）。
    """
    from core.models import Engine, Health

    if not check:
        return
    if check.get("engine") != Engine.JVM or check.get("health") != Health.OK:
        return
    if not source_fingerprint or source_fingerprint != str(check.get("fingerprint") or ""):
        return
    ms = int(check.get("response_time_ms") or 0)
    if ms > 0:
        src["respondTime"] = ms

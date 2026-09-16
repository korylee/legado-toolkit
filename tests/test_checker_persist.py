# -*- coding: utf-8 -*-
"""校验结果的落库路径（Web 那条链路）。

``backend/api/ops.py`` 构造 AsyncChecker 时传的是 ``use_store=True``、
**不传 cache_dir**。改之前这两个值合起来等于「校验算完从不落库」：

  - ``run()`` 里保存那一段被 ``if self.cache_dir:`` 挡住——cache_dir 是 None，
    整段跳过
  - ``save_cache_append`` 又是先 ``os.makedirs(self.cache_dir)`` 再判 use_store
    ——cache_dir 为 None 时第一步就抛 TypeError

表现是：界面点校验、请求成功、状态不变。**两半各守一个用例**，只修一半仍会坏。

本文件不 stub aiohttp（`test_checker_cache.py` 会 stub，那里跑不了 run()）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import inspect
import os
import re
import sqlite3
import tempfile
import unittest

from core.checker import AsyncChecker, is_cache_item_valid
from core.loader import _normalize_url
from core.models import build_record
from core.store import Store

_TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)


def make_source(url: str = "https://a.example/") -> dict:
    return {"bookSourceUrl": url, "bookSourceName": "A",
            "searchUrl": "https://a.example/s?q={{key}}",
            "ruleSearch": {"bookList": "class.item"}}


def _just_checked() -> str:
    """一个「刚校验过」的时刻。

    **别写死日期时间**：``is_cache_item_valid`` 会拒绝**未来**的校验时间
    （防时钟漂移），而写死的值在跑测试时可能还没到——这里就踩过：硬编码
    10:00:00，机器本地时间是 00:57，缓存被判无效，表现成「缓存不复用」。
    """
    return (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def checked(url: str = "https://a.example/", health: str = "ok"):
    rec = build_record(make_source(url), 0)
    rec.health = health
    rec.quality_stars = 4
    # 搜索响应时间非 0 = 这条缓存是**带着搜索探测**写下的（search_probed=True）。
    # 必须给：run() 默认深度到了「搜索」档，而 is_cache_item_valid 会把「本次要验
    # 搜索、缓存却没验过」的条目作废。不给的话本文件所有用它的用例都会**因为错误
    # 的理由**通过——最典型的是 test_changed_rules_invalidate_the_cache，它守的是
    # 「指纹不符必须重校」，却会变成「没验过搜索所以不复用」，指纹那条断言白写
    rec.search_response_ms = 120
    rec.checked_at = _just_checked()
    return rec


class StoreBackendSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def _rows(self):
        st = Store()
        try:
            return [dict(r) for r in st.conn.execute(
                "SELECT source_url, health, stars FROM checks ORDER BY id")]
        finally:
            st.close()

    def test_save_writes_to_store_without_cache_dir(self):
        # 前提：Web 那条链路就是 cache_dir=None + use_store=True。
        # 改之前 save_cache_append 走到 os.makedirs(None) 直接抛 TypeError
        ck = AsyncChecker(concurrency=1, use_store=True)
        self.assertIsNone(ck.cache_dir)
        ck.save_cache_append(checked())
        ck.close()
        self.assertEqual(ck.save_failures, 0)
        self.assertEqual([r["health"] for r in self._rows()], ["ok"])

    def test_run_persists_when_only_the_store_backend_exists(self):
        # 守 run() 里那道门：以前是 `if self.cache_dir:`，整段保存被跳过，
        # 于是请求成功、状态不变
        ck = AsyncChecker(concurrency=1, use_store=True)

        async def fake_check_one(session, record):
            record.health = "ok"
            record.quality_stars = 4
            record.checked_at = _just_checked()
            return record

        ck.check_one = fake_check_one
        asyncio.run(ck.run([build_record(make_source(), 0)]))
        self.assertEqual(len(self._rows()), 1)   # 改之前这里是 0

    def test_store_write_failure_is_counted_not_swallowed(self):
        # 写不进去的表现和「源本来就没变」在界面上无法区分，所以必须能报出来。
        # 改之前这里是 `except Exception: pass`，正好把故障盖住
        class BoomStore:
            def save_checks(self, rows):
                raise RuntimeError("db gone")

        ck = AsyncChecker(concurrency=1, use_store=True)
        ck._store = lambda: BoomStore()
        ck.save_cache_append(checked())
        self.assertEqual(ck.save_failures, 1)

    def test_transient_health_is_written_but_never_reused(self):
        """瞬时错误**落库，但不会被复用**——这两件事原来绑在一起，现在拆开了。

        改之前：超时/异常的源一条都不写，于是列表按「有没有 checks 行」把它们算成
        「未校验」，永远显示没跑过（实测 1222/3861 条），每次全量还要重打一遍请求。
        改之后：照写（界面显示「⏱超时」，能筛出来单独重测），复用那道门挪到
        `is_cache_item_valid`（一次断网/抖动仍然不会变成源的结论）。

        **两个方向都断言**：只断言"写进去了"的话，把复用那道门删掉照样绿——
        而那样一次断网就会把 3861 条全判成超时并复用 7 天。
        """
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked(health="timeout"))
        ck.close()
        rows = self._rows()
        self.assertEqual(len(rows), 1, "瞬时错误也要落库，否则界面永远显示「未校验」")
        self.assertEqual(rows[0]["health"], "timeout")
        # 而且不能被复用（哪怕它就在 TTL 内、指纹也一致）
        reader = AsyncChecker(concurrency=1, use_store=True)
        item = reader.load_cache()[_normalize_url("https://a.example/")]
        reader.close()
        self.assertFalse(is_cache_item_valid(build_record(make_source(), 0), item),
                         "瞬时错误的缓存复用了 = 把一次抖动当成源的结论")

    def test_search_probed_survives_the_store_roundtrip(self):
        """search_probed 必须真的落库、读得回来。

        **这是 v8 那根判定轴的命脉**。item 里写了而 checks 表没有这一列的话，
        checks_map 读回来恒为 None → 所有 OK 源被判「没验过搜索」→ 每次校验都重新
        发请求。表现是"校验跑完了、状态也变了"，完全看不出异常，只是慢——而慢在
        3861 条上是几分钟的事，很难归因到这里。
        """
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())          # checked() 带着搜索响应时间
        ck.close()

        ck2 = AsyncChecker(concurrency=1, use_store=True)
        cache = ck2.load_cache()
        ck2.close()
        item = cache.get(_normalize_url("https://a.example/"))
        self.assertIsNotNone(item)
        self.assertTrue(item["search_probed"])

    def test_star_basis_survives_the_store_roundtrip(self):
        """`star_basis` 也必须真的落库、读得回来。

        **和上面那条是同一个形状**（也就是本文件开头记的那次事故）：item 里写了而
        `checks` 表没有这一列的话，读回来恒为空串——表现是「校验跑完了、星级也对」，
        只是「实测 / 仅规则」这个标注永远不显示，界面上完全看不出异常。
        """
        rec = checked()
        rec.star_basis = "static"
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(rec)
        ck.close()

        ck2 = AsyncChecker(concurrency=1, use_store=True)
        cache = ck2.load_cache()
        ck2.close()
        item = cache.get(_normalize_url("https://a.example/"))
        self.assertIsNotNone(item)
        self.assertEqual(item["star_basis"], "static")


class RunProgressTests(unittest.TestCase):
    """长任务必须把进度报出来——**不报的话界面上永远是 0**。

    后端原来只把进度 `print` 到控制台（`进度: N/M`），`jobs.progress` 从头到尾
    是 0，而全量 3800 条的校验要跑十几分钟。用户看到的就是「点了没反应」，
    只能靠猜还在不在跑。

    CLI 用不上这个回调（打印够了），所以它是可选的；但 Web 那条链路必须传。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def test_progress_is_reported_per_source_not_only_per_batch(self):
        """**逐条报**，不是逐批报，更不是跑完才报一次。

        断言的是**报了什么**，不是报了几次：
        - 只断言「最后报了 (N,N)」→ 把回调挪到循环外照样过（M32 实测如此）
        - 只断言「中间有两三次」→ **按批报**（501 条只报 0/500/501）照样过，
          而按批报的表现正是「界面十几分钟只跳 8 次，看着像卡死」——本次要修的
          就是它
        所以钉死整条序列：0,1,2,…,501，一条都不能少。
        """
        ck = AsyncChecker(concurrency=1, use_store=True)

        async def fake_check_one(session, record):
            record.health = "ok"
            record.checked_at = _just_checked()
            return record

        ck.check_one = fake_check_one
        records = [build_record(make_source("https://a%d.example/" % i), i)
                   for i in range(501)]
        seen = []
        asyncio.run(ck.run(records, on_progress=lambda done, total: seen.append((done, total))))
        ck.close()
        self.assertEqual([d for d, _ in seen], list(range(0, 502)),
                         "每完成一条报一次，从起步那次的 0 一直到 501")
        self.assertEqual(seen[-1][1], 501)

    def test_progress_callback_is_optional(self):
        """不给回调也要能跑——CLI 与既有的测试调用点都不传。"""
        ck = AsyncChecker(concurrency=1, use_store=True)

        async def fake_check_one(session, record):
            record.health = "ok"
            record.checked_at = _just_checked()
            return record

        ck.check_one = fake_check_one
        asyncio.run(ck.run([build_record(make_source(), 0)]))
        ck.close()


class ChecksSchemaParityTests(unittest.TestCase):
    """`save_checks` 写入的列，和 `checks` 表**实际的列**，必须对得上。

    **结构性断言，不是逐列补**：`search_probed` 那次事故（item 里写了、表里没这列
    → 读回来恒为 None → 所有 OK 源永远不复用缓存）之所以能发生，就是因为没有一条
    测试把这两边对起来看。逐列补断言只能防住**已经踩过**的那些，而这类事情的形状
    永远是同一个——**加了一个字段，忘了在其中一处落库**（DDL / NEW_COLUMNS /
    INSERT / checks_map，四处要同时改）。这里把两个最容易漏的方向都钉住。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self.db = os.path.join(self.tmp.name, "sources.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _inserted_columns():
        sql = inspect.getsource(Store.save_checks)
        m = re.search(r"INSERT INTO checks\(([^)]+)\)", sql)
        assert m, "没找到 save_checks 的 INSERT——正则要跟着改"
        # **只取标识符**：那条 SQL 在源码里被拆成多个字符串字面量（引号、换行、
        # 缩进都在中间），按逗号切会把 `health,"` 这种碎片当成列名
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)))

    def test_written_columns_all_exist_in_the_table(self):
        """正向：写了表里没有的列 → 那一列静默丢失。"""
        with Store(self.db) as st:
            have = {r["name"] for r in st.conn.execute("PRAGMA table_info(checks)")}
        self.assertEqual(self._inserted_columns() - have, set(),
                         "save_checks 写了 checks 表里没有的列")

    def test_checks_columns_are_added_to_an_existing_db(self):
        """**老库**的 `checks` 表必须被幂等补列——这是上面两条都盖不住的方向。

        那两条结构性断言用的是**全新的库**：DDL 直接把列建出来，所以「忘了往
        `NEW_COLUMNS` 加一条」在它们那里是**全绿**的。这个缺口不是假想的——
        造变异时我自己就踩了一次：还原没匹配上，`star_basis` 从 `NEW_COLUMNS`
        掉了，测试照样全绿，是我回头 grep 才发现的。

        而真实用户的库是旧 schema，补齐靠的就是 `NEW_COLUMNS` 这条路径。
        """
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, source_url TEXT UNIQUE, "
            "name TEXT, source_type INTEGER, group_name TEXT, enabled INTEGER, "
            "raw_json TEXT, fingerprint TEXT, deleted_at TEXT NOT NULL DEFAULT '', "
            "created_at TEXT, updated_at TEXT)")
        # 「老库」= 现在的 DDL **减去 NEW_COLUMNS 里那几条**——不是随手编一个最小表。
        # 第一版就是编的最小表（只有 7 列），结果视图建不起来（引用了 probe_depth），
        # 而那些列**本来就不该在 NEW_COLUMNS 里**：它们是老版本就有的。
        # 判据是「哪些列是后来补的」，不是「哪些列我懒得写」。
        conn.execute(
            "CREATE TABLE checks (id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL DEFAULT '', cache_version INTEGER NOT NULL DEFAULT 0, "
            "health TEXT NOT NULL DEFAULT '', status_code INTEGER, response_time_ms INTEGER, "
            "search_hit TEXT DEFAULT '', search_response_ms INTEGER, "
            "stars INTEGER DEFAULT 0, quality_tags TEXT DEFAULT '', "
            "probe_depth INTEGER DEFAULT 1, chapter_count INTEGER DEFAULT 0, "
            "toc_complete INTEGER, toc_fail_reason TEXT DEFAULT '', "
            "content_fail_reason TEXT DEFAULT '', content_response_ms INTEGER, "
            "content_ok INTEGER, error TEXT DEFAULT '', checked_at TEXT NOT NULL)")
        conn.commit()
        conn.close()

        with Store(self.db) as st:
            have = {r["name"] for r in st.conn.execute("PRAGMA table_info(checks)")}
            for name, _decl in Store.NEW_COLUMNS["checks"]:
                self.assertIn(name, have, "老库补列漏了 %s" % name)
            # 视图里引用了 c.star_basis——列没补上时这条会抛
            self.assertEqual(st.query(), [])

    def test_every_table_column_is_written(self):
        """反向：表里有列从没被写过 → 它永远是默认值（读出来像「没数据」）。

        `search_probed` 当初就是漏在这一侧的反面：列补上了、写入漏了。
        自增 id 除外。
        """
        with Store(self.db) as st:
            have = {r["name"] for r in st.conn.execute("PRAGMA table_info(checks)")}
        self.assertEqual(have - self._inserted_columns() - {"id"}, set(),
                         "checks 表里有列从没被 save_checks 写过")


class CacheReuseTests(unittest.TestCase):
    """缓存复用与「复用了几条」的可见性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def _run(self, records, check_one):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.check_one = check_one
        asyncio.run(ck.run(records))
        return ck

    def test_second_run_reuses_cache_and_sends_nothing(self):
        # 先落一条
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            return record

        ck2 = self._run([build_record(make_source(), 0)], spy)
        self.assertEqual(ck2.cached_count, 1)    # 复用了几条要能报出来
        self.assertEqual(sent, [])               # 一条请求都没发

    def test_refresh_cache_forces_a_real_request(self):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            record.health = "ok"
            return record

        ck2 = AsyncChecker(concurrency=1, use_store=True)
        ck2.refresh_cache = True                  # 忽略缓存
        ck2.check_one = spy
        asyncio.run(ck2.run([build_record(make_source(), 0)]))
        self.assertEqual(ck2.cached_count, 0)
        self.assertEqual(sent, ["https://a.example/"])

    def test_changed_rules_invalidate_the_cache(self):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            return record

        # 同 URL、规则变了 → 指纹不符 → 必须重校
        changed = make_source()
        changed["ruleSearch"] = {"bookList": "class.CHANGED"}
        ck2 = self._run([build_record(changed, 0)], spy)
        self.assertEqual(ck2.cached_count, 0)
        self.assertEqual(sent, ["https://a.example/"])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_checker_persist` → 确认变红 → 还原）。
#
#  M1  run() 里的保存条件改回 `if self.cache_dir:`
#        → test_run_persists_when_only_the_store_backend_exists 红
#  M2  save_cache_append 把 os.makedirs 挪回 use_store 分支之前
#  M3  Store.save_checks 的 INSERT 里 search_probed 写死 0（= 写了但没落库）
#        → test_search_probed_survives_the_store_roundtrip 与
#          test_second_run_reuses_cache_and_sends_nothing 红
#        → test_save_writes_to_store_without_cache_dir 红（TypeError）
#  M3  store 写失败的 except 改回 `pass`（不计数）
#        → test_store_write_failure_is_counted_not_swallowed 红
#  M4  cached_count 恒定 0（不统计复用）
#        → test_second_run_reuses_cache_and_sends_nothing 红
#
# RunProgressTests 的变异（2026-09-16，修「全量校验卡在 0」）：
#
#  M32  把 on_progress 从批次循环里挪到循环外（只报末尾一次）
#         → test_progress_is_reported_per_source_not_only_per_batch 红
#         ⚠️ **第一版测试没抓住它**：那时断言的是 `len(seen) >= 2` 与
#         `seen[-1] == (N,N)`，而「起步报一次 (0,N) + 末尾报一次 (N,N)」正好满足
#         两条——把回调挪出去照样全绿。**是变异暴露了测试写弱了**，改成断言
#         中间值 `(500, 501)` 之后才真正拦住。
#         教训：断言「报了几次」拦不住「报的时机不对」，得断言**报了什么**。
#
#  M33  改回**按批报**（每批 500 报一次，那次 `(500, 501)` 断言照样满足）
#         → 红在 `[0, 500, 501] != [0, 1, 2, …, 501]`
#         这条是同一格上的第二个坑：M32 修好之后，按批报仍然能骗过「有中间值」
#         这种断言。全量 3861 条按批报 = 整场只有 8 次跳变，界面看着就是卡住，
#         所以断言收成了**整条序列逐条对齐**。
#
# ChecksSchemaParityTests 与 star_basis 往返的变异（2026-09-16）：
#
#  M17  `save_checks` 的 INSERT 里去掉 star_basis（**表里有列、只是不写它**，
#        就是 search_probed 那次事故的形状）
#         → test_every_table_column_is_written 红（结构性）
#         → test_star_basis_survives_the_store_roundtrip 红（行为）
#         **search_probed 的往返保持绿**——证明两条往返断言各守各的字段
#  M17'（第一版写坏了）只从 INSERT 的列名里删、没同步占位符数量
#         → 参数个数对不上，**六条一起红**（全都不是这条要测的东西）。
#         变异写得过重时，红的理由就不指向被测对象了，等于没测
#  M18  把 star_basis 从 DDL 与 NEW_COLUMNS 里都删掉（表里没这列、INSERT 却写）
#         → test_written_columns_all_exist_in_the_table 红（结构性，反方向）
#  M19  **只**把 star_basis 从 NEW_COLUMNS 删掉（DDL 保留）
#         → test_checks_columns_are_added_to_an_existing_db 红
#         ⚠️ **M18/M19 这两条是补测试的直接原因**：M19 是造变异时我自己真实犯的错
#         （还原没匹配上注释），而当时**整个测试文件仍然全绿**——因为上面两条结构性
#         断言用的是**全新的库**（DDL 直接建出列），根本走不到 NEW_COLUMNS 那条路径。
#         真实用户的库是旧 schema，靠的正是那条路径。

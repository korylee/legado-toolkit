# -*- coding: utf-8 -*-
"""Store 的筛选与统计口径。

这里守两件容易被写错、且用户一眼就会看出对不上的事：
  1. ``health="none"`` 要能筛出「没有校验记录」的源（``health IS NULL``）——
     等值过滤表达不出这个条件，传进去会变成恒空的 ``health = 'none'``
  2. 统计分布必须与「源总数」同口径（都排除软删除）——否则 chip 相加会比总数多
"""

from __future__ import annotations

import os
import pathlib
import shutil
import unittest
import uuid

from core.store import Store
from core.checker import CACHE_VERSION


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def make_source(url: str, name: str = "测试源") -> dict:
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "bookSourceType": 0,
        "bookSourceGroup": "",
        "enabled": True,
        "ruleSearch": {"bookList": ".book"},
    }


def make_check(url: str, health: str) -> dict:
    return {
        "v": CACHE_VERSION,
        "url": url,
        "fingerprint": "fp-" + url,
        "name": "测试源",
        "health": health,
        "status_code": 200,
        "response_time_ms": 100,
        "checked_at": "2026-09-15 10:00:00",
    }


class HealthFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_query_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/ 里
        # （实测那儿混着 183 个本文件产生的 `reason="测试"` 快照）
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([
            make_source("https://ok.com", "可用的"),
            make_source("https://dead.com", "失效的"),
            make_source("https://never.com", "从没校验过的"),
        ])
        st.save_checks([
            make_check("https://ok.com", "ok"),
            make_check("https://dead.com", "dead"),
        ])
        return st

    def test_none_filters_unchecked_sources(self):
        """health="none" 筛的是「没有校验记录」——不是「health 字段等于字符串 none」。"""
        with self._seed() as st:
            rows = st.query(health="none")
        self.assertEqual([r["source_url"] for r in rows], ["https://never.com"])

    def test_none_via_count_query_matches(self):
        """列表与计数必须同口径，否则分页会显示「共 1 条」却列出 0 行。"""
        with self._seed() as st:
            self.assertEqual(st.count_query(health="none"), 1)
            self.assertEqual(st.count_query(health="ok"), 1)
            self.assertEqual(st.count_query(health="dead"), 1)

    def test_equality_filter_still_works(self):
        with self._seed() as st:
            rows = st.query(health="ok")
        self.assertEqual([r["source_url"] for r in rows], ["https://ok.com"])

    def test_empty_health_means_no_filter(self):
        """空串 = 不筛。这个语义不能因为加了 "none" 分支而改变。"""
        with self._seed() as st:
            self.assertEqual(st.count_query(health=""), 3)


class StatsParityTests(unittest.TestCase):
    """统计分布与「源总数」必须同口径（都排除软删除）。"""

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_stats_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def test_distributions_sum_to_source_count(self):
        with Store(self.db) as st:
            st.upsert_sources([
                make_source("https://a.com"), make_source("https://b.com"),
                make_source("https://gone.com"),
            ])
            st.save_checks([make_check("https://a.com", "ok")])
            st.soft_delete(["https://gone.com"], "测试")
            s = st.stats()
        # 源总数（排除软删除）
        self.assertEqual(s["sources"], 2)
        # 三个分布相加都应等于源总数——含回收站的话会多出来
        self.assertEqual(sum(s["types"].values()), s["sources"])
        self.assertEqual(sum(s["health"].values()), s["sources"])


class QueryUrlsTests(unittest.TestCase):
    """``query_urls`` 必须与 ``query`` 同口径。

    它是「选中全部 N 条筛选结果」的数据来源：两边口径不一致的话，界面上写着
    「已选 800 条」而实际拿到的是另一批——而这一步的下一步是删除，用户是按那个
    数字下的手。
    """

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_urls_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([
            make_source("https://ok.com", "可用的"),
            make_source("https://dead.com", "失效的"),
            make_source("https://never.com", "从没校验过的"),
        ])
        st.save_checks([
            make_check("https://ok.com", "ok"),
            make_check("https://dead.com", "dead"),
        ])
        return st

    def test_urls_match_query_for_the_same_filter(self):
        with self._seed() as st:
            for params in ({}, {"health": "ok"}, {"health": "dead"},
                           {"health": "none"}, {"q": "dead"}):
                self.assertEqual(
                    sorted(st.query_urls(**params)),
                    sorted(r["source_url"] for r in st.query(limit=100, **params)),
                    "口径不一致: %r" % (params,))

    def test_export_by_filter_honors_url_subset(self):
        with self._seed() as st:
            picked = ["https://ok.com", "https://never.com"]
            srcs = st.export_by_filter(urls=picked)
            self.assertEqual(sorted(s["bookSourceUrl"] for s in srcs), sorted(picked))

    def test_url_list_filter_matches_query_and_count(self):
        with self._seed() as st:
            picked = ["https://ok.com", "https://never.com"]
            self.assertEqual(sorted(st.query_urls(urls=picked)), sorted(picked))
            self.assertEqual(st.count_query(urls=picked), 2)
            self.assertEqual(
                sorted(r["source_url"] for r in st.query(urls=picked, limit=100)),
                sorted(picked))

    def test_url_list_filter_normalizes_like_source_url(self):
        with self._seed() as st:
            self.assertEqual(st.query_urls(urls=[" https://OK.com/ "]), ["https://ok.com"])

    def test_empty_url_list_means_no_filter(self):
        with self._seed() as st:
            self.assertEqual(len(st.query_urls(urls=[])), 3)

    def test_deleted_sources_are_excluded(self):
        with self._seed() as st:
            st.soft_delete(["https://dead.com"])
            urls = st.query_urls()
        self.assertIn("https://ok.com", urls)
        self.assertNotIn("https://dead.com", urls)


class LatestCheckTests(unittest.TestCase):
    """同一秒内的两条校验记录，「最新」必须取**后写入**的那条。

    ``checked_at`` 只到秒。``ORDER BY checked_at DESC`` 在秒并列时的顺序是不确定的
    ——实测走 ``idx_checks_url`` 时返回的是**先**写入的那条。于是「最新结论」可能是
    上一轮的：列表上显示的 health 是旧的，v8 起更严重——``search_probed`` 也参与
    缓存复用判定，取错行会让「刚验过搜索」的源被判成「没验过」，每次校验都白打请求。
    """

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_latest_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([make_source("https://a.com")])
        st.save_checks([make_check("https://a.com", "dead")])
        st.save_checks([make_check("https://a.com", "ok")])   # checked_at 完全相同
        return st

    # 三处调用点共用同一份判据（`_latest_check_id_sql`），但每条 SQL 是各自拼的
    # （外层别名 s / c / 不给），**接错一处不报错、只取错行**，所以仍逐处断言：
    # 合成一条的话，第一个失败后面的不执行，另外两处接错也看不出来
    def test_checks_map_takes_the_later_row(self):
        with self._seed() as st:
            self.assertEqual(st.checks_map()["https://a.com"]["health"], "ok")

    def test_last_check_takes_the_later_row(self):
        with self._seed() as st:
            self.assertEqual(st.last_check("https://a.com")["health"], "ok")

    def test_list_view_takes_the_later_row(self):
        """列表走 v_sources 视图——那是最容易被漏掉的一份实现。"""
        with self._seed() as st:
            self.assertEqual(st.query(q="a.com")[0]["health"], "ok")



    def test_old_version_does_not_feed_current_state(self):
        with Store(self.db) as st:
            st.upsert_sources([make_source("https://a.com")])
            old = make_check("https://a.com", "ok")
            old["v"] = CACHE_VERSION - 1
            st.save_checks([old])
            self.assertEqual(st.checks_map(), {})
            self.assertIsNone(st.last_check("https://a.com"))
            self.assertIsNone(st.query(q="a.com")[0]["health"])

    def test_current_version_wins_over_newer_old_version_row(self):
        with Store(self.db) as st:
            st.upsert_sources([make_source("https://a.com")])
            old = make_check("https://a.com", "dead")
            old["v"] = CACHE_VERSION - 1
            old["checked_at"] = "2026-09-16 10:00:00"
            st.save_checks([old])
            st.save_checks([make_check("https://a.com", "ok")])
            self.assertEqual(st.checks_map()["https://a.com"]["health"], "ok")
            self.assertEqual(st.last_check("https://a.com")["health"], "ok")
            self.assertEqual(st.query(q="a.com")[0]["health"], "ok")

    def test_sweep_keeps_old_versions_and_removes_stale_current_rows(self):
        with Store(self.db) as st:
            st.upsert_sources([make_source("https://a.com")])
            old = make_check("https://a.com", "dead")
            old["v"] = CACHE_VERSION - 1
            st.save_checks([old])
            st.save_checks([make_check("https://a.com", "dead")])
            st.save_checks([make_check("https://a.com", "ok")])
            self.assertEqual(st.sweep_checks(), 1)
            rows = st.conn.execute(
                "SELECT cache_version, health FROM checks "
                "WHERE source_url = ? ORDER BY id", ("https://a.com",)).fetchall()
            self.assertEqual([(r["cache_version"], r["health"]) for r in rows], [
                (CACHE_VERSION - 1, "dead"), (CACHE_VERSION, "ok")])

    def test_sweep_keeps_exactly_what_checks_map_returns(self):
        """sweep 与 checks_map 必须同口径（`_latest_check_id_sql` 那份）。

        漂了的症状是：列表上显示 A 行、而它刚被这次清理删掉 → 健康度莫名回退，且
        不报错（`sweep_checks` 的 docstring 记的就是这个）。同秒两行是判据最容易
        分叉的地方，所以这里专门用它当夹具。
        """
        with Store(self.db) as st:
            st.upsert_sources([make_source("https://a.com")])
            st.save_checks([make_check("https://a.com", "dead")])
            st.save_checks([make_check("https://a.com", "ok")])   # 与上一条同秒
            keep = st.checks_map()["https://a.com"]["id"]
            older = make_check("https://a.com", "dead")
            older["checked_at"] = "2026-09-14 10:00:00"           # 更早的当前版本行
            st.save_checks([older])
            # 删两条：更早那条，以及同秒里 id 更小的那条（判据是 `id DESC`，取后写入的）
            self.assertEqual(st.sweep_checks(), 2)
            self.assertEqual(st.checks_map()["https://a.com"]["id"], keep,
                             "sweep 删掉了 checks_map 返回的那条")
            self.assertEqual(st.last_check("https://a.com")["id"], keep)


class LatestCheckJudgeSingleSourceTests(unittest.TestCase):
    """判据只许有一份：`ORDER BY checked_at DESC, id DESC` 在 store.py 里只出现一次。

    上面那条行为钉子能抓住「改坏一处实现」；抓不住的是**有人把 SQL 又抄回某个方法里**
    ——那份抄本此刻行为相同，下次改判据时才会分叉，而分叉的症状不报错。
    """

    def test_the_latest_check_sql_is_written_once(self):
        path = pathlib.Path(__file__).resolve().parent.parent / "core" / "store.py"
        src = path.read_text(encoding="utf-8")
        self.assertEqual(src.count("ORDER BY checked_at DESC, id DESC"), 1,
                         "「每源最新一条」的判据被抄成了第二份：应改走 _latest_check_id_sql")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_store_query.LatestCheckTests`
# → 确认变红 → 还原）。
#
#  M1  把共用判据（`_latest_check_id_sql`）里的 `, id DESC` 去掉
#        → test_checks_map_takes_the_later_row /
#          test_last_check_takes_the_later_row /
#          test_list_view_takes_the_later_row **三条各自红**
#          （拆成三条独立断言就是为了这个：合成一条的话，第一个失败后面的不再执行，
#            另外两处接错看不出来）
#
#  M2  只把 `sweep_checks` 那份的 `, id DESC` 换成 `, id ASC`（与 checks_map 不再同口径）
#        → test_sweep_keeps_exactly_what_checks_map_returns **红**
#          （`1 != 2`：它删掉了 checks_map 正要返回的那条——正是「健康度莫名回退」）
#        → LatestCheckJudgeSingleSourceTests 仍绿：它守的是「判据被抄第二份」，
#          而这次变异是**改**了那一份，不是抄

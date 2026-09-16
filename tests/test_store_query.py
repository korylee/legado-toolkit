# -*- coding: utf-8 -*-
"""Store 的筛选与统计口径。

这里守两件容易被写错、且用户一眼就会看出对不上的事：
  1. ``health="none"`` 要能筛出「没有校验记录」的源（``health IS NULL``）——
     等值过滤表达不出这个条件，传进去会变成恒空的 ``health = 'none'``
  2. 统计分布必须与「源总数」同口径（都排除软删除）——否则 chip 相加会比总数多
"""

from __future__ import annotations

import os
import shutil
import unittest
import uuid

from core.store import Store


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
        "v": 6,
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([make_source("https://a.com")])
        st.save_checks([make_check("https://a.com", "dead")])
        st.save_checks([make_check("https://a.com", "ok")])   # checked_at 完全相同
        return st

    # 三处「取最新」是**三份独立实现**（各一条 SQL），必须逐个断言：
    # 合成一条的话，第一个失败后面的就不执行了，另外两处漏改也看不出来
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


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_store_query.LatestCheckTests`
# → 确认变红 → 还原）。
#
#  M1  三处「取最新」一起去掉 `, id DESC`
#        → test_checks_map_takes_the_later_row /
#          test_last_check_takes_the_later_row /
#          test_list_view_takes_the_later_row **三条各自红**
#          （拆成三条独立断言就是为了这个：合成一条的话，第一个失败后面的不再执行，
#            另外两处漏改看不出来）

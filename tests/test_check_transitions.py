# -*- coding: utf-8 -*-
"""校验结果「相对上一次的变化」统计的测试。

守的是 ③ 的核心契约：摘要只报**变了什么**，并且把「首次有结论」与「变成 X」分开。
库里 3861 条有 3860 条从未校验过，第一次全量之后「新增可用 2000 条」不代表
「比上次好」——那是**首次**。混在一起这个数字就失去意义。
"""

from __future__ import annotations

import asyncio
import unittest

from backend.api import ops
from backend.api.check_summary import summarize_transitions
from core.loader import _normalize_url
from core.models import Health, build_record


def make_record(url: str, health: str):
    """给 `FakeChecker` 吃的 Record（`check_items_from_records` 读它的属性）。"""
    rec = build_record({"bookSourceUrl": url, "bookSourceName": "example"}, 0)
    rec.health = health
    return rec


def make_item(url: str, health: str) -> dict:
    """`summarize_transitions` 现在收**形状 dict**（两条校验路的 items 都是它）。

    与 `make_record` 分开：那个是给 `FakeChecker` 吃的（它按 Record 接口被
    `check_items_from_records` 读属性），两者不是同一个东西，别混。
    """
    return {"url": url, "name": "example", "health": health}


class TransitionSummaryTests(unittest.TestCase):
    def test_health_change_lands_in_new_health_bucket(self) -> None:
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_item("https://a.com", Health.DEAD)])
        self.assertEqual(out["changed"], {Health.DEAD: 1})
        self.assertEqual(out["first_checked"], 0)

    def test_unchanged_health_is_not_reported(self) -> None:
        """没变的不进 changed。

        缓存命中的源全走这条路径（health 与缓存里那条必然相同），所以这个条件
        写松一点就会满屏假变化——而「这次比上次多了 800 条失效」是会被当真的。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_item("https://a.com", Health.OK)])
        self.assertEqual(out["changed"], {})
        self.assertEqual(out["first_checked"], 0)

    def test_source_without_history_counts_as_first_checked(self) -> None:
        """没有历史记录的源只进 first_checked，不进 changed。"""
        out = summarize_transitions({}, [make_item("https://a.com", Health.OK)])
        self.assertEqual(out["first_checked"], 1)
        self.assertEqual(out["changed"], {})

    def test_url_is_normalized_before_comparing(self) -> None:
        """两侧 URL 必须归一再比。

        ``prev`` 的键在写库时就归一了，而 results 里的 ``r.url`` 是抓取时的原文
        （实测 20.7% 的源带尾斜杠）。不归一的话「没变」会被误报成「首次有结论」
        ——而首次那栏本来就是最大的数，错在里面看不出来。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_item("https://A.com/", Health.OK)])
        self.assertEqual(out["first_checked"], 0)
        self.assertEqual(out["changed"], {})

    def test_changed_items_name_the_sources(self) -> None:
        """变化必须给出**明细**，不只是计数。

        摘要说「6 条变成失效」，用户下一步一定是问「哪 6 条」——只给计数
        等于让他自己去 3800 行里翻。明细里的 url 同样要归一（前端拿它跳转/匹配）。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_item("https://A.com/", Health.DEAD)])
        self.assertEqual(out["changed_items"],
                         [{"url": "https://a.com", "name": "example",
                           "from": Health.OK, "to": Health.DEAD}])

    def test_unchanged_and_first_checked_are_not_in_the_items(self) -> None:
        """反向断言：明细里只有**真变了**的。

        首次有结论的源（库里绝大多数）不能混进来——那会把明细冲成几千条，
        而它们不需要被「跟进处理」，和「变成失效」完全不是一回事。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [
            make_item("https://a.com", Health.OK),      # 没变
            make_item("https://b.com", Health.DEAD),    # 库里没有 → 首次
        ])
        self.assertEqual(out["changed"], {})
        self.assertEqual(out["first_checked"], 1)
        self.assertEqual(out["changed_items"], [])

    def test_mixed_batch_buckets_correctly(self) -> None:
        """一批里三种情况并存时，各归各的桶。"""
        prev = {
            _normalize_url("https://a.com"): {"health": Health.OK},
            _normalize_url("https://b.com"): {"health": Health.DEAD},
        }
        results = [
            make_item("https://a.com", Health.DEAD),    # 变了 → changed[dead]
            make_item("https://b.com", Health.DEAD),    # 没变 → 哪都不进
            make_item("https://c.com", Health.OK),      # 首次 → first_checked
            make_item("https://d.com", Health.OK),      # 首次 → first_checked
        ]
        out = summarize_transitions(prev, results)
        self.assertEqual(out["first_checked"], 2)
        self.assertEqual(out["changed"], {Health.DEAD: 1})


class FakeChecker:
    """替身：不发请求，但**模拟真实链路把新结论写进库**。

    这正是本类要守的顺序问题的关键——如果它不写库，「跑之前读快照」与
    「跑之后读快照」就长得一模一样，测不出区别。
    """

    store = None
    records: list = []

    def __init__(self, **kwargs):
        self.cached_count = 0
        self.save_failures = 0
        self.hit_downgrades = []
        self.refresh_cache = False

    async def run(self, records, on_progress=None):
        # 签名必须与 AsyncChecker.run 一致（多一个 on_progress）——漏改的话
        # ops.run_check_job 传关键字参数会直接 TypeError，桩"跑不起来"总比
        # "跑起来了但测的不是真东西"好，但仍然是没跟上，得改
        for r in FakeChecker.records:
            FakeChecker.store.record_result(_normalize_url(r.url), r.health)
        if on_progress:
            on_progress(len(FakeChecker.records), len(FakeChecker.records))
        return FakeChecker.records

    def close(self):
        pass


class FakeStore:
    def __init__(self, prev):
        self.state = dict(prev)

    def export_sources(self):
        return [{"bookSourceUrl": u, "bookSourceName": u, "bookSourceType": 0}
                for u in ("https://a.com", "https://b.com")]

    def checks_map(self):
        # 每次都反映**当前**库状态，两种读法才会给出不同答案
        return {k: dict(v) for k, v in self.state.items()}

    def record_result(self, url, health):
        self.state[url] = {"health": health}

    def update_job(self, *args, **kwargs):
        pass

    def rebuild_system_tags(self, urls=None):
        # 签名必须跟真 Store 一致（`urls=None` = 全库重建）
        self.rebuilt = urls


class CheckJobTransitionsTests(unittest.TestCase):
    """接线：run_check_job 真的把 transitions 报出来了，且快照是**跑之前**读的。

    顺序写反（跑完再读）是最容易犯、也最难发现的错：那时新结论已经落库，
    每条源都是 old == new，摘要会永远显示「无状态变化」——恰恰把这次改动
    要回答的那个问题答错了，而且看起来一切正常。
    """

    def setUp(self) -> None:
        import core.checker
        self._orig = core.checker.AsyncChecker
        core.checker.AsyncChecker = FakeChecker
        self.store = FakeStore({_normalize_url("https://a.com"): {"health": Health.OK}})
        FakeChecker.store = self.store
        FakeChecker.records = [
            make_record("https://a.com", Health.DEAD),   # 上次 ok、这次 dead → 变了
            make_record("https://b.com", Health.OK),     # 库里没有 → 首次
        ]

    def tearDown(self) -> None:
        import core.checker
        core.checker.AsyncChecker = self._orig
        FakeChecker.store = None
        FakeChecker.records = []

    def test_job_reports_transitions_from_pre_run_snapshot(self) -> None:
        out = asyncio.run(ops.run_check_job("j1", self.store, {}))
        self.assertEqual(out["transitions"]["changed"], {Health.DEAD: 1})
        self.assertEqual(out["transitions"]["first_checked"], 1)

    def test_item_urls_are_normalized_for_the_frontend(self) -> None:
        """`items[].url` 必须是**归一化**后的 URL——前端拿它当 key 回填列表。

        `build_record` 给的 `r.url` 是 bookSourceUrl 的**原文**，而列表里的
        `source_url` 是 Store 归一化后存的（去空白 / 尾斜杠 / 转小写）。
        两侧不归一的话前端**一条都匹配不上**，表现是「校验完了列表不更新」，
        界面上看不出任何异常。本项目实测 20.7% 的源带尾斜杠（lessons §五）。

        这条断言看着琐碎，但它守的是一个**静默失效**：写错了不会报错，
        只会让就地回填这个功能整个不生效。
        """
        FakeChecker.records = [make_record("https://A.com/", Health.OK)]
        out = asyncio.run(ops.run_check_job("j1", self.store, {}))
        self.assertEqual(out["items"][0]["url"], "https://a.com")

    def test_items_carry_what_the_list_needs_to_backfill(self) -> None:
        """就地回填要用的字段一个都不能少——少一个就是「那一格永远不更新」。

        列表行的字段来自 `SourceOut`，回填直接覆盖它们；`items` 里缺哪个，
        页面上就有一格看起来像「没校验」。
        """
        FakeChecker.records = [make_record("https://a.com", Health.OK)]
        out = asyncio.run(ops.run_check_job("j1", self.store, {}))
        for key in ("url", "health", "stars", "star_basis",
                    "toc_complete", "content_ok", "search_hit", "checked_at"):
            self.assertIn(key, out["items"][0], "回填缺字段: %s" % key)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_check_transitions`
# → 确认变红 → 还原）。
#
#  M23 把「首次有结论」的源也塞进 changed_items（明细退化成「所有源」）
#        → test_unchanged_and_first_checked_are_not_in_the_items 红
#        （明细被几千条「首次」冲掉，而它们不需要跟进处理——和「变成失效」不是一回事）
#  M24 changed_items 里的 url 去掉归一化（计数那条测试**不红**，只有明细这条红）
#        → test_changed_items_name_the_sources 红
#        与 M22 同一课：归一化漏在**任何一处**都是静默失效，所以每处都要有断言
#
#  M22 `items[].url` 去掉归一化（`_normalize_url(r.url)` → `r.url`）
#        → test_item_urls_are_normalized_for_the_frontend 红
#        守的是一个**静默失效**：前端拿 items[].url 当 key 回填列表，而列表里的
#        source_url 是库归一化过的。不归一 → 一条都匹配不上 → 「校验完了列表不更新」，
#        界面上看不出任何异常。本条与 M1（两侧归一后才比）是同一课的两处落点。
#
#  M1  summarize_transitions 的 `elif old != r.health` 改成 `elif True`
#        （没变也算进 changed，即「缓存命中的源也会被报成变化」）
#        → test_unchanged_health_is_not_reported /
#          test_url_is_normalized_before_comparing /
#          test_mixed_batch_buckets_correctly 红
#  M2  `prev.get(_normalize_url(r.url))` 改成 `prev.get(r.url)`（只归一单侧）
#        → test_url_is_normalized_before_comparing 红
#  M3  `prev_checks = st.checks_map()` 挪到 run() 之后
#        → test_job_reports_transitions_from_pre_run_snapshot 红
#          （实测报 `{} != {'dead': 1}`，正是「摘要永远无变化」那个失败模式）

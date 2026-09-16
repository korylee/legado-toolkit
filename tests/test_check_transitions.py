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
from backend.api.ops import summarize_transitions
from core.loader import _normalize_url
from core.models import Health, build_record


def make_record(url: str, health: str):
    rec = build_record({"bookSourceUrl": url, "bookSourceName": "example"}, 0)
    rec.health = health
    return rec


class TransitionSummaryTests(unittest.TestCase):
    def test_health_change_lands_in_new_health_bucket(self) -> None:
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_record("https://a.com", Health.DEAD)])
        self.assertEqual(out["changed"], {Health.DEAD: 1})
        self.assertEqual(out["first_checked"], 0)

    def test_unchanged_health_is_not_reported(self) -> None:
        """没变的不进 changed。

        缓存命中的源全走这条路径（health 与缓存里那条必然相同），所以这个条件
        写松一点就会满屏假变化——而「这次比上次多了 800 条失效」是会被当真的。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_record("https://a.com", Health.OK)])
        self.assertEqual(out["changed"], {})
        self.assertEqual(out["first_checked"], 0)

    def test_source_without_history_counts_as_first_checked(self) -> None:
        """没有历史记录的源只进 first_checked，不进 changed。"""
        out = summarize_transitions({}, [make_record("https://a.com", Health.OK)])
        self.assertEqual(out["first_checked"], 1)
        self.assertEqual(out["changed"], {})

    def test_url_is_normalized_before_comparing(self) -> None:
        """两侧 URL 必须归一再比。

        ``prev`` 的键在写库时就归一了，而 results 里的 ``r.url`` 是抓取时的原文
        （实测 20.7% 的源带尾斜杠）。不归一的话「没变」会被误报成「首次有结论」
        ——而首次那栏本来就是最大的数，错在里面看不出来。
        """
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        out = summarize_transitions(prev, [make_record("https://A.com/", Health.OK)])
        self.assertEqual(out["first_checked"], 0)
        self.assertEqual(out["changed"], {})

    def test_mixed_batch_buckets_correctly(self) -> None:
        """一批里三种情况并存时，各归各的桶。"""
        prev = {
            _normalize_url("https://a.com"): {"health": Health.OK},
            _normalize_url("https://b.com"): {"health": Health.DEAD},
        }
        results = [
            make_record("https://a.com", Health.DEAD),    # 变了 → changed[dead]
            make_record("https://b.com", Health.DEAD),    # 没变 → 哪都不进
            make_record("https://c.com", Health.OK),      # 首次 → first_checked
            make_record("https://d.com", Health.OK),      # 首次 → first_checked
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

    async def run(self, records):
        for r in FakeChecker.records:
            FakeChecker.store.record_result(_normalize_url(r.url), r.health)
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

    def rebuild_system_tags(self):
        pass


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


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_check_transitions`
# → 确认变红 → 还原）。
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

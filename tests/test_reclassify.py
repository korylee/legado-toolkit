# -*- coding: utf-8 -*-
"""书源类型重判定（reclassify）的离线用例。

本模块 465 行此前**零测试**，而它按「累计打分 ≥3 且严格高于另一方向」改
``bookSourceType`` → 直接决定类型系统标签和分组。打分逻辑最怕阈值、权重、
信号词被静默改掉：改完不会有任何报错，只会让一批源被悄悄改判。

这里只测**纯函数**（静态打分 / 首页信号 / 合并判定），不联网。
lessons §四 定下的规矩是「实测优先于声明」「证据不足保持原样」——
下面每条断言都对着这两条。

**权重断言是刻意的**：改权重属于会改判定的行为变更，改的时候应该让这里变红。
"""

from __future__ import annotations

import unittest

from core.reclassify import combine_type, homepage_signals, infer_type_static

MANGA = 2
NOVEL = 0
KEEP = -1          # 证据不足 / 平手 → 保持原样


def scores(source):
    """只取 (漫画分, 小说分)，便于把权重钉死。"""
    _type, manga, novel, _why = infer_type_static(source)
    return manga, novel


def judge(source):
    return infer_type_static(source)[0]


class ConservatismTests(unittest.TestCase):
    """证据不足时一律保持原样——误改比漏改贵。"""

    def test_no_signal_keeps_original(self):
        self.assertEqual(judge({}), KEEP)

    def test_declared_type_alone_is_not_enough(self):
        # 声明值是「参考」不是「证据」：它只加 1 分，达不到阈值。
        # 这条守的是 lessons §四 的根因——默认值 novel 一路写进库
        for declared in (0, 2):
            with self.subTest(declared=declared):
                self.assertEqual(
                    judge({"bookSourceUrl": "https://x.example/",
                           "bookSourceType": declared}), KEEP)

    def test_unrelated_host_and_name_keeps_original(self):
        self.assertEqual(
            judge({"bookSourceUrl": "https://x.example/",
                   "bookSourceName": "某某站"}), KEEP)


class ThresholdTests(unittest.TestCase):
    """阈值是 3，且必须**严格高于**另一方向。"""

    def test_tie_does_not_reclassify(self):
        # 两个方向各 3 分。写成 `>=` 就会在这里改判——平手时没有理由动它
        tie = {"bookSourceUrl": "https://x.example/", "bookSourceName": "漫画小说",
               "bookSourceType": 1}      # 声明 1 两边都不加分，保证是纯平手
        self.assertEqual(scores(tie), (3, 3))
        self.assertEqual(judge(tie), KEEP)

    def test_one_point_lead_reclassifies(self):
        # 同上是平手，声明成 2 后漫画多 1 分 → 4:3 → 改判
        win = {"bookSourceUrl": "https://x.example/", "bookSourceName": "漫画小说",
               "bookSourceType": 2}
        self.assertEqual(scores(win), (4, 3))
        self.assertEqual(judge(win), MANGA)

    def test_exactly_three_points_is_enough(self):
        # 阈值边界：单项 3 分（域名特征）就够了
        self.assertEqual(judge({"bookSourceUrl": "https://manhua.example/"}), MANGA)

    def test_two_points_is_not_enough(self):
        # 域名小说特征只有 2 分，达不到阈值。注意声明成 0（或不写）时会再补 1 分
        # 正好到 3 —— 「缺省即小说」这个偏置是这张表最容易算错的地方
        self.assertEqual(scores({"bookSourceUrl": "https://book.example/",
                                 "bookSourceType": 1}), (0, 2))
        self.assertEqual(judge({"bookSourceUrl": "https://book.example/",
                                "bookSourceType": 1}), KEEP)

    def test_declared_bonus_tips_two_points_over_threshold(self):
        # 同一份静态证据：声明成 0（缺省就是 0）→ 3 分 → 改判；声明成 1 → 2 分 → 保持。
        # 也就是说「不写 bookSourceType」本身就在往小说方向推——lessons §四 说的
        # 「默认值一路写进库」在这里还能再看到一次
        self.assertEqual(judge({"bookSourceUrl": "https://book.example/"}), NOVEL)
        self.assertEqual(
            judge({"bookSourceUrl": "https://book.example/",
                   "bookSourceType": 1}), KEEP)


class SignalWeightTests(unittest.TestCase):
    """各信号的权重。改这些数字 = 改判定结果，应当让这里变红。"""

    def test_manga_host_hint_worth_three(self):
        self.assertEqual(scores({"bookSourceUrl": "https://manhua.example/"})[0], 3)

    def test_manga_text_hint_worth_three(self):
        self.assertEqual(
            scores({"bookSourceUrl": "https://x.example/",
                    "bookSourceName": "某某漫画"})[0], 3)

    def test_image_rule_worth_three(self):
        self.assertEqual(
            scores({"bookSourceUrl": "https://x.example/",
                    "ruleContent": {"image": "##img##"}})[0], 3)

    def test_image_style_full_worth_one(self):
        base = scores({"bookSourceUrl": "https://x.example/"})[0]
        self.assertEqual(
            scores({"bookSourceUrl": "https://x.example/",
                    "ruleContent": {"imageStyle": "FULL"}})[0], base + 1)

    def test_novel_text_hint_worth_three(self):
        # 用基线对比而不是写死数字：基线本身就含「声明类型缺省 0」的 1 分
        base = scores({"bookSourceUrl": "https://x.example/"})[1]
        self.assertEqual(
            scores({"bookSourceUrl": "https://x.example/",
                    "bookSourceName": "某某书屋"})[1], base + 3)

    def test_declared_type_bonus_goes_to_its_own_side(self):
        self.assertEqual(scores({"bookSourceUrl": "https://x.example/",
                                 "bookSourceType": 2})[0], 1)
        self.assertEqual(scores({"bookSourceUrl": "https://x.example/",
                                 "bookSourceType": 0})[1], 1)


class HomepageSignalTests(unittest.TestCase):
    """首页实测信号。纯函数，喂 HTML 即可。"""

    @staticmethod
    def _html(title, path, n=6):
        links = "".join('<a href="/%s/%d">x</a>' % (path, i) for i in range(n))
        return "<html><head><title>%s</title></head><body>%s</body></html>" % (title, links)

    def test_title_and_many_manga_links(self):
        manga, novel, why = homepage_signals(self._html("某某漫画", "comic"))
        self.assertGreaterEqual(manga, 5)    # 标题 3 + 链接 2
        self.assertEqual(novel, 0)
        self.assertTrue(why)

    def test_title_and_many_novel_links(self):
        manga, novel, _why = homepage_signals(self._html("某某书屋", "novel"))
        self.assertEqual(manga, 0)
        self.assertGreaterEqual(novel, 5)

    def test_link_threshold_is_five(self):
        # 少一条就不该计分：阈值 5 是刻意的，别被顺手改成 4
        _m, _n, _w = homepage_signals(self._html("无特征标题", "comic", n=4))
        just_below = homepage_signals(self._html("无特征标题", "comic", n=4))[0]
        just_at = homepage_signals(self._html("无特征标题", "comic", n=5))[0]
        self.assertEqual(just_below, 0)
        self.assertEqual(just_at, 2)

    def test_empty_html_returns_zero_without_raising(self):
        self.assertEqual(homepage_signals(""), (0, 0, []))


class CombineTests(unittest.TestCase):
    """静态 + 首页合并：两路相加后再判阈。"""

    def test_static_alone_is_not_enough(self):
        self.assertEqual(combine_type({"bookSourceUrl": "https://x.example/"},
                                      None)[0], KEEP)

    def test_weak_static_plus_strong_homepage_reclassifies(self):
        # 静态无信号，但首页实测 5 分 → 改判。这条守的是「实测优先于声明」
        self.assertEqual(
            combine_type({"bookSourceUrl": "https://x.example/"},
                         (5, 0, ["首页标题/关键词含漫画特征"]))[0], MANGA)

    def test_homepage_novel_signal_reclassifies_to_novel(self):
        self.assertEqual(
            combine_type({"bookSourceUrl": "https://x.example/"},
                         (0, 5, []))[0], NOVEL)

    def test_opposing_homepage_signal_can_cancel_out(self):
        # 静态 3:1（域名漫画特征 3，声明缺省给小说 1），首页再给小说 2 → 3:3 平手
        source = {"bookSourceUrl": "https://manhua.example/"}
        self.assertEqual(scores(source), (3, 1))
        self.assertEqual(combine_type(source, (0, 2, []))[0], KEEP)

    def test_opposing_homepage_signal_can_flip_the_verdict(self):
        # 同样的静态证据，首页给小说 3 → 3:4 → 改判成小说。
        # 首页实测能推翻静态判断，正是「实测优先于声明」的落点
        source = {"bookSourceUrl": "https://manhua.example/"}
        self.assertEqual(combine_type(source, (0, 3, []))[0], NOVEL)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → 跑 → 确认变红 → 还原），**跑的时候带 `python -B`**。
#
#  M1  判阈改成非严格（`manga > novel` → `manga >= novel`）
#        → ThresholdTests.test_tie_does_not_reclassify 红
#  M2  阈值 3 → 1
#        → ConservatismTests.test_declared_type_alone_is_not_enough 红
#  M3  域名漫画特征权重 3 → 2（等长替换）
#        → ThresholdTests.test_exactly_three_points_is_enough
#          SignalWeightTests.test_manga_host_hint_worth_three
#          CombineTests.test_opposing_homepage_signal_can_cancel_out 红
#  M4  首页链接阈值 5 → 4（等长替换）
#        → HomepageSignalTests.test_link_threshold_is_five 红
#
#  **必须带 -B（或不写字节码）**：M2/M3/M4 都是等长替换——文件 size 不变。
#  第一次跑时没带 -B，三条报出来的失败用例**全都是同一个名字**（且与变异无关），
#  看着像"变红了"其实跑的是上一轮的编译产物。等长变异 + 字节码缓存会让验证
#  给出可信度很低的假结果，比不跑更危险。详见
#  skills/legado-source-lessons 的变异测试一节。

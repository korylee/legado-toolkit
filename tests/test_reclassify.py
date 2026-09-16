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

import asyncio
import unittest
from unittest import mock

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

    def test_rule_content_image_is_not_a_signal(self):
        """`ruleContent.image` 不是信号——它是个**不存在的字段**。

        Legado 的 `ContentRule` 没有 `image`（图片走 `content` 规则本身，
        见 `looks_like_image_rule`），本项目也从不写它：实测库里的 3861 条源，
        `ruleContent.image` 非空的是 **0 条**。

        所以原来那条 `image += 3` 从来没执行过，它是死代码；留着它的危险不是
        「算错」，而是**有人看到它以为存在这个信号**，照着它去改权重或加规则。
        这条测试把「不读它」钉死：带这个字段的源必须和不带时判得一样。
        """
        with_field = {"bookSourceUrl": "https://x.example/",
                      "ruleContent": {"image": "##img##"}}
        without = {"bookSourceUrl": "https://x.example/"}
        self.assertEqual(scores(with_field), scores(without))
        self.assertEqual(judge(with_field), judge(without))
        self.assertEqual(judge(with_field), KEEP)

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


class SystemGroupFeedbackTests(unittest.TestCase):
    """group 里的**系统标签**不能当类型信号——它是按 `bookSourceType` 生成的。

    `organizer.group_title`（与 `store._system_group_for`）会把声明的类型写成
    分组标签，形如 `📖小说,待验证`。而 `_text_of` 原本把整个 group 当文本读，于是：

        类型错(默认 0) → group 写成「📖小说」 → 命中 "小说" → novel +3
                      → 单独就到阈值 3 → 再判成小说   ↺

    **错的标签自我固化，永远纠正不过来**。实测：命中 "小说" 的 3540 条源里，
    3506 条的 group 带着系统类型标签；剥掉系统标签后 119 条改判（其中 32 条从
    「小说」纠正为「漫画」）。这是 lessons §十八「判定输入里不能放我们自己写进去的结论」的实测根因——
    原判断说的「泛词太泛」并不成立（`阅读`/`txt` 各自只影响 4~5 条）。

    守两条：系统标签**不算证据**，用户标签**仍然算**（最后一条是反向断言，
    防止修过头把真信号一起丢掉）。
    """

    def test_system_type_tag_is_not_novel_evidence(self):
        # 只有系统标签的源：原本 novel = 3(标签) + 1(声明缺省) = 4 → 判成小说
        self.assertEqual(
            judge({"bookSourceUrl": "https://x.example/",
                   "bookSourceGroup": "📖小说,待验证"}), KEEP)

    def test_system_type_tag_is_not_manga_evidence(self):
        # 反方向同样成立：🎨漫画 也是生成的，不能反过来当漫画证据
        self.assertEqual(
            judge({"bookSourceUrl": "https://x.example/",
                   "bookSourceGroup": "🎨漫画,可用"}), KEEP)

    def test_wrong_group_tag_cannot_suppress_a_real_manga_signal(self):
        # 真实形状（`鬼罗丽漫画` / `天天看` 这批）：漫画站被错标成 0，group 于是
        # 写成「📖小说」，反过来把名称里的「漫画」信号压平（3:4 → 判成小说）
        self.assertEqual(
            judge({"bookSourceUrl": "https://x.example/",
                   "bookSourceName": "鬼罗丽漫画",
                   "bookSourceGroup": "📖小说,可用"}), MANGA)

    def test_user_tags_still_count(self):
        # 反向断言：用户自己打的标签是**真信号**，不能跟着系统标签一起丢。
        # 「漫画」二字不是系统标签（系统标签是 `🎨漫画` 这种带前缀的）
        self.assertEqual(
            judge({"bookSourceUrl": "https://x.example/",
                   "bookSourceGroup": "我的收藏,漫画"}), MANGA)


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


class DiagnoseWiringTests(unittest.TestCase):
    """`diagnose_source` 必须走那张共享的状态码判定表（`classify_http_status`）。

    **测接线**：只测 `classify_http_status` 本身的话，把 diagnose 改回一份私有
    实现照样全绿——**而它与域名探测的分叉正是这么来的**（实测在 503 / 404 /
    500 / 登录页四类输入上，两侧结论已经不一致）。

    判法：把共享表打桩，看 diagnose 的分桶会不会跟着变。
    （`classify_http_status` 是在函数体内 import 的，所以打在 `core.checker` 上
      才有用，打 `core.reclassify` 上的同名属性不管用。）
    """

    async def _fake_get(session, url, timeout=8.0, method="GET",
                        headers=None, body=""):
        return 200, "<html>正常</html>", ""

    SRC = {"bookSourceUrl": "https://a.com", "bookSourceName": "x"}

    def test_auth_bucket_comes_from_the_shared_table(self):
        from core import reclassify as R

        with mock.patch.object(R, "_get", self._fake_get), \
             mock.patch("core.checker.classify_http_status", return_value="auth"):
            res = asyncio.run(R.diagnose_source(None, self.SRC))
        self.assertEqual(res["bucket"], "需验证")

    def test_dead_bucket_comes_from_the_shared_table(self):
        """反向断言：表说 dead → 归「死站」，不能一律归「需验证」。

        少了这条，「把 diagnose 改成恒返回需验证」也会全绿。
        （这条不打桩——让它走真表：404 在那里判 DEAD。）
        """
        from core import reclassify as R

        async def get_404(session, url, timeout=8.0, method="GET",
                          headers=None, body=""):
            return 404, "", ""

        with mock.patch.object(R, "_get", get_404):
            res = asyncio.run(R.diagnose_source(None, self.SRC))
        self.assertEqual(res["bucket"], "死站")


class DiagnoseBucketTests(unittest.TestCase):
    """`diagnose_source` 的五个归因桶——决定用户下一步动作的那五个结论。

    这块原先**零覆盖**（整函数约 100 行，`grep` 全部 32 个测试文件零引用），
    而它产出「死站 / 需验证 / 站点转型 / 疑似可用 / 规则漂移」，直接对应
    「淘汰 / 人工复检 / 改类型 / 复检 / 进 AI 修复队列」五种动作。

    **完全离线**：把 `_get` 打桩成固定页面表即可（它只依赖这一个出口）。

    每条**单独写**而不是合成一条大用例：合成的话第一个桶不对后面就不执行了，
    另外几个桶改坏看不出来。
    """

    SRC = {"bookSourceUrl": "https://a.com", "bookSourceName": "某某漫画",
           "bookSourceType": 0,
           "searchUrl": "https://a.com/s?q={{key}}",
           "ruleSearch": {"bookList": "class.item"}}

    HOME = "https://a.com"

    def _run(self, pages, source=None):
        from core import reclassify as R

        async def fake_get(session, url, timeout=8.0, method="GET",
                           headers=None, body=""):
            return pages.get(url, (404, "", ""))

        with mock.patch.object(R, "_get", fake_get):
            return asyncio.run(R.diagnose_source(None, dict(source or self.SRC)))

    def _search_url(self, kw="海贼王"):
        from core.checker import parse_search_request
        return parse_search_request(self.SRC["searchUrl"], kw)[0]

    def test_no_url_is_other(self):
        res = self._run({}, source={"bookSourceName": "没有域名的源"})
        self.assertEqual(res["bucket"], "其他")

    def test_unreachable_is_dead(self):
        res = self._run({self.HOME: (None, "", "dns")})
        self.assertEqual(res["bucket"], "死站")

    def test_no_search_rule_is_rule_drift(self):
        """可达但没有搜索规则（仅发现源）→ 规则漂移，不是死站。"""
        src = {"bookSourceUrl": "https://a.com", "bookSourceName": "仅发现源"}
        res = self._run({self.HOME: (200, "<html>首页</html>", "")}, source=src)
        self.assertEqual(res["bucket"], "规则漂移")
        self.assertIn("无搜索规则", res["attribution"])

    def test_search_unparseable_is_rule_drift(self):
        """可达、有搜索规则，但 bookList 跑不出节点 → 规则漂移。"""
        res = self._run({self.HOME: (200, "<html>首页</html>", ""),
                         self._search_url(): (200, "<html>没有结果</html>", "")})
        self.assertEqual(res["bucket"], "规则漂移")

    def test_type_mismatch_is_site_transformed(self):
        """搜索解析成功，但**实测类型与声明不符** → 站点转型。"""
        res = self._run({
            self.HOME: (200, "<html>首页</html>", ""),
            self._search_url(): (200, '<div class="item">海贼王</div>', ""),
        })
        self.assertEqual(res["bucket"], "站点转型")
        self.assertIn("实测类型", res["attribution"])

    def test_type_match_is_probably_usable(self):
        """反向断言：同样的证据、但**声明与实测一致** → 疑似可用。

        少了这条，把「类型不符才报转型」写成「一律报转型」也会全绿。
        实测类型是漫画（名称含「漫画」），所以声明也写成 2。
        """
        src = dict(self.SRC, bookSourceType=2)
        res = self._run({
            self.HOME: (200, "<html>首页</html>", ""),
            self._search_url(): (200, '<div class="item">海贼王</div>', ""),
        }, source=src)
        self.assertEqual(res["bucket"], "疑似可用")


# ---------------------------------------------------------------- 变异记录
#
# DiagnoseWiringTests（2026-09-16 收拢状态码判定表）：
#  M42  `diagnose_source` 自己另写一份（`status in (401,403,429,503)`）
#         → test_auth_bucket_comes_from_the_shared_table 与
#           test_dead_bucket_comes_from_the_shared_table **两条都红**
#         ⚠️ 第一版变异引用了 `ANTI_BOT_MARKERS`——而那个导入**刚被这次改动删掉**，
#         于是报的是 `NameError`（error 而非 failure）。红的理由不指向被测对象，
#         等于没测。改成不依赖导入的等价写法才有效。
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
#  M5  把 `ruleContent.image` 的 +3 信号加回去（= 还原被删的死分支）
#        → SignalWeightTests.test_rule_content_image_is_not_a_signal 红
#        （**只有这一条能拦住它**：该分支在真实数据上从不执行，全量跑一遍
#           3861 条也看不出任何变化——不加这条断言，死分支随时会被重新引入）
#  M6  `_text_of` 改回读整个 group（= 还原那个循环）
#        → SystemGroupFeedbackTests 的那三条红（反向断言那条仍绿——它守的是
#           「别修过头」，两种口径下都该通过）
#  M7  `_text_of` 把 group 整个丢掉（**修过头**）
#        → SystemGroupFeedbackTests.test_user_tags_still_count 红
#        （M6 与 M7 是一对：只做 M6 会以为「全丢」也行，M7 证明用户标签得留着）
#
#  **必须带 -B（或不写字节码）**：M2/M3/M4 都是等长替换——文件 size 不变。
#  第一次跑时没带 -B，三条报出来的失败用例**全都是同一个名字**（且与变异无关），
#  看着像"变红了"其实跑的是上一轮的编译产物。等长变异 + 字节码缓存会让验证
#  给出可信度很低的假结果，比不跑更危险。详见
#  skills/legado-source-lessons 的变异测试一节。

# -*- coding: utf-8 -*-
"""找重复源（只读清单）的离线用例。

要守的是**判据本身**：这份清单是用来指导删源的，判宽了会让人删掉还能用的源，
判窄了这份清单就没用（库里的重复一条都找不出来）。三种输入各要一组：

  - 只是署名/排序/备注不同（真重复，地址不同）→ 必须成组
  - 规则本身不同（比如 searchUrl 换了）→ 必须**不**成组
  - 域名不同、规则相同（镜像站）→ 成组，但标成"跨域名"，不当作可自动处理的那类

外加「建议保留哪条」的优先级：**能用 > 地址干净 > 导入早**。
"""

from __future__ import annotations

import unittest

from core.dups import find_dup_groups, host_of, render_report, rule_signature, summarize


def src(url: str, name: str = "站", **extra) -> dict:
    base = {"bookSourceUrl": url, "bookSourceName": name,
            "bookSourceType": 0, "searchUrl": "/s?q={{key}}",
            "ruleSearch": {"bookList": "class.item"},
            "ruleToc": {"chapterList": "class.ch"}, "ruleContent": {"content": "id.c"}}
    base.update(extra)
    return base


class SignatureTests(unittest.TestCase):
    """哪些字段参与比较——这是整套判据的地基。"""

    def test_only_non_behavioral_fields_are_excluded(self) -> None:
        a = src("https://a.com#x", customOrder=1, lastUpdateTime=1, respondTime=9,
                bookSourceGroup="📖小说", bookSourceComment="来自某某分享")
        b = src("https://a.com#y", customOrder=2, lastUpdateTime=2, respondTime=8,
                bookSourceGroup="别的组", bookSourceComment="")
        self.assertEqual(rule_signature(a), rule_signature(b),
                         "地址/排序/时间/耗时/分组/备注都不该参与比较")

    def test_rule_change_makes_it_a_different_source(self) -> None:
        a = src("https://a.com")
        b = src("https://a.com")
        b["searchUrl"] = "/search?q={{key}}"
        self.assertNotEqual(rule_signature(a), rule_signature(b))

    def test_type_and_switches_count(self) -> None:
        a = src("https://a.com")
        b = src("https://a.com", bookSourceType=2)
        self.assertNotEqual(rule_signature(a), rule_signature(b))


class HostTests(unittest.TestCase):
    def test_signature_path_port_and_case_are_normalized(self) -> None:
        self.assertEqual(host_of("https://A.com/x#署名"), "a.com")
        self.assertEqual(host_of("http://a.com:8080/"), "a.com")
        # 端口写空了的畸形地址（库里真实存在）也要归一到同一个域名
        self.assertEqual(host_of("http://a.com:"), "a.com")


class GroupingTests(unittest.TestCase):
    def test_same_host_variants_are_one_group(self) -> None:
        groups = find_dup_groups([
            src("https://a.com#guaner", "随心看吧"),
            src("https://a.com#♤guaner", "随心看吧"),
            src("https://a.com#", "随心看吧", bookSourceComment="分享自xx"),
            src("https://a.com", "随心看吧", customOrder=99),
        ])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["same_host"])
        self.assertEqual(groups[0]["redundant"], 3)
        self.assertEqual(len(groups[0]["members"]), 4)

    def test_single_source_is_not_a_group(self) -> None:
        self.assertEqual(find_dup_groups([src("https://a.com")]), [])

    def test_different_hosts_are_flagged_as_cross_host(self) -> None:
        groups = find_dup_groups([src("https://a.com"), src("https://a.net")])
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["same_host"], "跨域名的不该当成可自动处理的那类")
        self.assertEqual(sorted(groups[0]["hosts"]), ["a.com", "a.net"])

    def test_malformed_port_does_not_look_like_another_host(self) -> None:
        groups = find_dup_groups([src("http://a.com:80"), src("http://a.com:")])
        self.assertTrue(groups[0]["same_host"], "同一个域名的两种端口写法不该算跨域名")


class KeepSuggestionTests(unittest.TestCase):
    """「建议保留」的优先级：能用 > 地址干净 > 导入早。"""

    def _keep_url(self, sources, checks=None):
        g = find_dup_groups(sources, checks)[0]
        return [m for m in g["members"] if m["order"] == g["keep"]][0]["url"]

    def test_cleaner_url_wins_when_both_work(self) -> None:
        self.assertEqual(
            self._keep_url([src("https://a.com#♤x"), src("https://a.com")]),
            "https://a.com")

    def test_a_working_source_beats_a_cleaner_dead_one(self) -> None:
        """镜像站那类：一个通一个死时，**能用的优先**，地址干净与否让位。"""
        from core.loader import _normalize_url
        checks = {_normalize_url("https://a.com#♤x"): {"health": "ok", "quality_stars": 5},
                  _normalize_url("https://a.com"): {"health": "dead", "quality_stars": 0}}
        self.assertEqual(
            self._keep_url([src("https://a.com"), src("https://a.com#♤x")], checks),
            "https://a.com#♤x")

    def test_earliest_import_wins_when_nothing_else_differs(self) -> None:
        self.assertEqual(
            self._keep_url([src("https://a.com#1"), src("https://a.com#2")]),
            "https://a.com#1")


class ReportTests(unittest.TestCase):
    def test_report_states_the_criteria_and_the_two_categories(self) -> None:
        """清单是给人做删除决策的：判据和"哪类能自动处理、哪类要人看"都要写在纸上。"""
        # 两组**规则不同**的源：一组同域名重复、一组跨域名（别写成一模一样的规则，
        # 那样四条会并成一组，测的就不是"两类分别统计"了）
        sources = [src("https://a.com#x"), src("https://a.com#y"),
                   src("https://b.com", searchUrl="/other?q={{key}}"),
                   src("https://b.net", searchUrl="/other?q={{key}}")]
        groups = find_dup_groups(sources)
        text = render_report(groups, len(sources), source_label="测试")
        self.assertIn("只读", text)
        self.assertIn("同一域名", text)
        self.assertIn("跨域名", text)
        self.assertIn("不改任何数据", text)
        s = summarize(groups, len(sources))
        self.assertEqual(s["same_host_redundant"], 1)
        self.assertEqual(s["cross_host_groups"], 1)


if __name__ == "__main__":
    unittest.main()

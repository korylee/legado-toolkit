# -*- coding: utf-8 -*-
"""找重复源（只读清单）的离线用例。

要守的是**判据本身**：这份清单是用来指导删源的，判宽了会让人删掉还能用的源，
判窄了这份清单就没用（库里的重复一条都找不出来）。判据是**四个核心字段**
（searchUrl / ruleSearch.bookList / ruleToc.chapterList / ruleContent.content），
三种输入各要一组：

  - 只是署名/元数据/转发噪音不同（真重复，地址不同）→ 必须成组
  - 核心字段不同（换版式：searchUrl 换路径、列表/目录/正文选择器换）→ 必须**不**成组
  - 域名不同、核心字段相同（镜像站）→ 成组，但标成"跨域名"，不当作可自动处理的那类

外加「建议保留哪条」的优先级：**能用 > 地址干净 > 导入早**。
"""

from __future__ import annotations

import unittest

from core.dups import (find_dup_groups, find_groups, find_host_groups,
                       find_mergeable_groups, find_mirror_groups,
                       find_name_groups, host_of,
                       rule_signature, same_site, site_key)


def src(url: str, name: str = "站", **extra) -> dict:
    base = {"bookSourceUrl": url, "bookSourceName": name,
            "bookSourceType": 0, "searchUrl": "/s?q={{key}}",
            "ruleSearch": {"bookList": "class.item"},
            "ruleToc": {"chapterList": "class.ch"}, "ruleContent": {"content": "id.c"}}
    base.update(extra)
    return base


class SignatureTests(unittest.TestCase):
    """哪些字段参与比较——这是整套判据的地基（四个核心字段）。"""

    def test_metadata_and_forwarding_noise_do_not_count(self) -> None:
        """地址署名/名称/排序/时间/分组/备注/checkKeyWord 都不是行为。"""
        a = src("https://a.com#x", bookSourceName="名字一", customOrder=1,
                weight=0, lastUpdateTime=1, respondTime=9, concurrentRate=None,
                enabledExplore=False, header=None, loginUrl="",
                bookSourceGroup="📖小说", bookSourceComment="来自某某分享")
        b = src("https://a.com#y", bookSourceName="名字二", customOrder=2,
                weight=-5, lastUpdateTime=2, respondTime=8, concurrentRate=0,
                enabledExplore=True, header={}, loginUrl=None,
                bookSourceGroup="别的组", bookSourceComment="")
        self.assertEqual(rule_signature(a), rule_signature(b),
                         "元数据与转发噪音不该把同一条源拆散")

    def test_core_rule_change_makes_it_a_different_source(self) -> None:
        """四个核心字段任何一个变了都是换源——po5 #77 那类改版要拦住。"""
        cases = [
            {"searchUrl": "/other?q={{key}}"},
            {"ruleSearch": {"bookList": "class.other"}},
            {"ruleToc": {"chapterList": "class.other"}},
            {"ruleContent": {"content": "id.other"}},
        ]
        for extra in cases:
            self.assertNotEqual(
                rule_signature(src("https://a.com")),
                rule_signature(src("https://a.com", **extra)),
                "核心字段 %s 变了必须分家" % list(extra)[0])

    def test_search_url_relative_forms_are_one_request(self) -> None:
        """App 拿 bookSourceUrl 当 baseUrl 补全 searchUrl（WebBook.searchBookAwait
        → AnalyzeUrl → NetworkUtils.getAbsoluteURL）：三种写法发出同一个请求。"""
        a = src("https://a.com", searchUrl="https://a.com/so.php?q={{key}}")
        b = src("https://a.com#", searchUrl="so.php?q={{key}}")
        c = src("https://a.com/", searchUrl="/so.php?q={{key}}")
        self.assertEqual(rule_signature(a), rule_signature(b))
        self.assertEqual(rule_signature(a), rule_signature(c))

    def test_search_url_option_tail_is_stripped(self) -> None:
        """`,{json}` 选项是 App 才懂的东西（AGENTS #20），不参与比较。"""
        a = src("https://a.com", searchUrl="/so.php?q={{key}}")
        b = src("https://a.com",
                searchUrl='/so.php?q={{key}},{"webView":true}')
        self.assertEqual(rule_signature(a), rule_signature(b))

    def test_blank_rule_variants_are_the_same(self) -> None:
        """空值三态（null / "" / 缺键）行为相同——po5.net 三条转发里全有。"""
        a = src("https://a.com")
        b = src("https://a.com", ruleToc={"chapterList": "class.ch", "isVip": ""},
                ruleContent={"content": "id.c", "replaceRegex": None})
        self.assertEqual(rule_signature(a), rule_signature(b))

    def test_check_keyword_does_not_split_duplicates(self) -> None:
        """checkKeyWord 只服务校验/调试（上游 App 不拿它搜书），不在核心字段里。"""
        a = src("https://www.bequw.com")
        b = src("https://www.bequw.com/#xsw8093",
                ruleSearch={"bookList": "class.item", "checkKeyWord": "剑来"})
        self.assertEqual(rule_signature(a), rule_signature(b))
        groups = find_dup_groups([a, b])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["redundant"], 1)

    def test_po5_three_forwards_are_one_group(self) -> None:
        """po5.net 三条转发（裸域 / # / #🎃）核心字段一致，必须成组；
        #77 是改版源，核心字段不同，留在组外。"""
        forwards = [
            src("https://www.po5.net", name="五二书库",
                searchUrl="/so/search.php?q={{key}}"),
            src("https://www.po5.net#", name="五二书库",
                searchUrl="so/search.php?q={{key}}"),
            src("https://www.po5.net#🎃", name="五二书库🎃",
                searchUrl="https://www.po5.net/so/search.php?q={{key}}",
                ruleContent={"content": "id.c", "nextContentUrl": ""}),
        ]
        revamp = src("https://www.po5.net#77", name="52书库",
                     searchUrl="/so/search.php?q={{key}}",
                     ruleSearch={"bookList": ".content article"},
                     ruleToc={"chapterList": ".list .mulu a"},
                     ruleContent={"content": "#nr1 p:not(:last-of-type)@textNodes"})
        groups = find_mergeable_groups(forwards + [revamp])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["members"]), 3, "三条转发成一组")
        self.assertTrue(all(m["url"] != "https://www.po5.net#77"
                            for m in groups[0]["members"]),
                        "改版源不该混进重复组")


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

    def test_name_difference_still_groups(self) -> None:
        """改名不等于换源：同规则、不同名称也要成组（比较里名称不参与）。"""
        groups = find_dup_groups([src("https://a.com", "名字一"),
                                  src("https://a.com#x", "名字二")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["redundant"], 1)
        self.assertEqual(len(groups[0]["members"]), 2)

    def test_different_port_is_cross_site(self) -> None:
        groups = find_dup_groups([src("http://a.com:8080/x"), src("http://a.com/y")])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["same_host"])
        self.assertFalse(groups[0]["same_site"])

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

    def test_verified_content_beats_cleaner_unverified_url(self) -> None:
        from core.loader import _normalize_url
        checks = {_normalize_url("https://a.com#x"): {"health": "ok", "content_ok": True},
                  _normalize_url("https://a.com"): {"health": "ok"}}
        self.assertEqual(
            self._keep_url([src("https://a.com"), src("https://a.com#x")], checks),
            "https://a.com#x")

    def test_a_working_source_beats_a_cleaner_dead_one(self) -> None:
        """镜像站那类：一个通一个死时，**能用的优先**，地址干净与否让位。"""
        from core.loader import _normalize_url
        checks = {_normalize_url("https://a.com#♤x"): {"health": "ok", "content_ok": True},
                  _normalize_url("https://a.com"): {"health": "dead", "content_ok": None}}
        self.assertEqual(
            self._keep_url([src("https://a.com"), src("https://a.com#♤x")], checks),
            "https://a.com#♤x")

    def test_earliest_import_wins_when_nothing_else_differs(self) -> None:
        self.assertEqual(
            self._keep_url([src("https://a.com#1"), src("https://a.com#2")]),
            "https://a.com#1")


class HostAndNameGroupTests(unittest.TestCase):
    """B/C 两档只做归并；规则是否相同、怎么处理由 API/前端决定。"""

    def test_rule_groups_expose_kind_and_key(self) -> None:
        groups = find_dup_groups([src("https://a.com"), src("https://a.com#x")])
        self.assertEqual(groups[0]["kind"], "rules")
        self.assertEqual(groups[0]["key"], groups[0]["signature"])

    def test_host_groups_merge_different_rules_by_host(self) -> None:
        a = src("https://a.com", name="A")
        b = src("https://a.com/x", name="B", searchUrl="/other?q={{key}}")
        groups = find_host_groups([a, b])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "host")
        self.assertEqual(groups[0]["key"], "a.com")
        self.assertEqual(len(groups[0]["members"]), 2)

    def test_host_groups_ignore_empty_host(self) -> None:
        # 没有 URL 的源不该因为 host="" 被并成一组
        self.assertEqual(find_host_groups([src(""), src("")]), [])

    def test_name_groups_merge_across_hosts(self) -> None:
        groups = find_name_groups([src("https://a.com", name="同名"),
                                   src("https://b.net", name="同名")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "name")
        self.assertEqual(groups[0]["key"], "同名")
        self.assertEqual(sorted(groups[0]["hosts"]), ["a.com", "b.net"])

    def test_name_groups_ignore_empty_or_whitespace_names(self) -> None:
        self.assertEqual(
            find_name_groups([src("https://a.com", name=""),
                              src("https://b.net", name="   ")]), [])

    def test_find_groups_combines_requested_kinds(self) -> None:
        a = src("https://a.com", name="孤儿名")
        b = src("https://a.com", name="另一名", searchUrl="/b?q={{key}}")
        host_only = find_groups([a, b], kinds=("host",))
        self.assertTrue(host_only)
        self.assertTrue(all(g["kind"] == "host" for g in host_only))
        self.assertEqual(find_groups([a, b], kinds=("rules",)), [])


class SiteKeyTests(unittest.TestCase):
    def test_default_ports_and_empty_port_are_normalized(self):
        self.assertEqual(site_key("http://a.com:"), "a.com")
        self.assertEqual(site_key("HTTP://A.com:80"), "a.com")
        self.assertEqual(site_key("https://a.com:443"), "a.com")
        self.assertEqual(site_key("http://a.com:8080"), "a.com:8080")

    def test_scheme_is_not_part_of_site_key(self):
        self.assertTrue(same_site("http://a.com", "https://a.com"))

    def test_non_default_port_or_host_is_a_different_site(self):
        self.assertFalse(same_site("http://a.com", "http://a.com:8080"))
        self.assertFalse(same_site("http://a.com", "http://b.com"))


class MergeableMirrorGroupTests(unittest.TestCase):
    def test_mergeable_requires_same_site_and_same_rules(self):
        groups = find_mergeable_groups([src("https://a.com/x"), src("https://a.com/y")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "mergeable")
        self.assertTrue(groups[0]["same_host"])
        self.assertEqual(
            find_mergeable_groups([src("https://a.com"), src("https://b.net")]), [])

    def test_non_default_port_splits_mergeable(self):
        self.assertEqual(len(find_mergeable_groups([
            src("http://a.com:8080/x"), src("http://a.com:8080/y")])), 1)
        self.assertEqual(find_mergeable_groups([
            src("http://a.com:8080/x"), src("http://a.com/y")]), [])

    def test_mirror_is_cross_site_only(self):
        groups = find_mirror_groups([src("https://a.com"), src("https://b.net")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "mirror")
        self.assertFalse(groups[0]["same_host"])
        self.assertEqual(find_mirror_groups([
            src("https://a.com/x"), src("https://a.com/y")]), [])

    def test_find_groups_can_request_mergeable_and_mirror(self):
        sources = [src("https://a.com/x"), src("https://a.com/y"), src("https://b.net/z")]
        got = find_groups(sources, kinds=("mergeable", "mirror"))
        self.assertEqual(sorted(g["kind"] for g in got), ["mergeable", "mirror"])


if __name__ == "__main__":
    unittest.main()

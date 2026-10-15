# -*- coding: utf-8 -*-
"""Web 导入接口（/api/import）的安全导入三分类测试。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

_TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)


def make_source(url: str, search_url: str = "https://example.com/search?q={{key}}") -> dict:
    return {
        "bookSourceName": "示例书源",
        "bookSourceUrl": url,
        "bookSourceType": 0,
        "searchUrl": search_url,
        "ruleSearch": {"bookList": ".book"},
    }


class _ImportCase(unittest.TestCase):
    """共用夹具：临时数据目录 + 一个 Store。

    提成基类是为了让「安全导入三分类」与「输入校验」两组用例共用它，
    而不是各自抄一份 setUp。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.temp_dir.name

        from core.store import Store
        self.store = Store()

    def tearDown(self) -> None:
        self.store.close()
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        self.temp_dir.cleanup()

    def _import(self, sources) -> dict:
        from backend.schemas import ImportBody
        from backend.api.imports import import_sources
        return import_sources(ImportBody(content=json.dumps(sources), source="t.json"), self.store)

    def _raw(self, content: str) -> dict:
        """原样喂字符串（校验用例要喂坏 JSON，不能经过 json.dumps）。"""
        from backend.schemas import ImportBody
        from backend.api.imports import import_sources
        return import_sources(ImportBody(content=content), self.store)


class ImportApiTests(_ImportCase):
    def test_new_url_is_marked_pending(self) -> None:
        result = self._import([make_source("https://new.example")])
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["conflict_count"], 0)
        source = self.store.get_source("https://new.example")
        self.assertIn("待验证", source.get("bookSourceGroup", ""))
        self.assertIn("规则不完整", source.get("bookSourceGroup", ""))

    def test_import_strips_url_fragment_and_compares_on_the_base_url(self) -> None:
        """导入身份去 fragment：`X##甲` 入库存 `X`，再导入 `X##乙` 与那条 `X` 比规则。"""
        first = self._import([make_source("https://sig.example##分享者甲")])
        self.assertEqual(first["new_count"], 1)
        stored = self.store.get_source("https://sig.example")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["bookSourceUrl"], "https://sig.example")

        same = self._import([make_source("https://sig.example##分享者乙")])
        self.assertEqual(same["duplicate_count"], 1)
        self.assertEqual(same["new_count"], 0)
        self.assertEqual(self.store.count_sources(), 1, "同一条源不该占两行")

        changed = self._import([make_source("https://sig.example##分享者丙",
                                            "https://example.com/other?q={{key}}")])
        self.assertEqual(changed["conflict_count"], 1)
        self.assertEqual(changed["new_count"], 0)
        self.assertEqual(self.store.count_sources(), 1)

    def test_preview_counts_incomplete_rules_without_importing(self) -> None:
        from backend.api.imports import preview_import
        from backend.schemas import ImportBody

        incomplete = make_source("https://incomplete.example")
        complete = make_source("https://complete.example")
        complete["ruleSearch"] = {
            "bookList": ".book", "name": ".name", "bookUrl": ".item@href",
        }
        complete["ruleToc"] = {"chapterList": ".chapter"}
        complete["ruleContent"] = {"content": ".content"}
        result = preview_import(ImportBody(content=json.dumps([incomplete, complete])),
                                st=self.store)

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["counts"]["new"], 2)
        self.assertEqual(result["counts"]["rules_incomplete"], 1)
        self.assertEqual([item["status"] for item in result["items"]], ["new", "new"])
        self.assertEqual([item["rules_incomplete"] for item in result["items"]], [True, False])
        self.assertIsNone(self.store.get_source("https://incomplete.example"))

    def test_preview_reuses_import_fingerprint_conflict_policy(self) -> None:
        from backend.api.imports import preview_import
        from backend.schemas import ImportBody

        current = make_source("https://existing.example")
        self._import([current])
        changed = make_source("https://existing.example",
                              "https://example.com/changed?q={{key}}")

        keep = preview_import(ImportBody(content=json.dumps([changed])), st=self.store)
        overwrite = preview_import(
            ImportBody(content=json.dumps([changed]), conflict_strategy="overwrite"),
            st=self.store,
        )

        self.assertEqual(keep["items"][0]["status"], "conflict")
        self.assertEqual(overwrite["items"][0]["status"], "update")
        self.assertEqual(self.store.get_source("https://existing.example")["searchUrl"],
                         current["searchUrl"])

    def test_preview_reuses_import_conflict_and_override_policy(self) -> None:
        from backend.api.imports import preview_import
        from backend.schemas import ImportBody

        current = make_source("https://existing.example")
        self._import([current])
        changed = make_source("https://existing.example",
                              "https://example.com/changed?q={{key}}")

        keep = preview_import(ImportBody(content=json.dumps([changed])), st=self.store)
        overwrite = preview_import(
            ImportBody(content=json.dumps([changed]), conflict_strategy="overwrite"),
            st=self.store,
        )

        self.assertEqual(keep["items"][0]["status"], "conflict")
        self.assertEqual(overwrite["items"][0]["status"], "update")
        self.assertEqual(self.store.get_source("https://existing.example")["searchUrl"],
                         current["searchUrl"])

    def test_import_keeps_defaults_and_drops_unknown_tags(self) -> None:
        # 空库第一次导入也要过滤：默认标签 R18 / 正版 保留，陌生标签直接清。
        source = make_source("https://tagged.example")
        source["bookSourceGroup"] = "📖小说,✅可用,R18,正版,陌生标签"
        result = self._import([source])
        self.assertEqual(result["new_count"], 1)
        group = self.store.get_source("https://tagged.example").get("bookSourceGroup", "")
        self.assertIn("可用", group)
        self.assertIn("R18", group)
        self.assertIn("正版", group)
        self.assertNotIn("陌生标签", group)

    def test_import_keeps_tags_the_user_already_created(self) -> None:
        # 用户主动建过的标签算「已知」；导入不能把它一起清掉。
        self.store.upsert_sources([make_source("https://seed.example")])
        self.store.set_user_tags(["https://seed.example"], ["精排"])
        source = make_source("https://tagged.example")
        source["bookSourceGroup"] = "📖小说,✅可用,精排,R18,陌生标签"
        self._import([source])
        group = self.store.get_source("https://tagged.example").get("bookSourceGroup", "")
        self.assertIn("精排", group)
        self.assertIn("R18", group)
        self.assertNotIn("陌生标签", group)
    def test_same_url_same_rule_is_duplicate(self) -> None:
        self._import([make_source("https://same.example")])
        result = self._import([make_source("https://same.example")])
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["new_count"], 0)

    def test_changed_rule_is_conflict_and_not_overwritten(self) -> None:
        self._import([make_source("https://conflict.example",
                                 "https://conflict.example/search?q={{key}}")])
        result = self._import([make_source("https://conflict.example",
                                           "https://conflict.example/find?q={{key}}")])
        self.assertEqual(result["conflict_count"], 1)
        self.assertTrue(result["conflict_file"])
        source = self.store.get_source("https://conflict.example")
        self.assertEqual(source.get("searchUrl"), "https://conflict.example/search?q={{key}}")


class ImportValidationTests(_ImportCase):
    """导入接口的**输入校验**：三个 400，以及 `invalid_count`。

    这块原先**零覆盖**——实测把它改坏（条件改 `if False:`、计数改 `pass`）
    全量 512 条**一条都不红**。而它是 Web 前端导入外部书源的正门：

      - `invalid_count` 是前端展示「多少条被丢弃」的唯一来源
      - 三个 400 是 JSON 坏掉 / 格式不对时用户看到的唯一提示

    **三个 400 是三个独立分支**（空内容 / JSON 坏 / 不是数组），各写一条——
    合成一条的话，第一个分支改坏后面的就看不出来。
    """

    def _assert_400(self, content: str) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._raw(content)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_content_is_rejected(self) -> None:
        """空内容的 400 必须是**它自己那条**，不能是「JSON 解析失败」顶上的。

        **两条分支都返回 400**——只断言状态码的话，「空内容」那条改坏了
        也看不出来（空串喂给 `json.loads` 一样抛、一样 400）。实测如此。
        所以必须断言**原因文案**。这个仓库见过这个形状：
        `test_js_in_middle_reports_js_reason` 就是为它写的——
        「断言必须检查原因内容，否则这类错报不会被任何用例发现」。
        """
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._raw("   ")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("不能为空", str(ctx.exception.detail))

    def test_broken_json_is_rejected(self) -> None:
        self._assert_400("{不是 JSON")

    def test_non_array_json_is_rejected(self) -> None:
        """`{"a": 1}` 是**合法 JSON** 但不是书源数组——与上一条是两个分支。"""
        self._assert_400('{"a": 1}')

    def test_invalid_items_are_counted_not_dropped_silently(self) -> None:
        """数组里混进非 dict / 空 URL 的项 → 计进 `invalid_count`，且不进库。

        **两个分支各验一次**：非 dict 与空 URL 走的不是同一个 `continue`，
        只测一个的话另一个改坏看不出来。
        """
        res = self._import([
            "我是一个字符串，不是书源对象",           # → 非 dict 分支
            {"bookSourceName": "没有 URL 的项"},      # → 空 URL 分支
            make_source("https://ok.example"),
        ])
        self.assertEqual(res["invalid_count"], 2)
        self.assertEqual(res["new_count"], 1)
        self.assertIsNotNone(self.store.get_source("https://ok.example"))


class RepositoryApiTests(unittest.TestCase):
    @staticmethod
    def _browse_repository():
        return {
            "id": "browse-library", "name": "浏览库",
            "base_url": "https://repository.example",
            "views": [{"label": "书源", "type": "browse"}],
            "display": {"title": "name", "updated_at": "updated_at", "domain": "url"},
            "browse": {
                "list": {"path": "/catalog"},
                "search": {"path": "/search", "query_param": "q",
                           "fixed_params": {"kind": "sources"}},
                "pagination": {
                    "page_param": "page", "page_size_param": "size", "page_size": 20,
                    "response_page": "current", "response_page_size": "page_size",
                    "response_total": "count",
                },
                "response": {
                    "response_items": "results",
                    "fields": {"id": "id", "name": "name", "url": "url",
                               "updated_at": "created"},
                },
            },
            "details": {"path_template": "/source/{id}", "response_items": ""},
        }

    def test_repository_catalog_exposes_configured_views_and_field_map(self) -> None:
        from backend.api.imports import list_import_repositories
        from backend.import_repositories import _REPOSITORIES

        config = self._browse_repository()
        with mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True):
            result = list_import_repositories()
        self.assertEqual([item["id"] for item in result["items"]], [config["id"]])
        repository = result["items"][0]
        self.assertEqual([view["type"] for view in repository["views"]], ["browse"])
        self.assertEqual(repository["views"][0]["id"], "browse")
        self.assertEqual(repository["capabilities"], {"list": True, "search": True})
        self.assertEqual(repository["display"]["updated_at"], "updated_at")
        self.assertNotIn("base_url", repository)

    def test_browse_empty_query_uses_list_operation(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryBrowseRequest

        config = self._browse_repository()
        seen = {}

        async def fake_fetch(repository, url):
            seen.update(repository=repository["id"], url=url)
            return {"results": [
                        {"id": "source-1", "name": "源",
                         "url": "https://book.example",
                         "created": 1791816547.5},
                        {"id": "source-2", "name": "新源",
                         "url": "https://new-book.example",
                         "created": 1791816548},
                    ],
                    "count": 31, "current": 1, "page_size": 20}

        class Store:
            def live_fingerprints(self):
                return {"https://book.example": "fingerprint"}

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_fetch),
        ):
            result = asyncio.run(imports.browse_import_repository(
                config["id"], RepositoryBrowseRequest(query="", page=1), Store()))

        self.assertEqual(seen["repository"], config["id"])
        self.assertEqual(seen["url"], "https://repository.example/catalog?page=1&size=20")
        self.assertEqual(result["items"][0]["updated_at"], 1791816547.5)
        self.assertTrue(result["items"][0]["in_library"])
        self.assertFalse(result["items"][1]["in_library"])
        self.assertEqual(result["pagination"], {"page": 1, "pageSize": 20, "total": 31})

    def test_collection_only_repository_is_config_driven(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryCollectionRequest

        config = {
            "id": "set-library", "name": "集合库", "base_url": "https://sets.example",
            "views": [{"label": "书源集", "type": "collections"}],
            "display": {"title": "name", "domain": "url"},
            "collection_list": {
                "path_template": "/sets", "response_items": "data",
                "fields": {"id": "id", "name": "title"},
            },
            "collection_details": {
                "path_template": "/sets/{collection_id}/sources",
                "response_items": "",
            },
            "details": {"path_template": "/source/{id}", "response_items": ""},
        }
        payloads = [
            {"data": [{"id": "set-1", "title": "精选"}]},
            [{"bookSourceName": "书源", "bookSourceUrl": "https://book.example"}],
        ]
        urls = []

        async def fake_fetch(repository, url):
            urls.append(url)
            return payloads.pop(0)

        with mock.patch.dict(_REPOSITORIES, {"set-library": config}, clear=True), \
             mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_fetch):
            catalog = imports.list_import_repositories()
            collections = asyncio.run(imports.list_repository_collections("set-library"))
            sources = asyncio.run(imports.get_import_repository_collection(
                "set-library", RepositoryCollectionRequest(
                    collection_id="set-1")))

        descriptor = next(item for item in catalog["items"] if item["id"] == "set-library")
        self.assertEqual(descriptor["views"], [
            {"id": "collections", "label": "书源集", "type": "collections"},
        ])
        self.assertEqual(collections["items"][0]["name"], "精选")
        self.assertEqual(sources["sources"][0]["bookSourceName"], "书源")
        self.assertEqual(urls[1], "https://sets.example/sets/set-1/sources")

    def test_collection_details_can_fetch_configured_external_json_link(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryCollectionRequest

        config = {
            "id": "external-sets", "name": "外链合集", "base_url": "https://index.example",
            "views": [{"label": "合集", "type": "collections"}],
            "display": {},
            "collection_list": {"path_template": "/sets", "response_items": "",
                                "fields": {"id": "id", "name": "name"}},
            "collection_details": {"url_field": "link", "response_items": ""},
        }

        async def fake_fetch(url, *, follow_public_redirects=False):
            self.assertEqual(url, "https://cdn.example/sources.json")
            self.assertTrue(follow_public_redirects)
            return [{"bookSourceName": "源", "bookSourceUrl": "https://book.example"}]

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_remote_payload", side_effect=fake_fetch),
        ):
            result = asyncio.run(imports.get_import_repository_collection(
                config["id"], RepositoryCollectionRequest(
                    collection_id="1", collection_url="https://cdn.example/sources.json")))

        self.assertEqual(result["sources"][0]["bookSourceName"], "源")

    def test_browse_html_directory_normalizes_items_and_pagination(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryBrowseRequest

        config = self._browse_repository()
        config["browse"]["pagination"] = {"page_param": "page", "page_size": 20}
        config["browse"].pop("response")
        config["browse"]["list"].update({
            "format": "html",
            "html": {
                "item_selector": ".entry",
                "fields": {
                    "id": {"selector": "h2 > a", "attribute": "href",
                           "pattern": "/content/id/(\\d+)\\.html$"},
                    "name": {"selector": "h2 > a"},
                    "author": {"selector": ".author", "pattern": "用户：(.+)"},
                },
                "pagination": {"total_selector": ".count",
                               "total_pattern": "共有(\\d+)条数据"},
            },
        })
        page_html = (
            '<div class="count">共有45条数据</div>'
            '<div class="entry"><h2><a href="/content/id/42.html">示例源</a></h2>'
            '<span class="author">用户：alice</span></div>'
        )
        seen = {}

        async def fake_text(_repository, url):
            seen["url"] = url
            return page_html

        class Store:
            def live_fingerprints(self):
                return {}

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_text", side_effect=fake_text),
        ):
            result = asyncio.run(imports.browse_import_repository(
                config["id"], RepositoryBrowseRequest(page=3), Store()))

        self.assertEqual(seen["url"], "https://repository.example/catalog?page=3")
        self.assertEqual(result["items"][0]["id"], "42")
        self.assertEqual(result["items"][0]["author"], "alice")
        self.assertEqual(result["pagination"], {"page": 3, "pageSize": 20, "total": 45})

    def test_html_collection_catalog_uses_requested_page_and_total(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES

        config = {
            "id": "html-sets", "name": "HTML 合集", "base_url": "https://sets.example",
            "views": [{"label": "合集", "type": "collections"}],
            "display": {},
            "collection_list": {
                "path_template": "/sets", "page_param": "page", "page_size": 20,
                "format": "html",
                "html": {
                    "item_selector": ".ylist",
                    "fields": {
                        "id": {"selector": "h2 > a", "attribute": "href",
                               "pattern": "/id/(\\d+)\\.html$"},
                        "name": {"selector": "h2 > a"},
                        "total": {"selector": ".count", "pattern": "源数量：(\\d+)"},
                    },
                    "pagination": {"total_selector": ".all-count",
                                   "total_pattern": "共有(\\d+)条数据"},
                },
            },
            "collection_details": {"path_template": "/sets/{collection_id}.html",
                                   "response_items": ""},
        }
        seen = {}

        async def fake_text(_repository, url):
            seen["url"] = url
            return ('<div class="all-count">共有41条数据</div><div class="ylist">'
                    '<h2><a href="/id/7.html">合集七</a></h2>'
                    '<span class="count">源数量：18</span></div>')

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_text", side_effect=fake_text),
        ):
            result = asyncio.run(imports.list_repository_collections(config["id"], page=2))

        self.assertEqual(seen["url"], "https://sets.example/sets?page=2")
        self.assertEqual(result["items"][0]["id"], "7")
        self.assertEqual(result["items"][0]["total"], 18)
        self.assertEqual(result["pagination"], {"page": 2, "pageSize": 20, "total": 41})

    def test_html_collection_detail_resolves_json_import_link(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryCollectionRequest

        config = {
            "id": "html-details", "name": "HTML 合集", "base_url": "https://sets.example",
            "views": [{"label": "合集", "type": "collections"}],
            "display": {},
            "collection_list": {"path_template": "/sets", "response_items": "",
                                "fields": {"id": "id", "name": "name"}},
            "collection_details": {
                "path_template": "/sets/{collection_id}.html",
                "format": "html_json",
                "json_link_selector": "a[href^='yuedu://booksource/importonline']",
                "response_items": "",
            },
        }
        seen = {}

        async def fake_text(_repository, url):
            seen["page"] = url
            return ('<a href="yuedu://booksource/importonline?src='
                    'https://sets.example/sets/7.json">导入</a>')

        async def fake_json(_repository, url, **options):
            seen["json"] = url
            seen["options"] = options
            return [{"bookSourceName": "源", "bookSourceUrl": "https://book.example"}]

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_text", side_effect=fake_text),
            mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_json),
        ):
            result = asyncio.run(imports.get_import_repository_collection(
                config["id"], RepositoryCollectionRequest(collection_id="7")))

        self.assertEqual(seen["page"], "https://sets.example/sets/7.html")
        self.assertEqual(seen["json"], "https://sets.example/sets/7.json")
        self.assertEqual(seen["options"], {
            "follow_public_redirects": False,
            "allowed_redirect_hosts": None,
        })
        self.assertEqual(result["sources"][0]["bookSourceName"], "源")

    def test_repository_transport_rejects_hosts_outside_adapter_config(self) -> None:
        from backend.api import imports
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            imports._validate_repository_url(
                self._browse_repository(), "https://attacker.example/collect")
        self.assertEqual(caught.exception.status_code, 500)

    def test_browse_keyword_uses_search_operation(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryBrowseRequest

        config = self._browse_repository()
        seen = {}

        async def fake_fetch(repository, url):
            seen.update(repository=repository["id"], url=url)
            return {"results": [{"id": "item-1", "name": "漫画",
                                 "url": "https://m.example",
                                 "created": 1790925308.821104}],
                    "count": 1, "current": 1, "page_size": 20}

        class Store:
            def live_fingerprints(self):
                return {}

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_fetch),
        ):
            result = asyncio.run(imports.browse_import_repository(
                config["id"], RepositoryBrowseRequest(query="漫画", page=1), Store()))

        self.assertEqual(seen["repository"], config["id"])
        self.assertIn("/search?", seen["url"])
        self.assertIn("kind=sources", seen["url"])
        self.assertIn("q=%E6%BC%AB%E7%94%BB", seen["url"])
        self.assertEqual(result["items"][0]["updated_at"], 1790925308.821104)
        self.assertEqual(result["pagination"], {"page": 1, "pageSize": 20, "total": 1})

    def test_search_overlay_without_path_reuses_list_endpoint(self) -> None:
        from backend.import_repositories import browse_url

        config = self._browse_repository()
        config["browse"]["search"].pop("path")
        self.assertEqual(
            browse_url(config, "关键词", 2),
            "https://repository.example/catalog?kind=sources&q=%E5%85%B3%E9%94%AE%E8%AF%8D&page=2&size=20",
        )

    def test_details_are_normalized_to_source_arrays(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryDetailsRequest

        config = self._browse_repository()
        seen = []

        async def fake_fetch(repository, url):
            seen.append(url)
            return [{"bookSourceName": "源", "bookSourceUrl": "https://m.example"}]

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_fetch),
        ):
            result = asyncio.run(imports.get_import_repository_details(
                config["id"], RepositoryDetailsRequest(
                    ids=["89e6bde2-ab94-4fec-a710-c269afcf6e84"])))

        self.assertEqual(result["items"][0]["sources"][0]["bookSourceName"], "源")
        self.assertTrue(seen[0].endswith(
            "/source/89e6bde2-ab94-4fec-a710-c269afcf6e84"))

    def test_details_reject_non_source_entries_instead_of_dropping(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryDetailsRequest
        from fastapi import HTTPException

        config = self._browse_repository()

        async def fake_fetch(_repository, _url):
            return [{"bookSourceName": "源", "bookSourceUrl": "https://m.example"}, "bad"]

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_payload", side_effect=fake_fetch),
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(imports.get_import_repository_details(
                    config["id"], RepositoryDetailsRequest(ids=["source-1"])))
        self.assertEqual(caught.exception.status_code, 502)

    def test_invalid_repository_item_id_is_rejected_before_transport(self) -> None:
        from backend.api import imports
        from backend.import_repositories import _REPOSITORIES
        from backend.schemas import RepositoryDetailsRequest
        from fastapi import HTTPException

        config = self._browse_repository()

        with (
            mock.patch.dict(_REPOSITORIES, {config["id"]: config}, clear=True),
            mock.patch.object(imports, "_fetch_repository_payload") as fetch,
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(imports.get_import_repository_details(
                    config["id"], RepositoryDetailsRequest(ids=["../private"])))

        self.assertEqual(caught.exception.status_code, 400)
        fetch.assert_not_called()


class ImportUrlTests(unittest.TestCase):
    def test_fetch_returns_json_array_without_network(self) -> None:
        from backend.api import imports

        raw = b'[{"bookSourceUrl":"https://example.com"}]'

        class _Response:
            status = 200
            content_length = len(raw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            class _Content:
                async def iter_chunked(self, _size):
                    yield raw

            content = _Content()

        class _Session:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return _Response()

        async def fake_target(_url):
            return urlsplit("https://example.com/sources.json"), "example.com", [
                (socket.AF_INET, "93.184.216.34")]

        with mock.patch.object(imports, "_public_https_target", side_effect=fake_target), \
             mock.patch.object(imports.aiohttp, "ClientSession", _Session):
            content = asyncio.run(imports._fetch_remote_json(
                "https://example.com/sources.json"))
        self.assertEqual(json.loads(content), [{"bookSourceUrl": "https://example.com"}])

    def test_import_url_rejects_non_https_before_network(self) -> None:
        from backend.api import imports
        from backend.schemas import ImportUrlRequest
        from fastapi import HTTPException

        with mock.patch.object(imports.aiohttp, "ClientSession") as session:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(imports.fetch_import_url(
                    ImportUrlRequest(url="http://example.com/sources.json")))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("HTTPS", caught.exception.detail)
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
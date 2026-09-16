# -*- coding: utf-8 -*-
"""Web 导入接口（/api/import）的安全导入三分类测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

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

    def test_import_preserves_health_status_and_user_tags(self) -> None:
        # 先种一个源让库非空，触发用户标签过滤分支
        self.store.upsert_sources([make_source("https://seed.example")])
        source = make_source("https://tagged.example")
        source["bookSourceGroup"] = "📖小说,✅可用,精排,R18"
        result = self._import([source])
        self.assertEqual(result["new_count"], 1)
        stored = self.store.get_source("https://tagged.example")
        group = stored.get("bookSourceGroup", "")
        self.assertIn("可用", group)
        self.assertIn("精排", group)
        self.assertIn("R18", group)

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


if __name__ == "__main__":
    unittest.main()
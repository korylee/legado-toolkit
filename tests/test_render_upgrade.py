# -*- coding: utf-8 -*-
"""渲染升级：材料判为「先渲染」时，用带 `webView` 的同一 URL 重跑一次。

钉两件事（都是「少了它就静默走回头路」的判据）：

  ① **key 形态**：渲染不是我们另发明的参数，而是把用户在源里会写的那条 URL 选项
     （`,{"webView":true}`）拼上去——上游 `AnalyzeUrl` 按这条语法解析，所以调试里
     与阅读时行为一致。
  ② **升级的门**：只有答案说「先渲染」才升级；没页面地址、引擎失败都要**留下原因**，
     不许把「升不了」做成「材料没变化」让上游以为判据错了（AGENTS #4）。

不跑 Gradle、不联网：引擎执行体打桩。
"""
from __future__ import annotations

import json
import unittest

from backend.jobs import debug_materials as DM
from core.debug_keys import render_key
from core.jvm_debug import MATERIAL_RENDERED_DOM


def _job(result: dict) -> dict:
    return {"payload": json.dumps({"source": {"bookSourceUrl": "https://x.com"}}),
            "result_json": json.dumps(result)}


def _result(*, layer: str, url: str, html: str = "<html>static</html>") -> dict:
    return {
        "steps": [{"name": "content", "page_id": "chapter", "url": url,
                   "structured": {"content": "占位图"}, "runtime": {}}],
        "pages": [{"id": "chapter", "url": url, "html": html, "origin": "engine",
                   "page_layer": {"layer": layer}}],
    }


class RenderKeyTests(unittest.TestCase):
    def test_option_is_appended_in_the_same_grammar_the_user_writes(self):
        self.assertEqual(render_key("content", "https://x.com/1.html"),
                         '--https://x.com/1.html,{"webView":true}')
        self.assertEqual(render_key("toc", "https://x.com/list"),
                         '++https://x.com/list,{"webView":true}')
        # 「详情」那一步在 key 语法里叫 info，没前缀
        self.assertEqual(render_key("bookUrl", "https://x.com/b/1"),
                         'https://x.com/b/1,{"webView":true}')

    def test_existing_options_are_not_doubled(self):
        """已经带过选项就以原样为准：拼第二份会让上游只认最后一个，把前面的吃掉。"""
        already = 'https://x.com/1.html,{"webView":true}'
        self.assertEqual(render_key("content", already), "--" + already)


class UpgradeGateTests(unittest.TestCase):
    def test_readable_material_is_left_alone(self):
        """材料本来就够读：`None` = 不需要升级（引擎一次都不跑）。"""
        called = []
        out = DM.upgraded_material(
            _job(_result(layer="L1", url="https://x.com/1.html")), "j", "content",
            runner=lambda *a, **k: called.append(1) or {})
        self.assertIsNone(out)
        self.assertEqual(called, [], "答案不是「先渲染」却去跑了引擎")

    def test_render_upgrade_swaps_in_the_rendered_material(self):
        """答案说「先渲染」→ 用渲染后的页面换掉材料，并自报材料种类与耗时。"""
        rendered = {
            "steps": [{"name": "content", "page_id": "chapter",
                       "url": "https://x.com/1.html",
                       "structured": {"content": "真图 34 张"}, "runtime": {"globals": []}}],
            # `rendered: True` 是**页上的事实**（`core.jvm_debug.material_kind` 只看它
            # 与 `render_js`、`origin`）：渲染过与否由引擎侧判，消费侧不自己推断
            "pages": [{"id": "chapter", "url": "https://x.com/1.html",
                       "html": "<html>rendered</html>", "origin": "engine",
                       # `rendered: True` + `render_js: False` 才是「渲染出的 DOM」；
                       # `render_js` 缺省会被判成 unknown（判据见 core.jvm_debug.material_kind）
                       "rendered": True, "render_js": False,
                       "page_layer": {"layer": "L3"}}],
        }
        seen = {}

        def fake_runner(source, key, **kw):
            seen["key"] = key
            return rendered

        out = DM.upgraded_material(
            _job(_result(layer="L3", url="https://x.com/1.html")), "j", "content",
            runner=fake_runner)
        self.assertEqual(seen["key"], '--https://x.com/1.html,{"webView":true}')
        self.assertEqual(out["html"], "<html>rendered</html>")
        # 渲染过 = `material_kind` 由引擎侧判成 rendered_dom；消费侧不自己推断
        self.assertEqual(out["kind"], MATERIAL_RENDERED_DOM)
        self.assertIn("cost_sec", out)

    def test_why_it_could_not_upgrade_is_kept(self):
        """升不了要留住原因：说成「材料没变化」等于让上游以为判据错了。"""
        # 那一步没有页面地址
        out = DM.upgraded_material(
            _job(_result(layer="L3", url="")), "j", "content",
            runner=lambda *a, **k: {})
        self.assertIn("渲染不了", out["skipped"])
        # 引擎那边失败
        def boom(*a, **k):
            raise RuntimeError("浏览器起不来")

        out2 = DM.upgraded_material(
            _job(_result(layer="L3", url="https://x.com/1.html")), "j", "content",
            runner=boom)
        self.assertIn("浏览器起不来", out2["skipped"])


if __name__ == "__main__":
    unittest.main()
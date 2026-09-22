# -*- coding: utf-8 -*-
"""生成之后的验证：**两条路共用同一份**（`core.jvm_debug.verify_generated`）。

原来 CLI（`services.add_source.run_add`）跑的是本地回放器、Web（`backend/api/ops.py`）
跑的是真引擎——同一件事两份实现，结论必然漂。这一条盯的就是「只剩一份，而且 CLI 也走它」，
外加引擎不可用时**不推翻生成结果**（原因要走到用户眼前，AGENTS #4）。
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from services.add_source import run_add

URL = "http://example.com/search.php?q=绍宋"
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "samsbook_search_shaosong.html"


def _fake_fetch(html):
    def go(url, *a, **kw):
        return html if "q=" in str(url) else "<html><body>详情页</body></html>"
    return go


class CliVerificationTests(unittest.TestCase):
    """CLI：验证走本机引擎，且没验成时不带走生成结果。"""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cli_verify_"))
        self.out = self.root / "gen.json"
        self.html = FIXTURE.read_text(encoding="utf-8")

    def _run(self, engine_result, verify=True, **kw):
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=_fake_fetch(self.html)))
            stack.enter_context(mock.patch("services.add_source._find_main_sources",
                                           return_value=[]))
            stack.enter_context(mock.patch("services.add_source.verify_generated",
                                           return_value=engine_result))
            stack.enter_context(contextlib.redirect_stdout(buf))
            out = run_add(URL, name="测试源", output=str(self.out), no_ask=True,
                          probe=False, verify=verify, interactive=False, **kw)
            return out, buf.getvalue()

    def test_the_cli_verification_is_the_engine_one(self):
        engine = {"source": "jvm", "all_ok": True,
                  "steps": [{"name": "search", "ok": True, "detail": "2 条结果"}]}
        out, printed = self._run(engine)
        self.assertEqual(out.rc, 0, out.error)
        self.assertIn("本机引擎", printed, "标签要跟着来源走（本地回放那句已经不该出现）")
        self.assertIn("search", printed)
        self.assertIn("全链路通过", printed)
        self.assertNotIn("离线回放", printed, "CLI 不该再报「本地粗略验证」")

    def test_no_verify_flag_skips_it_entirely(self):
        with mock.patch("services.add_source.verify_generated") as engine:
            out, _printed = self._run({}, verify=False)
        self.assertEqual(out.rc, 0)
        self.assertFalse(engine.called, "--no-verify 就不该去跑引擎")

    def test_engine_unavailable_keeps_the_source_and_says_why(self):
        skipped = {"steps": [], "pages": [], "all_ok": None, "skipped": True,
                   "engine": "jvm", "error": "本机引擎不可用：先在设置里填 App 源码目录"}
        out, printed = self._run(skipped)
        self.assertEqual(out.rc, 0, "没验成不该让生成失败")
        self.assertTrue(self.out.exists(), "源照样要落盘")
        self.assertIn("这次没验成", printed)
        self.assertIn("本机引擎不可用", printed, "原因要走到用户眼前")
        self.assertNotIn("全链路通过", printed, "没验成不能说通过")

    def test_the_shared_entry_point_is_used_with_the_detail_page(self):
        calls = []

        def fake(source, keyword, detail_url="", **kw):
            calls.append({"keyword": keyword, "detail_url": detail_url})
            return {"source": "jvm", "all_ok": True, "steps": []}

        with mock.patch("services.add_source.verify_generated", side_effect=fake), \
                mock.patch("services.add_source.fetch", side_effect=_fake_fetch(self.html)), \
                mock.patch("services.add_source._find_main_sources", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()):
                run_add(URL, name="测试源", output=str(self.out), no_ask=True,
                        probe=False, verify=True, interactive=False)
        self.assertEqual(len(calls), 1, "验证只该跑一次")
        self.assertEqual(calls[0]["keyword"], "绍宋")


class WebPathUsesTheSameEntryTests(unittest.TestCase):
    """Web 那条路（`ops.run_add_job`）不许再有自己的实现。"""

    def test_ops_has_no_local_verifier_and_calls_the_shared_one(self):
        text = (pathlib.Path(__file__).parent.parent / "backend/api/ops.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def _verify_generated", text,
                         "ops.py 里还留着一份本地实现——两条路会漂")
        self.assertIn("verify_generated", text, "ops.py 该用共用那份")


if __name__ == "__main__":
    unittest.main()

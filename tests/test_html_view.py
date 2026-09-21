# -*- coding: utf-8 -*-
"""前端纯函数 `utils/htmlView.js` 的闸门：用 node 跑它自己的断言。

**为什么由 Python 来当入口**：这个仓库的「全量测试」是 `python -m unittest discover
-s tests -t .` 一条命令，而 htmlView 是显示层的东西（放进后端要来回传两份正文，
放进抽屉又没人守）。所以让 node 跑断言、Python 收口——**一条命令仍然管整体**。

没有 node（或没装前端依赖）时**跳过**而不是红：这块是显示层的便利，不该让
「没装 node 的机器」跑不了全量测试。
"""
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST_FILE = ROOT / "frontend" / "src" / "utils" / "htmlView.test.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "没有 node：前端纯函数的断言跑不了（跳过，不算失败）")
class HtmlViewTests(unittest.TestCase):

    def test_node_assertions_pass(self):
        self.assertTrue(TEST_FILE.exists(), "找不到 %s" % TEST_FILE)
        p = subprocess.run(
            [NODE, "--test", str(TEST_FILE)],
            cwd=str(ROOT / "frontend"),
            capture_output=True, text=True, errors="replace", timeout=120)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        # 失败时只回中间几行，别把整份 node 输出糊到断言里
        tail = " | ".join(out.splitlines()[-12:])
        self.assertEqual(p.returncode, 0, "htmlView 断言没过：%s" % tail)


if __name__ == "__main__":
    unittest.main()

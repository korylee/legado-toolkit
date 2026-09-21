# -*- coding: utf-8 -*-
"""前端**纯函数**的闸门：把 `frontend/src/utils/*.test.js` 全交给 node 跑。

**为什么由 Python 来当入口**：这个仓库的「全量测试」是
`python -m unittest discover -s tests -t .` 一条命令，而这几块是显示层的东西
（放进后端要来回传两份正文，放进抽屉又没人守）。让 node 跑断言、Python 收口——
**一条命令仍然管整体**，而且新加一个 `*.test.js` 自动被收进来（下面用 glob）。

没有 node 时**跳过**而不是红：这是显示层的便利，不该让「没装 node 的机器」跑不了全量测试。
"""
import glob
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UTILS = ROOT / "frontend" / "src" / "utils"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "没有 node：前端纯函数的断言跑不了（跳过，不算失败）")
class FrontendUtilsTests(unittest.TestCase):

    def test_node_assertions_pass(self):
        files = sorted(glob.glob(str(UTILS / "*.test.js")))
        self.assertTrue(files, "一个 *.test.js 都没找到（glob 写错了？）：%s" % UTILS)
        p = subprocess.run(
            [NODE, "--test"] + files,
            cwd=str(ROOT / "frontend"),
            capture_output=True, text=True, errors="replace", timeout=180)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        # 失败时只回末尾几行，别把整份 node 输出糊进断言里
        tail = " | ".join(out.splitlines()[-14:])
        self.assertEqual(p.returncode, 0,
                         "前端纯函数断言没过（%d 个文件）：%s" % (len(files), tail))


if __name__ == "__main__":
    unittest.main()

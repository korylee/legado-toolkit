# -*- coding: utf-8 -*-
"""界面文案闸门：术语表与标点规范不许倒退。

规则与术语表在 ``tools/check_copy.py``（**唯一事实来源**，AGENTS #10）。
这里只做两件事：

1. 不许出现基线之外的新违规。文案漂移与枚举漂移是同一类问题（AGENTS #7 / #8），
   靠人记必然漂——所以挂进 unittest，跟全量测试一起跑（AGENTS #6）。
2. 基线本身要良构：它是「只减不增」的清单，写坏了闸门就哑了。

清理完一批文案后跑 ``python -m tools.check_copy --update-baseline`` 收窄基线。
"""

import json
import pathlib
import unittest

from tools.check_copy import BASELINE, has_copy_ok, key_of, load_baseline, scan

ROOT = pathlib.Path(__file__).resolve().parent.parent


class CopyGateTests(unittest.TestCase):
    def test_no_new_violations(self):
        base = load_baseline()
        hits = [v for v in scan(ROOT) if v["level"] == "error"]
        new = [v for v in hits if key_of(v) not in base]
        if new:
            detail = "\n".join(
                "  %s:%d  [%s] %s\n      %s"
                % (v["file"], v["line"], v["rule"], v["why"], v["text"][:80])
                for v in sorted(new, key=lambda x: (x["file"], x["line"]))[:20])
            self.fail(
                "新增了 %d 条文案违规（术语表见 tools/check_copy.py）：\n%s\n"
                "改文案即可；确属误报就往 TERM_RULES 里加例外，别直接改基线。"
                % (len(new), detail))

    def test_baseline_is_wellformed(self):
        """基线是纯字符串列表，且键长成 文件|规则|哈希 的样子。"""
        self.assertTrue(BASELINE.exists(), "基线文件不在，先跑 --update-baseline")
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        entries = data.get("entries")
        self.assertIsInstance(entries, list)
        for entry in entries:
            self.assertIsInstance(entry, str)
            self.assertEqual(len(entry.split("|")), 3, entry)


class CopyOkMarkerTests(unittest.TestCase):
    """``copy-ok`` 是**永久例外**的出口：必须逐字保留的串不进基线。

    基线是「只减不增」的欠账清单；往里塞永久条目等于把闸门关掉——下次谁翻到
    那条都会以为是一笔待还的账，而它其实永远不该被"还"。
    """

    def test_marker_on_the_line_or_the_one_above(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8", newline="\n") as f:
            f.write('X = "需代理复检"     # copy-ok: 换词表的键\n')
            f.write('Y = "本地回放"\n')
            path = pathlib.Path(f.name)
        try:
            self.assertTrue(has_copy_ok(path, 1, 1))
            self.assertFalse(has_copy_ok(path, 2, 2))
        finally:
            path.unlink()

    def test_retired_tag_keys_are_marked_not_reported(self):
        """真实的那一处：``core/tags.py`` 换词表的键（AGENTS #17）。

        它**必须**逐字是旧词，改了存量分组就认不出来。所以它带标记、不进基线——
        这条用例同时钉住「标记还在」和「工具确实跳过它」。
        """
        hits = [v for v in scan(ROOT) if v["file"] == "core/tags.py"]
        self.assertEqual(hits, [], [v["text"] for v in hits])


if __name__ == "__main__":
    unittest.main()

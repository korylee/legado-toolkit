# -*- coding: utf-8 -*-
"""`core.jvm_env` 的环境推导测试。

守的是「用户只填一个路径、其余全部推导」这条约定（AGENTS #13）：
每根被推导的轴都要有钉子，否则换台机器就会以「明明装了却报找不到」的形式坏掉——
而那正是这条约定最容易失效的地方。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from core import jvm_env


class VfoxJavaTests(unittest.TestCase):
    """vfox 的 JDK 发现。

    vfox 把 SDK 装在 `<root>/sdks/<name>`，**java 插件两种布局都见过**：
    单版本时 JDK 根直接落在 `sdks/java/`，多版本时在 `sdks/java/<version>/`。
    只认一种的话，另一种布局下会「明明装了却报找不到 JDK」。
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("VFOX_HOME")
        os.environ["VFOX_HOME"] = str(self.root)
        # 默认的 ~/.vfox 若真的存在，会与本用例的临时根混淆——显式指向临时根即可，
        # 因为实现优先读 VFOX_HOME
        self.sdks = self.root / "sdks" / "java"

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("VFOX_HOME", None)
        else:
            os.environ["VFOX_HOME"] = self._old
        self._tmp.cleanup()

    def _make_jdk(self, *parts: str) -> Path:
        d = self.sdks.joinpath(*parts)
        (d / "bin").mkdir(parents=True, exist_ok=True)
        (d / "bin" / ("java.exe" if os.name == "nt" else "java")).write_text("")
        return d

    def test_single_version_jdk_root(self) -> None:
        """单版本布局：JDK 根就是 sdks/java 本身。"""
        self._make_jdk()
        self.assertEqual(jvm_env._vfox_java_candidates(), [self.sdks])

    def test_versioned_subdirs_newest_first(self) -> None:
        """多版本布局：sdks/java/<version>/，按目录名倒序（新版优先）。"""
        self._make_jdk("17.0.9")
        self._make_jdk("21.0.1")
        got = [p.name for p in jvm_env._vfox_java_candidates()]
        self.assertEqual(got, ["21.0.1", "17.0.9"])

    def test_missing_root_is_not_an_error(self) -> None:
        """没装 vfox 不是错误（这条链只是候选之一，后面的目录会兜住）。"""
        self.assertEqual(jvm_env._vfox_java_candidates(), [])

    def test_home_fallback_when_env_absent(self) -> None:
        """没有 VFOX_HOME 时退回 ~/.vfox——**这是绝大多数用户的形态**
        （vfox 默认就装在那儿，没人会去设那个环境变量）。"""
        os.environ.pop("VFOX_HOME", None)
        home = self.root / "fakehome"
        (home / ".vfox" / "sdks" / "java" / "bin").mkdir(parents=True)
        (home / ".vfox" / "sdks" / "java" / "bin" /
         ("java.exe" if os.name == "nt" else "java")).write_text("")
        old_home = Path.home
        Path.home = classmethod(lambda cls: home)          # type: ignore[assignment]
        try:
            got = jvm_env._vfox_java_candidates()
        finally:
            Path.home = old_home                            # type: ignore[assignment]
        self.assertEqual(got, [home / ".vfox" / "sdks" / "java"])

    def test_vfox_ranks_before_legacy_dirs(self) -> None:
        """vfox 排在常见安装目录之前：用户既然用它管版本，就该以它为准。

        顺序反了的话，机器上同时存在旧的手装 JDK 时，会静默用旧的那个——
        而用户刚在 vfox 里切换过版本、正是想让它生效。
        """
        self._make_jdk()
        cands = jvm_env._common_jdk_candidates()
        self.assertIn(self.sdks, cands)
        legacy = [c for c in cands if "Program Files" in str(c)]
        if legacy:
            self.assertLess(cands.index(self.sdks), cands.index(legacy[0]))


if __name__ == "__main__":
    unittest.main()

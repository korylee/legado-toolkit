# -*- coding: utf-8 -*-
"""`core.jvm_env` 的环境推导测试。

守的是「用户只填一个路径、其余全部推导」这条约定（AGENTS #13）：
每根被推导的轴都要有钉子，否则换台机器就会以「明明装了却报找不到」的形式坏掉——
而那正是这条约定最容易失效的地方。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import jvm_env


class EnvironmentDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _java(self, home: Path) -> Path:
        exe = home / "bin" / ("java.exe" if os.name == "nt" else "java")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        return exe

    def test_java_home_is_used(self) -> None:
        home = self.root / "jdk"
        exe = self._java(home)
        info = SimpleNamespace(stderr='openjdk version "21.0.1"', stdout="", returncode=0)
        with patch.dict(os.environ, {"JAVA_HOME": str(home)}, clear=False), \
                patch("core.jvm_env.subprocess.run", return_value=info):
            result = jvm_env._find_java()
        self.assertTrue(result.ok)
        self.assertEqual(result.found, str(exe))

    def test_invalid_java_home_does_not_fall_back(self) -> None:
        with patch.dict(os.environ, {"JAVA_HOME": str(self.root / "missing")}, clear=False), \
                patch("core.jvm_env.shutil.which", return_value="C:/other/bin/java.exe"), \
                patch("core.jvm_env.subprocess.run") as run:
            result = jvm_env._find_java()
        self.assertFalse(result.ok)
        run.assert_not_called()

    def test_path_is_used_when_java_home_is_absent(self) -> None:
        exe = self._java(self.root / "jdk")
        info = SimpleNamespace(stderr='openjdk version "21.0.1"', stdout="", returncode=0)
        with patch.dict(os.environ, {}, clear=True), \
                patch("core.jvm_env.shutil.which", return_value=str(exe)), \
                patch("core.jvm_env.subprocess.run", return_value=info):
            result = jvm_env._find_java()
        self.assertTrue(result.ok)
        self.assertIn("PATH", result.detail)

    def _repo(self) -> Path:
        repo = self.root / "app"
        (repo / "app").mkdir(parents=True)
        (repo / "app" / "build.gradle.kts").write_text(
            "android {\n    compileSdk = 37\n}\n", encoding="utf-8")
        return repo

    def _sdk(self, name: str, platform: str = "android-37") -> Path:
        sdk = self.root / name
        (sdk / "platforms" / platform).mkdir(parents=True)
        return sdk

    def test_sdk_requires_project_compile_sdk(self) -> None:
        repo, sdk = self._repo(), self._sdk("sdk")
        with patch.dict(os.environ, {"ANDROID_HOME": str(sdk)}, clear=False):
            result = jvm_env._find_android_sdk(str(repo))
        self.assertTrue(result.ok)
        self.assertEqual(Path(result.found), sdk)

    def test_local_properties_is_authoritative(self) -> None:
        repo = self._repo()
        configured, other = self._sdk("configured"), self._sdk("other")
        escaped = str(configured).replace("\\", "\\\\")
        (repo / "local.properties").write_text("sdk.dir=" + escaped + "\n", encoding="utf-8")
        with patch.dict(os.environ, {"ANDROID_HOME": str(other)}, clear=False):
            result = jvm_env._find_android_sdk(str(repo))
        self.assertTrue(result.ok)
        self.assertEqual(Path(result.found), configured)

    def test_other_platform_version_does_not_pass(self) -> None:
        repo, sdk = self._repo(), self._sdk("sdk", "android-36")
        with patch.dict(os.environ, {"ANDROID_HOME": str(sdk)}, clear=False):
            result = jvm_env._find_android_sdk(str(repo))
        self.assertFalse(result.ok)
        self.assertIn("platforms;android-37", result.hint)

    def test_gradle_home_uses_default_not_app_drive(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=Path(home)):
                result = jvm_env._find_gradle_home("X:/app")
        self.assertTrue(result.ok)
        self.assertEqual(Path(result.found), Path(home) / ".gradle")


class ProcessEnvironmentTests(unittest.TestCase):
    def test_only_runtime_snapshot_paths_override_the_process_environment(self) -> None:
        resolved = {
            "app_repo": "X:/app",
            "java_home": "X:/jdk",
            "java_exe": "X:/jdk/bin/java.exe",
            "android_sdk": "X:/sdk",
            "gradle_user_home": "X:/.gradle",
        }
        env = jvm_env.process_environment({"ok": True, "runtime": resolved}, {
            "JAVA_HOME": "Y:/stale-jdk",
            "ANDROID_HOME": "Y:/stale-sdk",
            "GRADLE_USER_HOME": "Y:/.gradle",
            "KEEP_ME": "inherited",
        })
        self.assertEqual(env["LEGADO_REPO"], resolved["app_repo"])
        self.assertEqual(env["JAVA_HOME"], resolved["java_home"])
        self.assertEqual(env["ANDROID_HOME"], resolved["android_sdk"])
        self.assertEqual(env["ANDROID_SDK_ROOT"], resolved["android_sdk"])
        self.assertEqual(env["GRADLE_USER_HOME"], resolved["gradle_user_home"])
        self.assertEqual(env["KEEP_ME"], "inherited")

    def test_incomplete_selftest_cannot_produce_a_launch_environment(self) -> None:
        with self.assertRaisesRegex(ValueError, "自检未通过"):
            jvm_env.process_environment({"ok": False, "runtime": {}})


if __name__ == "__main__":
    unittest.main()

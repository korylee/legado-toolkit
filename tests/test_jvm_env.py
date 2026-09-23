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

    def test_java_eight_legacy_version_is_parsed(self) -> None:
        home = self.root / "jdk8"
        exe = self._java(home)
        info = SimpleNamespace(stderr='java version "1.8.0_402"', stdout="", returncode=0)
        with patch.dict(os.environ, {"JAVA_HOME": str(home)}, clear=False), \
                patch("core.jvm_env.subprocess.run", return_value=info):
            result = jvm_env._find_java()
        self.assertTrue(result.ok)
        self.assertEqual(8, result.version)

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
            "android {\n    compileSdk = 37\n}\n"
            "android { compileOptions { sourceCompatibility = JavaVersion.VERSION_21 } }\n"
            "kotlin { jvmToolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }\n",
            encoding="utf-8")
        (repo / "gradle" / "wrapper").mkdir(parents=True)
        (repo / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.6.1-bin.zip\n",
            encoding="utf-8")
        (repo / "gradle").mkdir(exist_ok=True)
        (repo / "gradle" / "gradle-daemon-jvm.properties").write_text(
            "toolchainVersion=21\n", encoding="utf-8")
        return repo

    def _sdk(self, name: str, platform: str = "android-37") -> Path:
        sdk = self.root / name
        (sdk / "platforms" / platform).mkdir(parents=True)
        return sdk

    def test_project_java_requirements_are_read_from_gradle_configuration(self) -> None:
        repo = self._repo()
        self.assertEqual(
            {"wrapper": "9.6.1", "daemon": 21, "toolchain": 21},
            jvm_env._project_java_requirements(str(repo)))

    def test_java_17_meets_gradle_9_launcher_but_not_daemon_or_toolchain_21(self) -> None:
        java = jvm_env.Check("Java 安装", True, found="X:/jdk17/bin/java.exe", version=17)
        launcher = jvm_env._java_requirement_check(
            "Gradle 启动 JVM", java, 17, "missing", "Gradle 9.6.1")
        daemon = jvm_env._java_requirement_check(
            "Gradle daemon JVM", java, 21, "missing", "daemon criteria", exact=True)
        toolchain = jvm_env._java_requirement_check(
            "项目编译 toolchain", java, 21, "missing", "project toolchain", exact=True)
        self.assertTrue(launcher.ok)
        self.assertFalse(daemon.ok)
        self.assertFalse(toolchain.ok)
        self.assertIn("当前 Java 17", daemon.detail)

    def test_gradle_java_candidates_include_explicit_paths_and_from_env(self) -> None:
        repo = self._repo()
        configured = self.root / "configured-jdk"
        from_env = self.root / "env-jdk"
        (repo / "gradle.properties").write_text(
            "org.gradle.java.installations.paths=" + str(configured).replace("\\", "\\\\") + "\n"
            "org.gradle.java.installations.fromEnv=JDK_FOR_GRADLE\n",
            encoding="utf-8")
        with patch.dict(os.environ, {"JAVA_HOME": str(self.root / "launcher-jdk"),
                                    "JDK_FOR_GRADLE": str(from_env),
                                    "GRADLE_USER_HOME": str(self.root / "gradle-home")}, clear=False):
            homes = jvm_env._gradle_java_homes(str(repo))
        found = {str(home): source for home, source in homes}
        self.assertIn(str(self.root / "launcher-jdk"), found)
        self.assertIn(str(configured), found)
        self.assertIn(str(from_env), found)
        self.assertEqual("Gradle installations.paths", found[str(configured)])
        self.assertEqual("Gradle fromEnv:JDK_FOR_GRADLE", found[str(from_env)])

    def test_gradle_user_home_managed_jdks_are_discovered(self) -> None:
        repo = self._repo()
        gradle_home = self.root / "gradle-home"
        managed = gradle_home / "jdks" / "jdk-21"
        self._java(managed)
        with patch.dict(os.environ, {"GRADLE_USER_HOME": str(gradle_home)}, clear=False):
            homes = jvm_env._gradle_java_homes(str(repo))
        found = {str(home): source for home, source in homes}
        self.assertEqual("Gradle User Home/jdks", found[str(managed)])

    def test_daemon_criteria_excludes_java_home_only_from_daemon_candidates(self) -> None:
        repo = self._repo()
        configured_home = self.root / "configured-daemon-jdk"
        self._java(configured_home)
        (repo / "gradle.properties").write_text(
            "org.gradle.java.home=" + str(configured_home).replace("\\", "\\\\") + chr(10),
            encoding="utf-8")
        with patch.dict(os.environ, {"GRADLE_USER_HOME": str(self.root / "gradle-home")}, clear=False):
            toolchain_homes = jvm_env._gradle_java_homes(str(repo))
            daemon_homes = jvm_env._gradle_java_homes(str(repo), daemon_criteria=True)
        self.assertIn(configured_home, [home for home, _ in toolchain_homes])
        self.assertNotIn(configured_home, [home for home, _ in daemon_homes])

    def test_toolchain_requirement_selects_matching_jdk_candidate(self) -> None:
        candidates = [
            jvm_env.Check("JDK 候选", True, found="X:/jdk17/bin/java.exe", detail="JAVA_HOME", version=17),
            jvm_env.Check("JDK 候选", True, found="Y:/jdk21/bin/java.exe", detail="Gradle paths", version=21),
        ]
        result = jvm_env._java_candidate_requirement_check(
            "项目编译 toolchain", candidates, 21, "project toolchain", exact=True)
        self.assertTrue(result.ok)
        self.assertEqual("Y:/jdk21/bin/java.exe", result.found)
        self.assertIn("未复刻 Gradle 的全部自动发现来源", result.detail)
        self.assertIn("版本必须等于 Java 21", result.detail)

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

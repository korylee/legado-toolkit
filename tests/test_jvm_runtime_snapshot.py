import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from core.jvm_runtime_snapshot import (compare, compare_readiness_environment,
                                       compare_runtime_snapshot)


class RuntimeSnapshotComparisonTests(unittest.TestCase):
    def setUp(self):
        self.dump = {
            "workingDir": "D:/app",
            "classpath": "D:/app/classes;D:/app/lib/a.jar",
            "maxHeapSize": "3g",
            "jvmArgs": ["-Dtest=true"],
            "systemProperties": {"android.manifest": "D:/app/manifest.xml"},
            "environment": {"JAVA_HOME": "D:/jdk", "LEGADO_TEST_JVM_LAUNCH_MODE": "refresh"},
        }
        self.actual = {
            "workingDir": "d:\\app",
            "classpath": "d:\\app\\classes;d:\\app\\lib\\a.jar",
            "jvmArgs": ["-Xmx3g", "-Dtest=true"],
            "systemProperties": {"android.manifest": "D:/app/manifest.xml"},
            "environment": {"JAVA_HOME": "D:/jdk", "LEGADO_TEST_JVM_LAUNCH_MODE": "direct"},
        }

    def test_matching_runtime_has_no_differences(self):
        self.assertEqual(compare(self.dump, self.actual), {})

    def test_reports_each_drifting_axis(self):
        self.actual["workingDir"] = "D:/other"
        self.actual["classpath"] = "D:/other/classes"
        self.actual["jvmArgs"] = ["-Xmx1g"]
        self.actual["systemProperties"]["android.manifest"] = "wrong"
        self.actual["environment"]["JAVA_HOME"] = "wrong"
        result = compare(self.dump, self.actual)
        self.assertEqual(set(result), {"workingDir", "classpath", "jvmArgs", "systemProperties", "environment"})


    def test_readiness_environment_matches_actual_paths(self):
        runtime = {
            "app_repo": "D:/repo",
            "java_home": "D:/jdk",
            "android_sdk": "D:/sdk",
            "gradle_user_home": "D:/.gradle",
        }
        actual_environment = {
            "LEGADO_REPO": "d:/repo",
            "JAVA_HOME": "d:/jdk",
            "ANDROID_HOME": "d:/sdk",
            "ANDROID_SDK_ROOT": "d:/sdk",
            "GRADLE_USER_HOME": "d:/.gradle",
        }
        self.assertEqual(compare_readiness_environment(runtime, actual_environment), {})

    def test_readiness_environment_reports_drift_and_missing_alias(self):
        runtime = {
            "app_repo": "D:/repo",
            "java_home": "D:/jdk",
            "android_sdk": "D:/sdk",
            "gradle_user_home": "D:/.gradle",
        }
        actual_environment = {
            "LEGADO_REPO": "D:/other-repo",
            "JAVA_HOME": "D:/jdk",
            "ANDROID_HOME": "D:/sdk",
            "GRADLE_USER_HOME": "D:/.gradle",
        }
        differences = compare_readiness_environment(runtime, actual_environment)
        self.assertEqual(differences["LEGADO_REPO"],
                         {"declared": "D:/repo", "actual": "D:/other-repo"})
        self.assertEqual(differences["ANDROID_SDK_ROOT"],
                         {"declared": "D:/sdk", "actual": None})

    def test_compare_includes_readiness_environment_differences(self):
        runtime = {
            "app_repo": "D:/repo",
            "java_home": "D:/jdk",
            "android_sdk": "D:/sdk",
            "gradle_user_home": "D:/.gradle",
        }
        actual = dict(self.actual)
        actual["environment"] = {
            "LEGADO_REPO": "D:/other-repo",
            "JAVA_HOME": "D:/jdk",
            "ANDROID_HOME": "D:/sdk",
            "ANDROID_SDK_ROOT": "D:/sdk",
            "GRADLE_USER_HOME": "D:/.gradle",
        }
        result = compare(self.dump, actual, runtime=runtime)
        self.assertEqual(result["readinessEnvironment"]["LEGADO_REPO"]["actual"],
                         "D:/other-repo")


class RuntimeSnapshotReportTests(unittest.TestCase):
    """stdout 只留摘要：完整 declared/actual 清单在报告文件里（实测一次 16KB）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.declared = self.root / "dump.json"
        self.actual = self.root / "dump.json.actual.gradle.validate.json"
        base = {
            "workingDir": "D:/app",
            "maxHeapSize": "3g",
            "jvmArgs": [],
            "systemProperties": {},
            "environment": {},
        }
        self.declared.write_text(json.dumps(
            {**base, "classpath": "D:/a.jar"}), encoding="utf-8")
        self.actual.write_text(json.dumps(
            {**base, "jvmArgs": ["-Xmx3g"],
             "classpath": ";".join("D:/j%d.jar" % i for i in range(50))}),
            encoding="utf-8")

    def test_stdout_gets_a_summary_while_the_file_keeps_the_detail(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report = compare_runtime_snapshot("gradle", "validate",
                                              path=self.actual,
                                              declared_path=self.declared)
        line = out.getvalue().strip()
        self.assertIn("classpath(declared=1/actual=50)", line)
        self.assertIn("comparison.json", line, "要告诉读者详情在哪")
        self.assertLess(len(line), 300, "stdout 不许灌完整清单")
        self.assertNotIn("D:/j49.jar", line)
        saved = json.loads(
            self.actual.with_suffix(".comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(
            len(saved["differences"]["classpath"]["actual"]), 50,
            "完整清单必须留在报告里，摘要不是删数据")
        self.assertEqual(set(report), {"mode", "entry", "differences"})


if __name__ == "__main__":
    unittest.main()

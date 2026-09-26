import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import jvm_direct


class RefreshSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "test_jvm_env.json"
        self.out.write_bytes(b"previous valid snapshot")
        self.dump = {
            "workingDir": self.tmp.name, "classpath": self.tmp.name,
            "maxHeapSize": "1g", "jvmArgs": [], "systemProperties": {},
            "environment": {}, "javaHomeEnv": self.tmp.name,
        }
        self.actual = {
            "workingDir": self.tmp.name, "classpath": self.tmp.name,
            "jvmArgs": ["-Xmx1g"], "systemProperties": {}, "environment": {},
        }
        patcher = patch.object(jvm_direct, "dump_path", return_value=self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, code=0, *, declare=True, capture=True):
        def fake_gradle(*args, **kwargs):
            candidate = Path(kwargs["env"]["LEGADO_TEST_JVM_ENV_OUT"])
            self.assertNotEqual(candidate, self.out)
            if declare:
                self.dump["environment"]["LEGADO_TEST_JVM_ENV_OUT"] = str(candidate)
                candidate.write_text(json.dumps(self.dump), encoding="utf-8")
            if capture:
                self.actual["environment"]["LEGADO_TEST_JVM_ENV_OUT"] = str(candidate)
                Path(str(candidate) + ".actual.refresh.refresh.json").write_text(
                    json.dumps(self.actual), encoding="utf-8")
            return SimpleNamespace(returncode=code, stdout="output", stderr="error")

        with patch("core.gradle_distribution.distribution_status",
                   return_value={"status": "ready"}), \
             patch.object(jvm_direct.subprocess, "run", side_effect=fake_gradle):
            return jvm_direct.refresh()

    def _assert_clean(self):
        self.assertEqual(list(self.out.parent.glob(self.out.name + ".refresh.*")), [])

    def test_unprepared_wrapper_preserves_previous_snapshot(self):
        with patch("core.gradle_distribution.distribution_status",
                   return_value={"status": "missing", "reason": "not prepared"}):
            self.assertEqual(jvm_direct.refresh(), 1)
        self.assertEqual(self.out.read_bytes(), b"previous valid snapshot")
        self._assert_clean()

    def test_gradle_failure_after_dump_preserves_previous_snapshot(self):
        self.assertEqual(self._run(code=1), 1)
        self.assertEqual(self.out.read_bytes(), b"previous valid snapshot")
        self._assert_clean()

    def test_missing_actual_snapshot_is_not_success(self):
        self.assertEqual(self._run(capture=False), 1)
        self.assertEqual(self.out.read_bytes(), b"previous valid snapshot")
        self._assert_clean()

    def test_missing_report_does_not_replace_previous_snapshot(self):
        with patch("core.jvm_runtime_snapshot.compare_runtime_snapshot",
                   return_value={"differences": {}}):
            self.assertEqual(self._run(), 1)
        self.assertEqual(self.out.read_bytes(), b"previous valid snapshot")
        self._assert_clean()

    def test_runtime_difference_is_diagnostic_only(self):
        self.dump["environment"]["JAVA_HOME"] = "C:/jdk"
        self.actual["environment"]["JAVA_HOME"] = "C:/different-jdk"
        self.assertEqual(self._run(), 0)
        report = Path(str(self.out) + ".actual.refresh.refresh.comparison.json")
        diff = json.loads(report.read_text(encoding="utf-8"))["differences"]
        self.assertEqual(diff["environment"]["JAVA_HOME"]["actual"], "C:/different-jdk")
        self._assert_clean()

    def test_success_publishes_matching_snapshot_and_report(self):
        self.assertEqual(self._run(), 0)
        self.dump["environment"]["LEGADO_TEST_JVM_ENV_OUT"] = str(self.out)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8")), self.dump)
        actual = Path(str(self.out) + ".actual.refresh.refresh.json")
        report = actual.with_suffix(".comparison.json")
        self.assertEqual(json.loads(actual.read_text(encoding="utf-8")), self.actual)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["differences"], {})
        from core.jvm_runtime_snapshot import compare
        self.assertEqual(compare(json.loads(self.out.read_text(encoding="utf-8")),
                                 json.loads(actual.read_text(encoding="utf-8"))), {})
        self._assert_clean()


if __name__ == "__main__":
    unittest.main()

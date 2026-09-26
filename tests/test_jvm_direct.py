"""JVM dump schema 与直起边界测试。

不启动 Java、不跑 Gradle：只验证缺字段时不会把当前调用方环境带进子进程。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from core import jvm_daemon, jvm_direct


def valid_dump(**overrides):
    dump = {
        "workingDir": "D:/app",
        "classpath": "D:/app/classes;D:/app/libs/*",
        "environment": {"PATH": "D:/jdk/bin", "JAVA_HOME": "D:/jdk"},
        "jvmArgs": [],
        "systemProperties": {},
        "javaLauncher": None,
        "javaHomeEnv": "D:/jdk",
        "maxHeapSize": "3g",
    }
    dump.update(overrides)
    return dump


class DumpSchemaTests(unittest.TestCase):
    def test_missing_runtime_fields_are_reported_together(self) -> None:
        dump = valid_dump()
        for key in ("workingDir", "classpath", "environment", "jvmArgs", "systemProperties"):
            dump.pop(key)

        with self.assertRaises(jvm_direct.DumpSchemaError) as ctx:
            jvm_direct.validate_dump(dump)

        message = str(ctx.exception)
        for key in ("workingDir", "classpath", "environment", "jvmArgs", "systemProperties"):
            self.assertIn(key, message)
        self.assertIn("--refresh", message)

    def test_invalid_types_are_rejected(self) -> None:
        with self.assertRaises(jvm_direct.DumpSchemaError) as ctx:
            jvm_direct.validate_dump(valid_dump(
                environment={"PATH": 1}, jvmArgs=[1], systemProperties={"x": 1}))

        message = str(ctx.exception)
        self.assertIn("environment.PATH", message)
        self.assertIn("jvmArgs", message)
        self.assertIn("systemProperties.x", message)

    def test_java_source_and_heap_are_explicit(self) -> None:
        with self.assertRaises(jvm_direct.DumpSchemaError) as ctx:
            jvm_direct.validate_dump(valid_dump(
                javaLauncher=None, javaHomeEnv=None, maxHeapSize=""))

        message = str(ctx.exception)
        self.assertIn("javaLauncher 与 javaHomeEnv", message)
        self.assertIn("maxHeapSize", message)

    def test_empty_collections_are_explicit_and_do_not_inherit(self) -> None:
        dump = valid_dump(environment={}, jvmArgs=[], systemProperties={})
        self.assertIs(jvm_direct.validate_dump(dump), dump)
        self.assertEqual(jvm_direct.java_env(dump), {})

    def test_java_env_uses_dump_snapshot_only(self) -> None:
        dump = valid_dump(environment={"DUMP_ONLY": "yes"})
        with mock.patch.dict(jvm_direct.os.environ, {"CALLER_ONLY": "no"}, clear=True):
            self.assertEqual(jvm_direct.java_env(dump), {"DUMP_ONLY": "yes"})


class BoundaryTests(unittest.TestCase):
    def test_run_direct_rejects_missing_runtime_fields_before_subprocess(self) -> None:
        for field in ("workingDir", "classpath", "environment"):
            with self.subTest(field=field):
                dump = valid_dump()
                dump.pop(field)
                with mock.patch.object(jvm_direct.subprocess, "run") as run:
                    with self.assertRaises(jvm_direct.DumpSchemaError):
                        jvm_direct.run_direct(dump)
                run.assert_not_called()

    def test_daemon_rejects_missing_runtime_fields_before_start(self) -> None:
        for field in ("workingDir", "classpath", "environment"):
            with self.subTest(field=field):
                dump = valid_dump()
                dump.pop(field)
                with mock.patch.object(jvm_daemon, "start") as start:
                    with self.assertRaises(jvm_direct.DumpSchemaError):
                        jvm_daemon.ensure(dump)
                start.assert_not_called()


class ExecutionReadinessTests(unittest.TestCase):
    def test_missing_snapshot_returns_actionable_reason(self) -> None:
        missing = Path("D:/missing/test_jvm_env.json")
        with mock.patch.object(jvm_direct, "dump_path", return_value=missing):
            result = jvm_direct.execution_readiness()

        self.assertFalse(result["ok"])
        self.assertIn("snapshot", result["reason"])
        self.assertIn("刷新", result["reason"])
        self.assertEqual(result["dump_path"], str(missing))

    def test_valid_snapshot_does_not_require_android_sdk_or_gradle(self) -> None:
        dump = valid_dump(
            workingDir=str(Path.cwd()),
            classpath=__file__,
        )
        with mock.patch.object(jvm_direct, "dump_path", return_value=Path(__file__)), \
             mock.patch.object(jvm_direct, "dump_is_stale", return_value=False), \
             mock.patch.object(jvm_direct, "java_exe", return_value=__file__):
            result = jvm_direct.execution_readiness(dump)

        self.assertTrue(result["ok"], result)
        self.assertEqual({item["id"] for item in result["checks"]}, {
            "runtime_snapshot", "working_directory", "runtime_classpath",
            "java_executable", "source_signature",
        })


if __name__ == "__main__":
    unittest.main()

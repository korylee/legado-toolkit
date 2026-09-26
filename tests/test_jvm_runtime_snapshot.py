import unittest
from core.jvm_runtime_snapshot import compare, compare_readiness_environment


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


if __name__ == "__main__":
    unittest.main()

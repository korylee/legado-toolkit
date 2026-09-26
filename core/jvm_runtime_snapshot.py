"""Compare declared Gradle JVM settings with snapshots captured inside the test JVM."""
from __future__ import annotations
import json
import os
from pathlib import Path
from core.jvm_direct import dump_path


READINESS_ENVIRONMENT = {
    "app_repo": ("LEGADO_REPO",),
    "java_home": ("JAVA_HOME",),
    "android_sdk": ("ANDROID_HOME", "ANDROID_SDK_ROOT"),
    "gradle_user_home": ("GRADLE_USER_HOME",),
}


def _norm_path(value: object) -> str:
    if value is None:
        return ""
    return os.path.normcase(os.path.normpath(str(value)))


def compare_readiness_environment(runtime: dict, actual_environment: dict) -> dict[str, object]:
    """Compare the frozen readiness paths with the environment seen by the test JVM."""
    differences = {}
    actual_environment = actual_environment or {}
    for runtime_key, environment_keys in READINESS_ENVIRONMENT.items():
        declared = runtime.get(runtime_key)
        if not declared:
            continue
        for environment_key in environment_keys:
            actual = actual_environment.get(environment_key)
            if _norm_path(declared) != _norm_path(actual):
                differences[environment_key] = {"declared": declared, "actual": actual}
    return differences


def compare(dump: dict, actual: dict, runtime: dict | None = None) -> dict[str, object]:
    differences = {}
    declared_dir, actual_dir = dump["workingDir"], actual["workingDir"]
    if _norm_path(declared_dir) != _norm_path(actual_dir):
        differences["workingDir"] = {"declared": declared_dir, "actual": actual_dir}
    sep = os.pathsep
    expected_cp = [_norm_path(x) for x in dump["classpath"].split(sep)]
    actual_cp = [_norm_path(x) for x in actual["classpath"].split(sep)]
    if expected_cp != actual_cp:
        differences["classpath"] = {"declared": expected_cp, "actual": actual_cp}
    expected_args = ["-Xmx" + dump["maxHeapSize"], *dump["jvmArgs"]]
    if expected_args != actual["jvmArgs"]:
        differences["jvmArgs"] = {"declared": expected_args, "actual": actual["jvmArgs"]}
    for field in ("systemProperties", "environment"):
        expected = dump[field]
        observed = actual[field]
        # Launch mode and dump output path are capture controls, not runtime inputs.
        ignored = {"LEGADO_TEST_JVM_LAUNCH_MODE", "LEGADO_TEST_JVM_ENV_OUT"} if field == "environment" else set()
        changed = {k: {"declared": v, "actual": observed.get(k)}
                   for k, v in expected.items() if k not in ignored and observed.get(k) != v}
        if changed:
            differences[field] = changed
    if runtime:
        readiness_differences = compare_readiness_environment(
            runtime, actual.get("environment") or {})
        if readiness_differences:
            differences["readinessEnvironment"] = readiness_differences
    return differences


def compare_runtime_snapshot(mode: str, entry: str, path: Path | None = None, runtime: dict | None = None, declared_path: Path | None = None) -> dict:
    dump_file = declared_path or dump_path()
    actual_file = path or Path(str(dump_file) + f".actual.{mode}.{entry}.json")
    try:
        declared = json.loads(dump_file.read_text(encoding="utf-8"))
        actual = json.loads(actual_file.read_text(encoding="utf-8"))
        differences = compare(declared, actual, runtime=runtime)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"JVM runtime snapshot 对拍不可用（仅诊断）：{exc}")
        return {"error": str(exc)}
    report = {"mode": mode, "entry": entry, "differences": differences}
    report_file = actual_file.with_suffix(".comparison.json")
    try:
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"JVM runtime snapshot 报告无法保存（仅诊断）：{exc}")
    if differences:
        print(f"JVM runtime snapshot 有差异（仅诊断）：{json.dumps(differences, ensure_ascii=False)}")
    else:
        print("JVM runtime snapshot 对拍一致")
    return report

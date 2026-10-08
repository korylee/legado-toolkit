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


def norm_path(value: object) -> str:
    if value is None:
        return ""
    return os.path.normcase(os.path.normpath(str(value)))


def dir_inside_repo(child: object, repo_root: object) -> bool:
    """`child`（dump 的 workingDir = 测试 JVM 的**模块目录**）是否属于 `repo_root`（**仓库根**）。

    两级天然不等（实测 2026-09-26：dump.workingDir=`…\\app` 而 LEGADO_REPO=`…`，
    字符串比对永远不等，选路闸门据此把每次调试都推回 Gradle）——判「同一仓库」只能是
    相等或 child 是 repo_root 的子目录；边界按 os.sep 切，防 `D:\\foo` 匹配 `D:\\foobar`。
    选路闸门（core/jvm_debug._runtime_dump_mismatch）与对拍链共用这一处判据。
    """
    c, r = norm_path(child), norm_path(repo_root)
    if not c or not r:
        return False
    return c == r or c.startswith(r + os.sep)


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
            if norm_path(declared) != norm_path(actual):
                differences[environment_key] = {"declared": declared, "actual": actual}
    return differences


def compare(dump: dict, actual: dict, runtime: dict | None = None) -> dict[str, object]:
    differences = {}
    declared_dir, actual_dir = dump["workingDir"], actual["workingDir"]
    if norm_path(declared_dir) != norm_path(actual_dir):
        differences["workingDir"] = {"declared": declared_dir, "actual": actual_dir}
    sep = os.pathsep
    expected_cp = [norm_path(x) for x in dump["classpath"].split(sep)]
    actual_cp = [norm_path(x) for x in actual["classpath"].split(sep)]
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


def describe_differences(differences: dict) -> str:
    """把差异压成一行：哪些字段不同、各自多少项。

    完整 declared/actual 清单可能有几百条（实测 2026-10-06 一次跑批 16KB），
    灌进 stdout 只会淹没日志；完整内容本来就会写进 `.comparison.json`。
    """
    parts = []
    for field, value in differences.items():
        if isinstance(value, dict) and "declared" in value and "actual" in value:
            declared, actual = value["declared"], value["actual"]
            if isinstance(declared, list) or isinstance(actual, list):
                parts.append("%s(declared=%d/actual=%d)"
                             % (field, len(declared or []), len(actual or [])))
            else:
                parts.append(field)
        elif isinstance(value, dict):
            parts.append("%s（%d 项）" % (field, len(value)))
        else:
            parts.append(field)
    return "、".join(parts) or "（未分类）"


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
    saved = True
    try:
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        saved = False
        print(f"JVM runtime snapshot 报告无法保存（仅诊断）：{exc}")
    if differences:
        where = "；完整清单见 %s" % report_file if saved else ""
        print("JVM runtime snapshot 有差异（仅诊断）：%s%s"
              % (describe_differences(differences), where))
    else:
        print("JVM runtime snapshot 对拍一致")
    return report

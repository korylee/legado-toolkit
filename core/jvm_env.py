# -*- coding: utf-8 -*-
"""JVM 校验服务的环境推导与自检（S2）。

**设计约束**（TODO `jvm-env-readiness`）：App 仓库配置优先决定 SDK / Java 要求；
运行时只从显式环境变量、`PATH`、`local.properties`、管理台显式 SDK 目录和 Gradle 项目配置发现，
不猜机器安装目录。Gradle 用户目录使用明确环境变量或 Gradle 默认目录。
自检不安装工具；它只做路径、版本、所需 SDK 平台和可写性检查。
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.gradle_distribution import (GradleDistributionError, distribution_status,
                                      wrapper_distribution_url)


@dataclass
class Check:
    """一项环境检查的结果。ok=False 时 hint 告诉用户下一步做什么。"""
    name: str
    ok: bool
    found: str = ""        # 实际使用的路径/版本
    detail: str = ""       # 探测过程的关键信息（给界面 tooltip）
    hint: str = ""         # ok=False 时的建议动作
    items: List[Dict[str, str]] = field(default_factory=list)  # 探测过的候选
    version: Optional[int] = None
    id: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)


CHECK_IDS = {
    "运行平台": "platform",
    "App 源码目录": "app_repo",
    "启动器（appservice/legado-gradle.bat）": "launcher",
    "Java 安装": "java",
    "Gradle wrapper": "gradle_wrapper",
    "Gradle 启动 JVM": "gradle_client_jvm",
    "Gradle daemon JVM": "gradle_daemon_jvm",
    "项目编译 toolchain": "project_toolchain",
    "Android SDK": "android_sdk",
    "Gradle 用户目录": "gradle_user_home",
    "Gradle Wrapper 分发包": "gradle_distribution",
}


def _check_payload(check: Check) -> Dict[str, Any]:
    payload = dict(check.__dict__)
    payload["id"] = check.id or CHECK_IDS.get(check.name, check.name)
    return payload


def _path_from_java_exe(exe: str) -> str:
    return str(Path(exe).resolve().parent.parent)


def _fingerprint(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _find_java() -> Check:
    tried: List[Dict[str, str]] = []
    jh = os.environ.get("JAVA_HOME", "")
    if jh:
        # An explicit but broken JAVA_HOME must not silently switch the engine.
        candidates = [Path(jh) / "bin" / ("java.exe" if os.name == "nt" else "java")]
        source = "JAVA_HOME"
    else:
        path_java = shutil.which("java.exe" if os.name == "nt" else "java")
        candidates = [Path(path_java)] if path_java else []
        source = "PATH"
    for exe in candidates:
        if not exe.is_file():
            tried.append({"path": str(exe), "result": "不存在"})
            continue
        try:
            out = subprocess.run(
                [str(exe), "-version"], capture_output=True, text=True, timeout=10,
                env={**os.environ, "JAVA_HOME": str(exe.parent.parent)})
            if out.returncode != 0:
                tried.append({"path": str(exe), "result": "java -version 退出码 " + str(out.returncode)})
                continue
            raw_version = out.stderr or out.stdout
            ver = next((line.strip() for line in raw_version.splitlines()
                        if re.search(r'\bversion\s+"?\d', line, re.I)), "")
            m = re.search(r'\bversion\s+"?(\d+)', ver, re.I)
            major = int(m.group(1)) if m else 0
            if major == 1:
                legacy = re.search(r'\bversion\s+"?1\.(\d+)', ver, re.I)
                major = int(legacy.group(1)) if legacy else major
            tried.append({"path": str(exe), "result": ver.strip()[:60]})
            if major > 0:
                return Check("Java 安装", True, found=str(exe),
                             detail=source + "；" + ver.strip(), items=tried, version=major)
        except Exception as e:
            tried.append({"path": str(exe), "result": "执行失败: %s" % type(e).__name__})
    return Check("Java 安装", False,
                 hint="安装有效的 JDK，并设置 JAVA_HOME 或 PATH", items=tried)


def _gradle_java_homes(app_repo: str, *, daemon_criteria: bool = False) -> List[tuple[Path, str]]:
    """Collect configured or Gradle-managed JDK homes without guessing install directories."""
    gradle_home = Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle"))).expanduser()
    property_files = [gradle_home / "gradle.properties", Path(app_repo) / "gradle.properties"]
    properties: Dict[str, str] = {}
    for path in property_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = re.match(r"\s*(org\.gradle\.java\.(?:home|installations\.(?:paths|fromEnv)))\s*[=:]\s*(.*?)\s*$", line)
            if match and not line.lstrip().startswith(("#", "!")):
                properties.setdefault(match.group(1), _java_property_value(match.group(2)))

    homes: List[tuple[Path, str]] = []
    java_home = properties.get("org.gradle.java.home")
    if java_home and not daemon_criteria:
        homes.append((Path(java_home).expanduser(), "Gradle org.gradle.java.home"))
    for value in properties.get("org.gradle.java.installations.paths", "").split(","):
        if value.strip():
            homes.append((Path(value.strip()).expanduser(), "Gradle installations.paths"))
    for name in properties.get("org.gradle.java.installations.fromEnv", "").split(","):
        env_home = os.environ.get(name.strip(), "")
        if env_home:
            homes.append((Path(env_home).expanduser(), "Gradle fromEnv:" + name.strip()))
    managed_jdks = gradle_home / "jdks"
    try:
        for home in managed_jdks.iterdir():
            if home.is_dir() and (home / "bin" / ("java.exe" if os.name == "nt" else "java")).is_file():
                homes.append((home, "Gradle User Home/jdks"))
    except OSError:
        pass

    path_java = shutil.which("java.exe" if os.name == "nt" else "java")
    if os.environ.get("JAVA_HOME"):
        homes.insert(0, (Path(os.environ["JAVA_HOME"]).expanduser(), "JAVA_HOME"))
    elif path_java:
        homes.insert(0, (Path(path_java).resolve().parent.parent, "PATH"))
    unique: List[tuple[Path, str]] = []
    seen = set()
    for home, source in homes:
        key = os.path.normcase(str(home.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append((home, source))
    return unique


def _inspect_java_candidates(app_repo: str, *, daemon_criteria: bool = False) -> List[Check]:
    candidates: List[Check] = []
    for home, source in _gradle_java_homes(app_repo, daemon_criteria=daemon_criteria):
        exe = home / "bin" / ("java.exe" if os.name == "nt" else "java")
        if not exe.is_file():
            candidates.append(Check("JDK 候选", False, found=str(home), detail=source + "；未找到 bin/java"))
            continue
        try:
            out = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=10,
                                 env={**os.environ, "JAVA_HOME": str(home)})
            raw = out.stderr or out.stdout
            line = next((item.strip() for item in raw.splitlines()
                         if re.search(r'\bversion\s+"?\d', item, re.I)), "")
            match = re.search(r'\bversion\s+"?(\d+)', line, re.I)
            major = int(match.group(1)) if match else 0
            if major == 1:
                legacy = re.search(r'\bversion\s+"?1\.(\d+)', line, re.I)
                major = int(legacy.group(1)) if legacy else major
            ok = out.returncode == 0 and major > 0
            candidates.append(Check("JDK 候选", ok, found=str(exe) if ok else str(home),
                                    detail=source + "；" + (line or "无法解析 Java 版本"), version=major or None))
        except Exception as exc:
            candidates.append(Check("JDK 候选", False, found=str(home),
                                    detail=source + "；执行失败：" + type(exc).__name__))
    return candidates


def _java_candidate_requirement_check(name: str, candidates: List[Check], required: Optional[int],
                                      detail_prefix: str, exact: bool = False) -> Check:
    if required is None:
        return Check(name, False, detail=detail_prefix, hint="无法从 Gradle 配置推导 Java 要求")
    valid = [item for item in candidates if item.ok and item.version is not None and
             (item.version == required if exact else item.version >= required)]
    items = [{"path": item.found, "result": item.detail} for item in candidates]
    discovery_note = "候选来自 JAVA_HOME/PATH、Gradle 显式属性及 User Home/jdks；未复刻 Gradle 的全部自动发现来源"
    if valid:
        selected = valid[0]
        rule = "版本必须等于 Java %d" % required if exact else "版本至少为 Java %d" % required
        return Check(name, True, found=selected.found,
                     detail="%s：%s；候选满足：%s。%s" %
                     (detail_prefix, rule, selected.detail, discovery_note),
                     items=items, version=selected.version)
    rule = "版本必须等于 Java %d" % required if exact else "版本至少为 Java %d" % required
    return Check(name, False, detail="%s：未发现满足要求（%s）的显式候选。%s" %
                 (detail_prefix, rule, discovery_note),
                 hint="检查 Gradle Java 安装配置，或在 JAVA_HOME/PATH 中提供所需 JDK",
                 items=items)


def _project_java_requirements(app_repo: str) -> Dict[str, Any]:
    """从 wrapper、daemon criteria 与 app 编译配置读取 Java 版本要求。"""
    root = Path(app_repo)
    requirements: Dict[str, Any] = {"wrapper": None, "daemon": None, "toolchain": None}
    wrapper = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    try:
        text = wrapper.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"distributionUrl=.*?gradle-(\d+(?:\.\d+)+)(?:-[^/\\]+)?-(?:bin|all)\.zip", text)
        if match:
            requirements["wrapper"] = match.group(1)
    except OSError:
        pass

    daemon = root / "gradle" / "gradle-daemon-jvm.properties"
    try:
        for line in daemon.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"\s*toolchainVersion\s*=\s*(\d+)\s*$", line)
            if match:
                requirements["daemon"] = int(match.group(1))
                break
    except OSError:
        pass

    for relative in ("app/build.gradle.kts", "app/build.gradle"):
        try:
            text = (root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        versions = [int(value) for value in re.findall(
            r"JavaLanguageVersion\.of\s*\(\s*(\d+)", text)]
        versions.extend(int(value) for value in re.findall(
            r"jvmToolchain\s*\(\s*(\d+)\s*\)", text))
        if versions:
            requirements["toolchain"] = max(versions)
            break
    return requirements


def _java_requirement_check(name: str, java: Check, required: Optional[int],
                            missing_hint: str, detail_prefix: str,
                            exact: bool = False) -> Check:
    if required is None:
        return Check(name, False, detail=detail_prefix, hint=missing_hint)
    if java.version is None:
        return Check(name, False, detail=detail_prefix,
                     hint="先修复 Gradle 启动 JVM；无法确认当前 Java 版本")
    ok = java.version == required if exact else java.version >= required
    relation = "必须匹配" if exact else "至少需要"
    detail = "%s：%s Java %d，当前 Java %d" % (detail_prefix, relation, required, java.version)
    return Check(name, ok, found=java.found if ok else "", detail=detail,
                 hint="当前 JAVA_HOME/PATH 的 Java %d 不满足该项要求 Java %d；请安装并选择对应 JDK"
                 % (java.version, required) if not ok else "",
                 version=java.version)


def _java_property_value(raw: str) -> str:
    """解开 local.properties 中 Java Properties 的常见反斜杠转义。"""
    out: List[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            i += 1
            escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
            out.append(escapes.get(raw[i], raw[i]))
        else:
            out.append(raw[i])
        i += 1
    return "".join(out)


def _project_compile_sdk(app_repo: str) -> Optional[str]:
    """从常见 Groovy / Kotlin DSL 声明读取 compileSdk；无法判读时返回 None。"""
    for relative in ("app/build.gradle.kts", "app/build.gradle",
                     "build.gradle.kts", "build.gradle"):
        path = Path(app_repo) / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(
            r"(?m)^\s*compileSdk(?:Version)?\s*(?:=\s*|\(\s*)?"
            r"(?:[\"']android-)?(\d+)[\"']?\s*\)?\s*$", text)
        if match:
            return "android-" + match.group(1)
    return None


def _local_sdk_dir(app_repo: str, tried: List[Dict[str, str]]) -> Optional[Path]:
    path = Path(app_repo) / "local.properties"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"\s*sdk\.dir\s*[=:]\s*(.*)", line)
            if match:
                value = _java_property_value(match.group(1).strip())
                tried.append({"path": str(path), "result": "sdk.dir=" + value})
                return Path(value).expanduser()
    except OSError as exc:
        tried.append({"path": str(path), "result": "读取失败：" + str(exc)})
    return None


def _matching_android_platform(platforms_dir: Path, required_platform: str) -> Optional[Path]:
    """Find an installed platform by Android API metadata, not only directory name.

    Newer SDK repositories may name API 37 packages ``android-37.0`` or
    ``android-37.1`` while the project still declares ``compileSdk = 37``.
    ``source.properties`` is the package metadata SDK Manager uses for that
    distinction; accepting a matching API level avoids a false negative from
    string-concatenating ``android-<compileSdk>``.
    """
    match = re.fullmatch(r"android-(\d+)", required_platform)
    if not match or not platforms_dir.is_dir():
        return None
    api_level = match.group(1)
    try:
        entries = sorted(platforms_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or not (entry / "android.jar").is_file():
            continue
        try:
            text = (entry / "source.properties").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        api = re.search(
            r"(?m)^\s*AndroidVersion\.ApiLevel\s*=\s*(\d+)(?:\.\d+)?\s*$",
            text)
        if api and api.group(1) == api_level:
            return entry
    return None


def _sdk_roots_from_path() -> List[tuple[Path, str]]:
    """从 PATH 中的 Android 工具反推 SDK 根目录；只作辅助候选，不扫描磁盘。"""
    candidates: List[tuple[Path, str]] = []
    commands = ("sdkmanager", "sdkmanager.bat", "avdmanager", "avdmanager.bat",
                "adb", "adb.exe")
    for command in commands:
        tool = shutil.which(command)
        if not tool:
            continue
        path = Path(tool).expanduser()
        parents = list(path.parents)
        for index, parent in enumerate(parents):
            if parent.name in {"platform-tools", "build-tools", "tools"}:
                if index + 1 < len(parents):
                    candidates.append((parent.parent, "PATH: " + command))
                break
            if (parent.name == "bin" and index + 2 < len(parents)
                    and parents[index + 2].name == "cmdline-tools"):
                if index + 3 < len(parents):
                    candidates.append((parents[index + 3], "PATH: " + command))
                break
    return candidates


def _android_platform(platforms_dir: Path, required_platform: str) -> tuple[Optional[Path], str]:
    """返回有效平台目录及失败原因；平台目录必须同时有 android.jar 和 API 元数据。"""
    required = platforms_dir / required_platform
    if required.is_dir():
        if not (required / "android.jar").is_file():
            return None, "目标平台目录缺少 android.jar"
        try:
            text = (required / "source.properties").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return None, "目标平台目录缺少 source.properties，无法确认 API 版本"
        match = re.search(
            r"(?m)^\s*AndroidVersion\.ApiLevel\s*=\s*(\d+)(?:\.\d+)?\s*$",
            text)
        required_api = required_platform.removeprefix("android-")
        if not match or match.group(1) != required_api:
            return None, "目标平台 source.properties 无法确认 API " + required_api
        return required, ""
    matched = _matching_android_platform(platforms_dir, required_platform)
    if matched is not None:
        return matched, ""
    return None, "缺少项目要求的平台 " + required_platform


def _find_android_sdk(app_repo: str, configured_sdk_dir: str = "") -> Check:
    tried: List[Dict[str, str]] = []
    required_platform = _project_compile_sdk(app_repo) if app_repo else None
    local = _local_sdk_dir(app_repo, tried) if app_repo else None
    if not required_platform:
        return Check("Android SDK", False,
                     hint="无法从 App Gradle 配置读取固定 compileSdk；请检查 app/build.gradle(.kts)",
                     items=tried)
    configured = Path(configured_sdk_dir).expanduser() if configured_sdk_dir.strip() else None
    if local and configured:
        local_key = os.path.normcase(str(local.resolve(strict=False)))
        configured_key = os.path.normcase(str(configured.resolve(strict=False)))
        if local_key != configured_key:
            return Check(
                "Android SDK", False,
                detail="项目 local.properties 与管理台 SDK 目录不一致",
                hint="请保留一个配置：修改 local.properties 的 sdk.dir，或清空管理台 SDK 目录",
                items=tried)
    env_home = os.environ.get("ANDROID_HOME", "").strip()
    env_root = os.environ.get("ANDROID_SDK_ROOT", "").strip()
    if local:
        candidates = [(local, "App local.properties")]
    elif configured:
        candidates = [(configured, "管理台 Android SDK 目录")]
    else:
        candidates = []
        if env_home:
            candidates.append((Path(env_home).expanduser(), "ANDROID_HOME"))
        if env_root:
            candidates.append((Path(env_root).expanduser(), "ANDROID_SDK_ROOT"))
        candidates.extend(_sdk_roots_from_path())
        # Android Studio 的默认安装位作最后兜底：单一路径探测，不算扫盘
        studio_default = (Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"
                          if os.name == "nt" else Path.home() / "Android" / "Sdk")
        candidates.append((studio_default, "Android Studio 默认目录"))
    if env_home and env_root and os.path.normcase(env_home) != os.path.normcase(env_root):
        tried.append({"path": env_root, "result": "与 ANDROID_HOME 不一致；按候选顺序检查"})
    seen = set()
    existing_root = False
    partial_root = False
    invalid_reason = ""
    for c, source in candidates:
        c = c.expanduser()
        key = os.path.normcase(str(c.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        if not c.is_dir():
            tried.append({"path": str(c), "result": source + "；目录不存在"})
            continue
        existing_root = True
        platforms_dir = c / "platforms"
        if not platforms_dir.is_dir():
            # 约束①：platform-tools 之类的分发包不是完整 SDK 根——说明原因并
            # 换下一个候选，而不是冒充「缺平台」
            partial_root = True
            tried.append({"path": str(c), "result": source
                          + "；不是完整 SDK 根（缺少 platforms/，多半只是 platform-tools 分发包）"})
            continue
        matched, reason = _android_platform(platforms_dir, required_platform)
        if matched is not None:
            tried.append({"path": str(c), "result": source + "；包含所需 " + required_platform})
            return Check("Android SDK", True, found=str(c),
                         detail="项目要求 " + required_platform + "；实际平台目录 " + matched.name,
                         items=tried,
                         metadata={"source": source, "platform": matched.name})
        invalid_reason = reason or invalid_reason
        tried.append({"path": str(c), "result": source + "；" + reason})
    if invalid_reason:
        return Check("Android SDK", False, detail=invalid_reason,
                     hint=("已找到完整 SDK 根目录，但缺少项目要求的平台 " + required_platform
                           + "；请安装对应平台后重新检查"), items=tried)
    if partial_root:
        return Check("Android SDK", False,
                     detail="发现的候选目录不是完整 SDK 根（缺少 platforms/ 目录）",
                     hint=("PATH 上的 adb 多半来自 platform-tools 分发包；"
                           "请在设置 → JVM 校验里选择完整的 SDK 根目录"), items=tried)
    return Check("Android SDK", False,
                 detail="候选 SDK 根目录不存在或不可读",
                 hint="未发现可用的 Android SDK 根目录；请配置 local.properties、环境变量，或选择 SDK 目录",
                 items=tried)


def _find_gradle_home(app_repo: str) -> Check:
    tried: List[Dict[str, str]] = []
    env = os.environ.get("GRADLE_USER_HOME")
    candidate = Path(env).expanduser() if env else Path.home() / ".gradle"
    source = "GRADLE_USER_HOME" if env else "Gradle 默认目录"
    parent = candidate if candidate.exists() else candidate.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = (candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)) or (
        not candidate.exists() and parent.is_dir() and os.access(parent, os.W_OK | os.X_OK))
    result = "可写" if writable else "不存在且父目录不可写，或目录不可写"
    tried.append({"path": str(candidate), "result": source + "；" + result})
    if writable:
        detail = "已有目录可写" if candidate.exists() else "目录尚不存在，Gradle 可在可写父目录下创建"
        return Check("Gradle 用户目录", True, found=str(candidate), detail=detail, items=tried)
    return Check("Gradle 用户目录", False, hint="修复该目录权限，或设置有效的 GRADLE_USER_HOME", items=tried)


def process_environment(result: Dict[str, Any], base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构造实际交给 Gradle 启动器的环境；只接受同一次自检解析出的路径。"""
    runtime = result.get("runtime")
    if not result.get("ok") or not isinstance(runtime, dict) or not runtime:
        raise ValueError("JVM 环境自检未通过，不能启动 Gradle")
    env = dict(os.environ if base is None else base)
    env.update({
        "LEGADO_REPO": str(runtime["app_repo"]),
        "JAVA_HOME": str(runtime["java_home"]),
        "ANDROID_HOME": str(runtime["android_sdk"]),
        "ANDROID_SDK_ROOT": str(runtime["android_sdk"]),
        "GRADLE_USER_HOME": str(runtime["gradle_user_home"]),
    })
    return env


def readiness(app_repo: str, configured_sdk_dir: str = "") -> Dict[str, Any]:
    """发现并校验一次 JVM 执行环境，返回可冻结的 readiness 契约。

    每次调用都重新探测当前配置与工具链，避免不完整的失效键返回旧结论。
    """
    app_repo = (app_repo or "").strip()
    configured_sdk_dir = (configured_sdk_dir or "").strip()
    repo_ok = bool(app_repo) and (Path(app_repo) / "gradlew.bat").exists() or \
        bool(app_repo) and (Path(app_repo) / "gradlew").exists()
    checks: List[Check] = []

    supported_platform = os.name == "nt"
    checks.append(Check(
        "运行平台", supported_platform,
        found="Windows" if supported_platform else os.name,
        hint="当前 Gradle 启动器仅支持 Windows；此平台没有对应的启动器"))

    if not app_repo:
        checks.append(Check("App 源码目录", False, hint="先在上方填入 legado-with-MD3 的本地路径"))
    elif not repo_ok:
        checks.append(Check("App 源码目录", False, found=app_repo,
                            hint="目录里没有 gradlew(.bat)——确认填的是仓库根"))
    else:
        checks.append(Check("App 源码目录", True, found=app_repo))

    if repo_ok:
        launcher = Path(__file__).resolve().parents[1] / "appservice" / "legado-gradle.bat"
        checks.append(Check(
            "启动器（appservice/legado-gradle.bat）", supported_platform and launcher.is_file(),
            found=str(launcher) if supported_platform and launcher.is_file() else "",
            hint=("当前没有非 Windows 启动器" if not supported_platform else
                  "管理仓库根下缺 appservice/ 目录——git pull 或重新检出")))

        java = _find_java()
        checks.append(java)
        requirements = _project_java_requirements(app_repo)
        wrapper_version = requirements["wrapper"]
        wrapper_ok = wrapper_version is not None
        checks.append(Check(
            "Gradle wrapper", wrapper_ok,
            found="Gradle " + wrapper_version if wrapper_ok else "",
            detail="从 gradle/wrapper/gradle-wrapper.properties 读取",
            hint="无法读取 wrapper 版本；检查 distributionUrl 配置" if not wrapper_ok else ""))
        wrapper_major = int(wrapper_version.split(".", 1)[0]) if wrapper_version else None
        client_min = 17 if wrapper_major is not None and wrapper_major >= 9 else 8
        checks.append(_java_requirement_check(
            "Gradle 启动 JVM", java, client_min,
            "无法从 Gradle wrapper 版本推导启动 JVM 要求",
            "Gradle %s 客户端运行要求" % (wrapper_version or "wrapper")))
        toolchain_candidates = _inspect_java_candidates(app_repo)
        daemon_candidates = _inspect_java_candidates(
            app_repo, daemon_criteria=bool(requirements["daemon"]))
        checks.append(_java_candidate_requirement_check(
            "Gradle daemon JVM", daemon_candidates,
            requirements["daemon"] or java.version,
            "gradle/gradle-daemon-jvm.properties" if requirements["daemon"] else
            "未配置 daemon criteria；要求与启动 JVM 版本一致", exact=bool(requirements["daemon"])))
        checks.append(_java_candidate_requirement_check(
            "项目编译 toolchain", toolchain_candidates, requirements["toolchain"],
            "app/build.gradle(.kts) 中的 Java 配置", exact=True))
        checks.append(_find_android_sdk(app_repo if repo_ok else "", configured_sdk_dir))
        gradle_home = _find_gradle_home(app_repo if repo_ok else "")
        checks.append(gradle_home)
        if not wrapper_ok:
            checks.append(Check(
                "Gradle Wrapper 分发包", False,
                detail="无法检查：Wrapper distributionUrl 不可用",
                hint="先修复 Gradle wrapper 配置"))
        elif not gradle_home.ok:
            checks.append(Check(
                "Gradle Wrapper 分发包", False,
                detail="无法检查：Gradle 用户目录不可用",
                hint="先修复 Gradle 用户目录"))
        else:
            try:
                wrapper_cache = distribution_status(app_repo, gradle_home.found)
            except (GradleDistributionError, OSError) as exc:
                wrapper_cache = {"status": "invalid", "reason": str(exc)}
            cache_ok = wrapper_cache.get("status") == "ready"
            checks.append(Check(
                "Gradle Wrapper 分发包", cache_ok,
                found=wrapper_cache.get("cache_dir", "") if cache_ok else "",
                detail=("缓存已就绪（%s）" % wrapper_cache.get("source", "archive")
                        if cache_ok else wrapper_cache.get("reason", "缓存中没有分发包")),
                hint=("执行 `uv run python scripts/prepare_gradle.py` 准备 Gradle 分发包"
                      if not cache_ok else "")))

    ok = all(c.ok for c in checks)
    by_name = {c.name: c for c in checks}
    java = by_name.get("Java 安装")
    launcher_check = by_name.get("启动器（appservice/legado-gradle.bat）")
    wrapper_check = by_name.get("Gradle wrapper")
    client_check = by_name.get("Gradle 启动 JVM")
    daemon_check = by_name.get("Gradle daemon JVM")
    toolchain_check = by_name.get("项目编译 toolchain")
    sdk = by_name.get("Android SDK")
    gradle = by_name.get("Gradle 用户目录")
    distribution = by_name.get("Gradle Wrapper 分发包")
    runtime = {}
    wrapper_url = ""
    wrapper_cache = ""
    if repo_ok:
        try:
            wrapper_url = wrapper_distribution_url(app_repo)
        except (GradleDistributionError, OSError):
            wrapper_url = ""
        if distribution:
            wrapper_cache = str(distribution.found or "")
    if (repo_ok and java and java.ok and sdk and sdk.ok and gradle and gradle.ok
            and launcher_check and launcher_check.ok and wrapper_check and wrapper_check.ok
            and client_check and client_check.ok and daemon_check and daemon_check.ok
            and toolchain_check and toolchain_check.ok and distribution and distribution.ok):
        java_exe = Path(java.found).resolve()
        app_root = Path(app_repo).expanduser().resolve()
        daemon_exe = str(daemon_check.found)
        toolchain_exe = str(toolchain_check.found)
        runtime = {
            "app_repo": str(app_root),
            "java_exe": str(java_exe),
            "java_home": str(java_exe.parent.parent),
            "java": {
                "role": "gradle_client",
                "exe": str(java_exe),
                "home": str(java_exe.parent.parent),
                "version": java.version,
            },
            "daemon_java": {
                "home": _path_from_java_exe(daemon_exe),
                "exe": daemon_exe,
                "version": daemon_check.version,
                "source": "gradle-daemon-jvm.properties" if requirements["daemon"]
                          else "discovered",
            },
            "toolchain_java": {
                "home": _path_from_java_exe(toolchain_exe),
                "exe": toolchain_exe,
                "version": toolchain_check.version,
                "source": "app/build.gradle(.kts)",
            },
            "android_sdk": str(Path(sdk.found).expanduser().resolve()),
            "compile_sdk": _project_compile_sdk(app_repo),
            "android_sdk_source": sdk.metadata.get("source", ""),
            "android_platform": sdk.metadata.get("platform", ""),
            "gradle_user_home": str(Path(gradle.found).expanduser().resolve()),
            "launcher": str(Path(launcher_check.found).resolve()),
            "wrapper_version": requirements["wrapper"],
            "wrapper_distribution_url": wrapper_url,
            "wrapper_distribution_cache": wrapper_cache,
            "launch_mode": "daemon_or_gradle",
        }
    checks_payload = [_check_payload(c) for c in checks]
    stable_payload = {
        "repo_ok": repo_ok,
        "runtime": runtime,
        "checks": checks_payload,
    }
    result = {
        "ok": ok,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fingerprint": _fingerprint(stable_payload),
        "checks": checks_payload,
        "repo_ok": repo_ok,
        "runtime": runtime,
    }
    return result

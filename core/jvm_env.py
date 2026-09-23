# -*- coding: utf-8 -*-
"""JVM 校验服务的环境推导与自检（S2）。

**设计约束**（TODO `jvm-env-readiness`）：App 仓库配置优先决定 SDK / Java 要求；
运行时只从显式环境变量、`PATH`、`local.properties` 和 Gradle 项目配置发现，
不猜机器安装目录。Gradle 用户目录使用明确环境变量或 Gradle 默认目录。
自检不安装工具；它只做路径、版本、所需 SDK 平台和可写性检查。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


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


def _find_android_sdk(app_repo: str) -> Check:
    tried: List[Dict[str, str]] = []
    required_platform = _project_compile_sdk(app_repo) if app_repo else None
    local = _local_sdk_dir(app_repo, tried) if app_repo else None
    if not required_platform:
        return Check("Android SDK", False,
                     hint="无法从 App Gradle 配置读取固定 compileSdk；请检查 app/build.gradle(.kts)",
                     items=tried)
    env_home = os.environ.get("ANDROID_HOME")
    env_root = os.environ.get("ANDROID_SDK_ROOT")
    if local:
        candidates = [(local, "App local.properties")]
    elif env_home:
        candidates = [(Path(env_home).expanduser(), "ANDROID_HOME")]
    elif env_root:
        candidates = [(Path(env_root).expanduser(), "ANDROID_SDK_ROOT")]
    else:
        candidates = []
    if env_home and env_root and os.path.normcase(env_home) != os.path.normcase(env_root):
        tried.append({"path": env_root, "result": "与 ANDROID_HOME 冲突；按 ANDROID_HOME 检查"})
    seen = set()
    for c, source in candidates:
        c = c.expanduser()
        key = os.path.normcase(str(c.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        platform = c / "platforms"
        required = platform / required_platform
        if required.is_dir():
            tried.append({"path": str(c), "result": source + "；包含所需 " + required_platform})
            return Check("Android SDK", True, found=str(c),
                         detail="项目要求 " + required_platform, items=tried)
        tried.append({"path": str(c), "result": source + "；缺少 platforms/" + required_platform})
    return Check("Android SDK", False,
                 hint="使用 sdkmanager 安装 `platforms;" + required_platform + "`，或在 App 仓库 local.properties 配置 sdk.dir",
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


def selftest(app_repo: str) -> Dict[str, Any]:
    """全量自检：项目、Gradle client/daemon/toolchain Java、SDK、Gradle 用户目录与启动器。"""
    app_repo = (app_repo or "").strip()
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
        checks.append(_find_android_sdk(app_repo if repo_ok else ""))
        checks.append(_find_gradle_home(app_repo if repo_ok else ""))

    ok = all(c.ok for c in checks)
    by_name = {c.name: c for c in checks}
    java = by_name.get("Java 安装")
    sdk = by_name.get("Android SDK")
    gradle = by_name.get("Gradle 用户目录")
    runtime = {}
    if repo_ok and java and java.ok and sdk and sdk.ok and gradle and gradle.ok:
        java_exe = Path(java.found).resolve()
        app_root = Path(app_repo).expanduser().resolve()
        runtime = {
            "app_repo": str(app_root),
            "java_exe": str(java_exe),
            "java_home": str(java_exe.parent.parent),
            "android_sdk": str(Path(sdk.found).expanduser().resolve()),
            "gradle_user_home": str(Path(gradle.found).expanduser().resolve()),
        }
    return {
        "ok": ok,
        "checks": [c.__dict__ for c in checks],
        "repo_ok": repo_ok,
        "runtime": runtime,
    }

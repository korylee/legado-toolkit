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
            tried.append({"path": str(exe), "result": ver.strip()[:60]})
            if major >= 17:
                return Check("JDK", True, found=str(exe), detail=source + "；" + ver.strip(),
                             items=tried)
            tried[-1]["result"] += "（版本低于 17，不满足 Gradle 9 要求）"
        except Exception as e:
            tried.append({"path": str(exe), "result": "执行失败: %s" % type(e).__name__})
    return Check("JDK", False, hint="安装 JDK 17+（Gradle daemon 需要 21，推荐直接装 21）",
                 items=tried)


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
    """全量自检：app_repo → 仓库可用性 → JDK → SDK → gradle-home → 启动器是否在。"""
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

        checks.append(_find_java())
        checks.append(_find_android_sdk(app_repo if repo_ok else ""))
        checks.append(_find_gradle_home(app_repo if repo_ok else ""))

    ok = all(c.ok for c in checks)
    by_name = {c.name: c for c in checks}
    java = by_name.get("JDK")
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

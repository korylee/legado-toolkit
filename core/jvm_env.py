# -*- coding: utf-8 -*-
"""JVM 校验服务的环境推导与自检（S2）。

**设计约束**（TODO §一「JVM 校验服务」）：
- 设置里只有一个路径输入（App 源码目录）；JDK / Android SDK / Gradle 用户目录
  **全部在这里推导**，不让用户填——尤其 gradle-home「必须与仓库同盘」，
  让用户填它等于让手工维护一个算得出来的状态。
- 自检是**只读**的（探测路径存在性 + 版本号），不装任何东西；缺什么、
  在哪缺、怎么装，逐条返回给界面。

派生优先级（每项都按此链找，全部失败才报缺）：
  JDK          JAVA_HOME → vfox（`~/.vfox/sdks/java`）→ 常见安装目录（含
               Android Studio JBR）→ PATH 里的 java
  Android SDK  ANDROID_HOME → <repo>/local.properties 的 sdk.dir → 常见目录
  gradle-home  GRADLE_USER_HOME（设置进程的环境变量）→ **与仓库同盘的
               <盘>:\\.gradle**（KSP 的跨盘限制，lessons §四十八）
"""

from __future__ import annotations

import os
import re
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


def _vfox_java_candidates() -> List[Path]:
    """vfox 装过的 JDK（`VFOX_HOME` 或 `~/.vfox` 下的 `sdks/java`）。

    vfox 把 SDK 装在 ``<root>/sdks/<name>``，**java 插件两种布局都见过**：
    单版本时 JDK 根直接落在 ``sdks/java/``（里面有 bin/ release），多版本时在
    ``sdks/java/<version>/`` 下。两种都收，多版本按目录名倒序（取新版优先）——
    只认一种的话，另一种布局下会「明明装了却报找不到 JDK」。
    """
    root = Path(os.environ.get("VFOX_HOME") or (Path.home() / ".vfox"))
    base = root / "sdks" / "java"
    if not base.is_dir():
        return []
    if (base / "bin").is_dir():
        return [base]
    return sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)


def _common_jdk_candidates() -> List[Path]:
    out = [
        Path(os.environ["JAVA_HOME"]) if os.environ.get("JAVA_HOME") else None,
        # vfox 管理的 JDK 排在最前：用户既然用它管版本，就该以它为准
        *_vfox_java_candidates(),
        Path("C:/Program Files/Java"),
        Path("C:/Program Files/Eclipse Adoptium"),
        Path("D:/Program Files/Java"),
        Path("C:/Program Files/Android/Android Studio/jbr"),   # AS 自带 JBR
    ]
    return [p for p in out if p]


def _find_java() -> Check:
    tried: List[Dict[str, str]] = []
    # 1) JAVA_HOME
    jh = os.environ.get("JAVA_HOME", "")
    candidates: List[Path] = []
    if jh:
        candidates.append(Path(jh) / "bin/java.exe")
        candidates.append(Path(jh) / "bin/java")
    # 2) 常见安装目录下按版本号倒序找最新的 JDK 17+/21
    for d in _common_jdk_candidates():
        if d.is_file() and d.name.lower().startswith("java"):
            candidates.append(d)
        elif d.is_dir():
            for child in sorted(d.iterdir(), reverse=True):
                if child.is_dir() and re.match(r"(?i)jdk-?\d", child.name):
                    exe = child / "bin/java.exe"
                    if exe.exists():
                        candidates.append(exe)
                elif child.name.lower() == "bin":
                    exe = child / ("java.exe" if os.name == "nt" else "java")
                    if exe.exists():
                        candidates.append(exe)
    # 3) PATH 里的 java
    candidates.append(Path("java.exe" if os.name == "nt" else "java"))

    for exe in candidates:
        if not exe.exists() and not str(exe).lower().endswith(("java.exe", "java")):
            tried.append({"path": str(exe), "result": "不存在"})
            continue
        try:
            out = subprocess.run(
                [str(exe), "-version"], capture_output=True, text=True, timeout=10,
                env={**os.environ, "JAVA_HOME": str(exe.parent.parent)})
            ver = (out.stderr or out.stdout).splitlines()[0] if (out.stderr or out.stdout) else ""
            m = re.search(r'"?(\d+)\.?', ver)
            major = int(m.group(1)) if m else 0
            tried.append({"path": str(exe), "result": ver.strip()[:60]})
            if major >= 17:
                return Check("JDK", True, found=str(exe), detail=ver.strip(),
                             items=tried)
            tried[-1]["result"] += "（版本低于 17，不满足 Gradle 9 要求）"
        except Exception as e:
            tried.append({"path": str(exe), "result": "执行失败: %s" % type(e).__name__})
    return Check("JDK", False, hint="安装 JDK 17+（Gradle daemon 需要 21，推荐直接装 21）",
                 items=tried)


def _find_android_sdk(app_repo: str) -> Check:
    tried: List[Dict[str, str]] = []
    candidates: List[Path] = []
    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        candidates.append(Path(env))
    # 仓库的 local.properties（Gradle 官方途径）
    lp = Path(app_repo) / "local.properties" if app_repo else None
    if lp and lp.exists():
        try:
            for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*sdk\.dir\s*=\s*(.+)", line)
                if m:
                    candidates.append(Path(m.group(1).strip().replace("\\\\", "\\")))
                    tried.append({"path": str(lp), "result": "sdk.dir=" + m.group(1).strip()})
                    break
        except Exception:
            pass
    for d in (Path("C:/Android/Sdk"), Path("D:/Android/Sdk"),
              Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk"):
        candidates.append(d)
    seen = set()
    for c in candidates:
        if not c or str(c) in seen:
            continue
        seen.add(str(c))
        platform = c / "platforms"
        has_platform = platform.exists() and any(platform.iterdir()) if platform.exists() else False
        tried.append({"path": str(c), "result": "OK" if has_platform else "缺 platforms/"})
        if has_platform:
            plats = sorted(p.name for p in platform.iterdir())
            return Check("Android SDK", True, found=str(c),
                         detail="platforms: " + ", ".join(plats[-3:]), items=tried)
    return Check("Android SDK", False,
                 hint="安装 cmdline-tools 后 `sdkmanager \"platforms;android-37.0\" "
                      "\"build-tools;37.0.0\" platform-tools`；sdkmanager 卡住就用 curl 直拉",
                 items=tried)


def _find_gradle_home(app_repo: str) -> Check:
    tried: List[Dict[str, str]] = []
    env = os.environ.get("GRADLE_USER_HOME")
    if env:
        tried.append({"path": env, "result": "来自环境变量"})
        p = Path(env)
        if p.exists():
            return Check("Gradle 用户目录", True, found=str(p), items=tried,
                         detail="（必须与仓库同盘，否则 KSP 报跨盘错误）")
    # 与仓库同盘的 .gradle（没有就用不了的隐藏约束在这里消化掉）
    if app_repo:
        drive = Path(app_repo).anchor
        cand = Path(drive) / ".gradle"
        tried.append({"path": str(cand), "result": "已配置（与仓库同盘）"})
        return Check("Gradle 用户目录", True, found=str(cand), items=tried,
                     detail="按「与仓库同盘」推导；首次跑批由 Gradle 自动创建")
    return Check("Gradle 用户目录", False, hint="先填 App 源码目录", items=tried)


def selftest(app_repo: str) -> Dict[str, Any]:
    """全量自检：app_repo → 仓库可用性 → JDK → SDK → gradle-home → 启动器是否在。"""
    app_repo = (app_repo or "").strip()
    repo_ok = bool(app_repo) and (Path(app_repo) / "gradlew.bat").exists() or \
        bool(app_repo) and (Path(app_repo) / "gradlew").exists()
    checks: List[Check] = []

    if not app_repo:
        checks.append(Check("App 源码目录", False, hint="先在上方填入 legado-with-MD3 的本地路径"))
    elif not repo_ok:
        checks.append(Check("App 源码目录", False, found=app_repo,
                            hint="目录里没有 gradlew(.bat)——确认填的是仓库根"))
    else:
        checks.append(Check("App 源码目录", True, found=app_repo))

    if repo_ok:
        init_script = Path(app_repo).parent  # 不假设；启动器在管理仓库里
        checks.append(Check(
            "启动器（appservice/legado-gradle.bat）", Path("appservice/legado-gradle.bat").exists(),
            found="appservice/legado-gradle.bat" if Path("appservice/legado-gradle.bat").exists() else "",
            hint="管理仓库根下缺 appservice/ 目录——git pull 或重新检出"))

        checks.append(_find_java())
        checks.append(_find_android_sdk(app_repo if repo_ok else ""))
        checks.append(_find_gradle_home(app_repo if repo_ok else ""))

    ok = all(c.ok for c in checks)
    return {
        "ok": ok,
        "checks": [c.__dict__ for c in checks],
        "repo_ok": repo_ok,
        "app_repo": app_repo,
    }

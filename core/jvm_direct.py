"""「不起 Gradle 直起一次 JVM」的原语（常驻 daemon 与 CLI 共用）。

**为什么单独一个模块**：两个消费方——CLI（`scripts/jvm_debug_direct.py`）与
`core/jvm_daemon.py`，而常驻那条是**产品路径**：`core/` 不能反过来 import `scripts/`。
参数拼装（classpath / 系统属性 / jvmArgs / 堆）各写一份就会漂，漂的表现与
`core/jvm_debug` 当初拆出来的理由一样（「界面上跑出来的和命令行跑出来的不一样」）。

## dump 是什么、为什么必须有它

`appservice/legado-test.init.gradle` 按环境变量 `LEGADO_TEST_JVM_ENV_OUT` 把**测试 JVM 的
运行环境**（classpath / 系统属性 / jvmArgs / workingDir）写成 JSON。绕开 Gradle 直起就得拿
这份 dump——尤其**系统属性**：Robolectric 靠 AGP 注入的那几个定位 Android 资源，
Gradle 里是隐式给的、看不见。

**dump 是编译后的产物**，所以「它比 `.kt` 新」等价于「这份类是新编译的」——
`core.jvm_debug._run_launcher` 每次 Gradle 跑测都顺手刷它，靠的就是这条不变式
（只改一边 = 常驻被永久挡住，或跑旧类）。

## 三条平台事实（改回去会静默坏）

- classpath 会长到**几万字符**（远超 Windows 命令行的 32767 上限）→ 走 `java @argfile`；
  argfile 里 `\\` 要翻倍（JDK 的 `@file` 解析把它当转义符）。
- `javaLauncher` 的 `installationPath` 是 **JDK 根目录**，不是可执行文件。
- `maxHeapSize`（3g，为全量跑批 OOM 设的）**不在 jvmArgs 里**，得自己补 `-Xmx`。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.jvm_debug import AGSVC, LAUNCHER, LAUNCHER_CLASS
from core.paths import data_path

DUMP_NAME = "test_jvm_env.json"
#: refresh 用最便宜的那个测试：dump 挂在**所有** test 任务上，跑哪个都能拿到，
#: 而 `DebugServiceLauncher` 会真跑一次调试，白花一次站点请求。
REFRESH_TEST = "io.legado.app.service.ServiceJsonTest"


class DumpSchemaError(ValueError):
    """dump 不能安全用于直起/常驻时的结构错误。"""


def _dump_error(issues: List[str]) -> DumpSchemaError:
    details = "；".join(issues) or "结构不是对象"
    return DumpSchemaError(
        "JVM dump 字段无效：%s；请运行一次 `scripts/jvm_debug_direct.py --refresh`"
        % details)


def validate_dump(dump: Any) -> Dict[str, Any]:
    """校验直起所需的完整运行环境，禁止缺字段时继承调用方状态。

    `javaLauncher` / `javaHomeEnv` 是两个明确的 Java 来源，至少有一个非空即可；
    `maxHeapSize` 是 Gradle 任务的显式配置，缺失时不能静默退回 JVM 默认堆。
    `jvmArgs` 与 `systemProperties` 可以是空集合，但键和集合类型必须存在且正确。
    """
    if not isinstance(dump, dict):
        raise _dump_error([])

    issues: List[str] = []

    def required_string(name: str) -> Optional[str]:
        if name not in dump:
            issues.append("缺少 %s" % name)
            return None
        value = dump[name]
        if not isinstance(value, str) or not value.strip():
            issues.append("%s 必须是非空字符串" % name)
            return None
        return value

    required_string("workingDir")
    required_string("classpath")
    required_string("maxHeapSize")

    if "environment" not in dump:
        issues.append("缺少 environment")
    elif not isinstance(dump["environment"], dict):
        issues.append("environment 必须是对象")
    else:
        for key, value in dump["environment"].items():
            if not isinstance(key, str) or not key:
                issues.append("environment 的键必须是非空字符串")
                break
            if not isinstance(value, str):
                issues.append("environment.%s 必须是字符串" % key)
                break

    if "jvmArgs" not in dump:
        issues.append("缺少 jvmArgs")
    elif not isinstance(dump["jvmArgs"], list):
        issues.append("jvmArgs 必须是数组")
    elif any(not isinstance(value, str) or not value.strip()
             for value in dump["jvmArgs"]):
        issues.append("jvmArgs 的每一项必须是非空字符串")

    if "systemProperties" not in dump:
        issues.append("缺少 systemProperties")
    elif not isinstance(dump["systemProperties"], dict):
        issues.append("systemProperties 必须是对象")
    else:
        for key, value in dump["systemProperties"].items():
            if not isinstance(key, str) or not key:
                issues.append("systemProperties 的键必须是非空字符串")
                break
            if value is not None and not isinstance(value, str):
                issues.append("systemProperties.%s 必须是字符串或 null" % key)
                break

    java_launcher = dump.get("javaLauncher")
    java_home = dump.get("javaHomeEnv")
    if java_launcher is not None and (not isinstance(java_launcher, str)
                                      or not java_launcher.strip()):
        issues.append("javaLauncher 必须是非空字符串或 null")
    if java_home is not None and (not isinstance(java_home, str)
                                  or not java_home.strip()):
        issues.append("javaHomeEnv 必须是非空字符串或 null")
    if not ((isinstance(java_launcher, str) and java_launcher.strip())
            or (isinstance(java_home, str) and java_home.strip())):
        issues.append("javaLauncher 与 javaHomeEnv 至少一个必须有值")

    if issues:
        raise _dump_error(issues)
    return dump


def dump_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", DUMP_NAME))


def newest_kt() -> float:
    """`appservice/test` 下 Kotlin 源码的最新 mtime——判断 dump 是不是过期了。"""
    src = AGSVC / "test"
    return max((p.stat().st_mtime for p in src.rglob("*.kt")), default=0.0)


def dump_is_stale() -> bool:
    p = dump_path()
    return (not p.exists()) or newest_kt() > p.stat().st_mtime


def refresh(timeout_min: int = 30) -> int:
    """拉一次 Gradle（带 dump 的环境变量），把测试 JVM 的环境写出来。

    **环境变量是唯一开关**：不设它，`legado-test.init.gradle` 与现在完全一样。
    """
    out = dump_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    env = {**os.environ, "LEGADO_TEST_JVM_ENV_OUT": str(out)}
    t0 = time.time()
    p = subprocess.run(
        ["cmd", "/c", str(LAUNCHER), ":app:testAppDebugUnitTest", "--tests", REFRESH_TEST,
         "--rerun"],
        cwd=str(AGSVC), env=env, capture_output=True, text=True, errors="replace",
        timeout=timeout_min * 60)
    cost = time.time() - t0
    if not out.exists():
        print("dump 失败（退出码 %d，%.1fs）——看 Gradle 输出里 [appservice] 那几行"
              % (p.returncode, cost))
        for ln in (p.stdout or "").strip().splitlines()[-8:]:
            print("  | " + ln)
        return 1
    try:
        load_dump(warn_stale=False)
    except (OSError, ValueError) as e:
        print("dump 结构无效：%s" % e)
        return 1
    print("dump 完成：%s（%.1fs）" % (out, cost))
    return 0


def load_dump(warn_stale: bool = True) -> Dict[str, Any]:
    p = dump_path()
    if not p.exists():
        raise FileNotFoundError(
            "没有 dump（%s）：先跑一次 `scripts/jvm_debug_direct.py --refresh`" % p)
    try:
        dump = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DumpSchemaError(
            "JVM dump 无法读取：%s；请运行一次 `scripts/jvm_debug_direct.py --refresh`" % e
        ) from e
    validate_dump(dump)
    if warn_stale and dump_is_stale():
        print("⚠️  dump 早于 appservice 的 Kotlin 源码——类可能不是最新的，"
              "建议先 `--refresh`（否则时间与结论都不是真实产物的）")
    return dump


def java_exe(dump: Dict[str, Any]) -> str:
    """用 Gradle 自己会 fork 的那个 JVM（dump 里的 `javaLauncher`），其次 JAVA_HOME。

    **`javaLauncher` 是 JDK 根目录**（`installationPath`），要自己拼 `bin/java`。
    """
    validate_dump(dump)
    exe_name = "java.exe" if os.name == "nt" else "java"
    cand = dump.get("javaLauncher")
    if cand:
        p = pathlib.Path(str(cand))
        return str(p / "bin" / exe_name) if p.is_dir() else str(p)
    jh = str(dump.get("javaHomeEnv") or "").rstrip("\\/")
    if jh:
        return str(pathlib.Path(jh) / "bin" / exe_name)
    raise DumpSchemaError("JVM dump 没有 Java 启动来源；请运行一次 `scripts/jvm_debug_direct.py --refresh`")


def _quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def java_argv(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS) -> List[str]:
    validate_dump(dump)
    argv: List[str] = []
    heap = dump["maxHeapSize"]
    # 堆要显式带上：它是 Test 任务的另一个属性、不在 jvmArgs 里，而它正是为
    # 「全量跑批 OOM」设的 3g——漏了就等于悄悄把堆退回默认值（实测过）
    argv.append("-Xmx" + heap)
    argv += list(dump["jvmArgs"])
    for k, v in dump["systemProperties"].items():
        if v is None:      # 空值不能写 -Dk=null：那是把 null 当字符串传进去
            continue
        argv.append("-D%s=%s" % (k, v))
    argv += ["-cp", dump["classpath"]]
    argv += ["org.junit.runner.JUnitCore", main_class]
    return argv


def write_argfile(argv: List[str], path: Optional[pathlib.Path] = None) -> pathlib.Path:
    p = path or (dump_path().parent / "jvm_direct.args")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(_quote(a) for a in argv) + "\n", encoding="utf-8", newline="\n")
    return p


def java_command(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS,
                 use_argfile: bool = True) -> List[str]:
    """完整命令行：`[java, @argfile]`（或展开的参数，`use_argfile=False` 时）。"""
    argv = java_argv(dump, main_class)
    if use_argfile:
        return [java_exe(dump), "@" + str(write_argfile(argv))]
    return [java_exe(dump)] + argv


def java_env(dump: Dict[str, Any]) -> Dict[str, str]:
    validate_dump(dump)
    # dump 记录的是 Gradle 测试 JVM 的完整环境；不能再合并当前调用方环境，
    # 否则缺失或漂移的变量会让直起和 Gradle 跑出不同结论。
    return dict(dump["environment"])


def run_direct(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS, timeout: int = 300,
               use_argfile: bool = True) -> Tuple[int, float, str, str]:
    """直起一次，返回 `(退出码, 墙钟秒, stdout, stderr)`（签名同 `_run_launcher`）。"""
    validate_dump(dump)
    cmd = java_command(dump, main_class, use_argfile=use_argfile)
    t0 = time.time()
    p = subprocess.run(cmd, cwd=dump["workingDir"], env=java_env(dump),
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return p.returncode, time.time() - t0, (p.stdout or ""), (p.stderr or "")


def direct_launcher(dump: Dict[str, Any], timeout: int = 300) -> Callable[[], Tuple[int, float, str, str]]:
    """给 `run_jvm_debug(launcher=...)` 用的可调用对象。"""
    def _run() -> Tuple[int, float, str, str]:
        return run_direct(dump, LAUNCHER_CLASS, timeout=timeout)
    return _run

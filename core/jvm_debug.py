# -*- coding: utf-8 -*-
"""JVM 调试通道：把「跑一次 App 调试链」收成可复用的模块。

**为什么单开一个模块**：这条链有两个消费方——CLI（`scripts/jvm_debug_run.py`）与后端
`POST /api/rules/jvm-debug`。参数拼装与产物解析各写一份就会漂，而漂的表现是「界面上跑
出来的和命令行跑出来的不一样」——排查时根本想不到是两份实现（lessons §二十三）。

**返回体与设备通道（`core/app_debug.run_app_debug`）同形状**
（`source/steps/pages/all_ok/events/error`），且事件流与设备 WS **逐事件同构**——
前端抽屉与卡片零改动就能吃（`build_steps` 一行不改）。

**两条边界**：① `pages` 是 `fetch_debug_pages` **补抓**的（抽屉的「整页源码」需要整页 HTML；
而**规则命中的那块 DOM** 现在由本机引擎自己带回来——侧车里的 `matched_html`，见
`build_steps` 的 `matched` 参数）；② **不代用户登录**：登录态来自用户自己的浏览器 profile（`scripts/jvm_login.py` 预热一次）或 `cookie=`
手工给，这里只把它转发给 JVM 侧。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.app_debug import build_steps, fetch_debug_pages, matched_map
from core.fetch import CACHE_AUTO
from core.paths import ARGS_PARTS, data_path

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGSVC = ROOT / "appservice"
#: 启动器的参数文件。**放在 data/ 下**（运行时数据，AGENTS #1）——它长在 `appservice/`
#: 只是历史约定：`legado-gradle.bat` 会 `pushd` 进 App 仓库，测试 JVM 的 CWD 是别人的
#: 目录，所以这个文件只能靠「挨着启动器」或环境变量定位。现在由我们写出绝对路径，
#: 并用 `LEGADO_APPSERVICE_ARGS` 交给那次 Gradle（见 `_run_launcher`）
ARGS = pathlib.Path(data_path(*ARGS_PARTS))
LAUNCHER = AGSVC / "legado-gradle.bat"
LAUNCHER_CLASS = "io.legado.app.service.DebugServiceLauncher"

#: 退出码 → 文案。**与 `DebugService.kt` 的常量一一对应**，改那边要改这里；
#: 契约钉在 `tests/test_jvm_debug_contract.py::test_exit_codes_match_the_service`。
CODE_TEXT = {
    0: "正常",
    2: "零事件（一条都没收到）",
    3: "超时（已收到的是部分结果）",
    4: "入参/输入错误或进程内异常",
    5: "事件流被截断（没等到终止事件）",
}

#: **跑批与调试共用一把锁**：两条链都写 `appservice/args.properties`（跑批写
#: keyword/depth，调试写 file/key/out），而那个文件是**另一个进程**的入参入口——
#: 同时跑会互相踩（参数被改写），那种失败看起来像「JVM 坏了」。D2 之后调试默认走常驻、
#: 不碰 Gradle，所以「抢同一个 Gradle 任务」只对**回落那条路**成立；锁仍然必须有，
#: 理由就是那个参数文件。非阻塞获取——拿不到就直说，**不排队**：排队会让界面上的一张
#: 卡片转十几分钟，那比一句「另一个任务在跑」糟得多。
#:
#: **两侧都必须真拿这把锁**（`run_jvm_debug` 与 `backend/api/jvm.py` 的跑批）：
#: 只有一侧拿，注释里那句「共用」就成了一个读起来像存在的保护（AGENTS #12）。
RUN_LOCK = threading.Lock()

#: 拿不到 `RUN_LOCK` 时的**同一句话**。跑批与调试是同一把锁、同一个原因，
#: 两处各写一份就会在界面上长得不一样，而用户看不出那其实是同一件事。
BUSY_REASON = ("另一个 JVM 任务在跑（跑批与调试共用同一个参数文件），"
               "等它跑完再来")


def _write_args(src_file: str, key: str, out_file: str, timeout: int, cookie: str = "") -> None:
    """写 args.properties。

    **必须 `newline="\n"`**：这是 git 跟踪的文件，而 `write_text` 在 Windows 上把 `\n`
    翻成 `\r\n`——跑一次就把工作区弄脏（内容与 HEAD 逐字节相同，只差行尾，
    `git diff` 连内容都不显示）。同 `agent-write-safety` §三。
    """
    lines = [
        "# 由 core/jvm_debug.py 生成（跑批时后端会重写它）",
        "file=%s" % pathlib.Path(src_file).as_posix(),
        "key=%s" % key,
        "out=%s" % pathlib.Path(out_file).as_posix(),
        "timeout=%d" % timeout,
    ]
    if cookie:
        lines.append("cookie=%s" % cookie)
    ARGS.parent.mkdir(parents=True, exist_ok=True)
    ARGS.write_text("\n".join(lines + [""]), encoding="utf-8", newline="\n")


def _run_launcher(timeout_min: int = 20) -> Tuple[int, float, str, str]:
    """拉一次 Gradle（阻塞）。返回 `(退出码, 墙钟秒, stdout, stderr)`。

    单独一个函数是为了让测试能把它换成假的——**不跑 Gradle 也能测组装逻辑**。

    **顺手刷新 dump**（S5-A 第二期 D2）：把 `LEGADO_TEST_JVM_ENV_OUT` 传给这次 Gradle，
    `legado-test.init.gradle` 的 `doFirst` 就会把测试 JVM 的环境重新 dump 一份。
    这条不变式是常驻那条链的前提——**「dump 比 .kt 新」等价于「这份类是新编译的」**：
    Gradle 一定会先编译，所以它跑过之后 dump 就是可信的；少了这一步，改一次 Kotlin
    就会让常驻一直被「类可能是旧的」挡在外面，直到有人手工 `--refresh`。
    """
    t0 = time.time()
    env = {**os.environ,
           "LEGADO_TEST_JVM_ENV_OUT": str(data_path("app_probe", "test_jvm_env.json")),
           # 参数文件在哪：**必须显式告诉它**（`AppserviceEnv.loadArgs` 的第 1 候选）。
           # 不给的话它退回「挨着启动器找」——那里现在没有这个文件，而表现是启动器
           # 打印一句「找不到 args.properties，跳过」之后**什么都不跑**
           "LEGADO_APPSERVICE_ARGS": str(ARGS)}
    p = subprocess.run(
        ["cmd", "/c", str(LAUNCHER), ":app:testAppDebugUnitTest",
         "--tests", LAUNCHER_CLASS, "--rerun"],
        cwd=str(AGSVC), env=env, capture_output=True, text=True, errors="replace",
        timeout=timeout_min * 60)
    return p.returncode, time.time() - t0, (p.stdout or ""), (p.stderr or "")


def _read_ndjson(path: str) -> List[Dict[str, Any]]:
    f = pathlib.Path(path)
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def _env_error() -> str:
    """环境不可用时的**可执行**提示（不自检就开跑，只会在 Gradle 里炸出一堆看不懂的错）。"""
    from core import jvm_env, settings_store

    conf = settings_store.load().get("jvm", {})
    st = jvm_env.selftest(conf.get("app_repo", ""))
    if st.get("ok"):
        return ""
    bad = [c for c in (st.get("checks") or []) if not c.get("ok")]
    first = bad[0] if bad else {}
    hint = first.get("hint") or "在「设置 → JVM 校验」里填 App 源码目录并自检"
    return "本机引擎不可用：%s —— %s" % (first.get("name") or "环境自检未通过", hint)


def default_launcher(notes: List[str]) -> Callable[[], Tuple[int, float, str, str]]:
    """产品默认的拉起方式（S5-A 第二期 D2）：**优先常驻 daemon，不可用回落 Gradle**。

    两条边界，都是实测教训：

    1. **dump 比 .kt 旧（或没有 dump）就不用常驻**：那意味着「这份类可能不是最新编译的」。
       常驻进程里跑的是**加载时那份类**，改了 `appservice/` 却用它，表现是「规则改了没生效」
       ——看起来像源/规则的问题，其实是我们在跑旧代码。Gradle 那条一定会先编译，
       所以这种情况下它才是权威路径（而且它跑完会顺手把 dump 刷新，见 `_run_launcher`）。
    2. **回落目标是 Gradle，不是「直起」**：直起同样依赖 dump 的新鲜度，而这里要的是
       「无论如何都能跑对」。`core/jvm_daemon` 内部自己的回落（daemon 半路死了）也走这一条。

    `notes` 是**回落的痕迹**：`run_jvm_debug` 会把它接到第一条 step 的附注上——
    静默降级会让「常驻一直没生效」永远没人发现（而用户只会觉得「也没快多少」）。
    """
    from core import jvm_daemon, jvm_direct

    try:
        if jvm_direct.dump_is_stale():
            return _run_launcher
        dump = jvm_direct.load_dump(warn_stale=False)
    except Exception as e:                       # dump 读不了（权限/损坏）也别挡住调试
        notes.append("这次没能用常驻进程（读 dump 失败：%s），已改用 Gradle（启动慢一些）" % e)
        return _run_launcher
    return jvm_daemon.launcher_from_args(dump, on_note=notes.append, fallback=_run_launcher,
                                         fallback_name="Gradle")


def run_jvm_debug(source: Dict[str, Any],
                  key: str = "我",
                  timeout: int = 60,
                  cookie: str = "",
                  cache: str = CACHE_AUTO,
                  proxy: str = "",
                  out_path: str = "",
                  launcher: Optional[Callable[[], Tuple[int, float, str, str]]] = None,
                  ) -> Dict[str, Any]:
    """跑一次 JVM 调试，返回与设备通道同形状的结果。

    ``out_path`` 只是给测试用的覆盖口（默认落在 `data/app_probe/jvm_debug.ndjson`）。

    ``launcher`` 是**「拉起那一步」的替换口**（签名同 :func:`_run_launcher`）：
    不给就用 :func:`default_launcher`——**优先常驻 daemon、不可用回落 Gradle**（D2）。
    直起（`java @argfile`）与常驻都从这条缝接进来。
    **参数拼装 / NDJSON 与侧车解析 / 结果组装只有这一份**——三条拉起方式各写一份，
    就会出现「命令行跑出来的和界面上跑出来的不一样」（这就是本模块存在的理由）。
    """
    out: Dict[str, Any] = {
        "source": "jvm",
        "steps": [],
        "pages": [],
        "all_ok": True,
        "events": [],
        "error": "",
    }
    src = dict(source or {})
    if not str(src.get("bookSourceUrl", "") or "").strip():
        out["error"] = "缺少 bookSourceUrl（它同时是 cookie 注入的键，不能空）"
        return out
    env_err = _env_error()
    if env_err:
        out["error"] = env_err
        return out

    if not RUN_LOCK.acquire(blocking=False):
        out["error"] = BUSY_REASON
        return out
    saved = ARGS.read_text(encoding="utf-8") if ARGS.exists() else ""
    ndjson = out_path or data_path("app_probe", "jvm_debug.ndjson")
    # 源 JSON 落在 **out 同目录**：测试传临时 out 时它跟着走，不会写进真 `data/`
    src_file = str(pathlib.Path(ndjson).parent / "jvm_debug_src.json")
    meta_file = pathlib.Path(str(ndjson) + ".meta.json")
    code = -1
    cost = 0.0
    #: 拉起方式留下的痕迹（常驻不可用 → 回落了）。**必须露出来**：静默降级会让
    #: 「常驻一直没生效」永远没人发现，而用户只会觉得「也没快多少」。
    launch_notes: List[str] = []
    try:
        for p in (pathlib.Path(ndjson), meta_file):
            if p.exists():
                p.unlink()
        # **绝对路径**：这两个路径写给**另一个进程**（启动器会 pushd 到 App 仓库根），
        # 相对路径会落到那里去（后端的 `_export_sources_file` 也是同一条纪律）
        pathlib.Path(src_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(src_file).write_text(json.dumps([src], ensure_ascii=False),
                                          encoding="utf-8", newline="\n")
        _write_args(src_file, key, ndjson, timeout, cookie)
        _, cost, _so, _se = (launcher or default_launcher(launch_notes))()
    finally:
        # 还原：跑批与调试共用这一个参数文件，别把调试的参数留在里面
        if saved:
            ARGS.write_text(saved, encoding="utf-8", newline="")
        RUN_LOCK.release()

    events_raw = _read_ndjson(ndjson)
    meta: Dict[str, Any] = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    code = int(meta.get("code", -1))
    out["code"] = code
    out["code_text"] = CODE_TEXT.get(code, "未知（没产出侧车）")
    out["cost_sec"] = round(cost, 1)
    out["cookie_len"] = int(meta.get("cookie_len") or 0)
    out["hint"] = str(meta.get("hint") or "")
    # 事件：与设备通道**同形** `[{"t": 秒, "text": 原文}]`（抽屉按这个渲染）
    out["events"] = [{"t": round((e.get("elapsed_ms") or 0) / 1000.0, 3),
                      "text": e.get("text", "")} for e in events_raw]
    if not events_raw:
        out["error"] = "本机引擎一条事件都没收到。" + out["hint"]
        return out

    # 第三期 matched_html 回填（TODO §一点八）：本机引擎会把每段规则命中的 DOM 带回来，
    # 形状 {url: {step: html}}——**过一道显式闸门**（AGENTS #22），不合法就整块忽略并留日志
    steps = build_steps(events_raw, matched=matched_map(meta.get("matched_html")))
    # 拉起方式的痕迹进附注（回落了要说出来；正常走常驻时这里是空的）。
    # 放最后一段附注**之前**：先讲「这次是怎么跑起来的」，再讲「跑的过程中缺了什么」
    if launch_notes and steps:
        steps[0]["notes"] = list(steps[0]["notes"]) + launch_notes
        steps[0]["has_notes"] = True
    try:
        out["pages"] = fetch_debug_pages(steps, src, proxy=proxy, cache=cache)
    except Exception as e:
        # 补证据失败绝不能把已经拿到的判定丢掉（与设备通道同一立场）
        if steps:
            steps[0]["notes"] = list(steps[0]["notes"]) + ["页面抓取整体失败：%s" % e]
            steps[0]["has_notes"] = True
    out["steps"] = steps
    out["all_ok"] = all(s["ok"] for s in steps)
    if code == 2:
        out["error"] = "本机引擎一条事件都没收到。" + out["hint"]
    elif code == 4:
        out["error"] = "本机引擎入参/输入错误：%s" % (meta.get("error") or "见侧车")
    elif code in (3, 5) and steps:
        # 超时/截断：**保留部分结果**，把状态写进第一条 step 的附注（同「补抓失败」
        # 那一手：结论比诊断重要）。`all_ok` 不因此变 False——它只说「有没有 fail」
        if steps:
            steps[0]["notes"] = list(steps[0]["notes"]) + [
                "本机调试：%s" % out["code_text"]]
            steps[0]["has_notes"] = True
    return out

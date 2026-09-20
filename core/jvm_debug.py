# -*- coding: utf-8 -*-
"""JVM 调试通道（S5-A4）：把 A1 那条 CLI 收成可复用的模块。

**为什么单开一个模块**：这条链现在有两个消费方——CLI（`scripts/jvm_debug_run.py`，
A1–A3 的验收都跑它）与后端 `POST /api/rules/jvm-debug`（界面上选「本机引擎」）。
参数拼装与产物解析各写一份就会漂（lessons §二十三），而漂的表现是「界面上跑出来的
和命令行跑出来的不一样」——排查时根本想不到是两份实现。

**返回体与设备通道（`core/app_debug.run_app_debug`）同形状**
（`source/steps/pages/all_ok/events/error`）——前端抽屉与卡片零改动就能吃。这是 A1 定的
「NDJSON 与设备 WS 逐事件同构」的直接收益：`build_steps` 一行不改。

**`pages` 用 `fetch_debug_pages` 补抓**，与设备通道同一口径：本批还没有把 JVM 里每段的
真实 HTML 交回来（那是第三期 matched_html 回填），而抽屉「看源码改规则」需要 HTML。

**不代用户登录**（A3 的边界）：登录态来自用户自己的浏览器 profile
（`scripts/jvm_login.py` 预热一次）或 `cookie=` 手工给，这里只把它转发给 JVM 侧。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.app_debug import build_steps, fetch_debug_pages
from core.fetch import CACHE_AUTO
from core.paths import data_path

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGSVC = ROOT / "appservice"
ARGS = AGSVC / "args.properties"
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

#: **跑批与调试共用一把锁**：两条链都写 `appservice/args.properties`、都拉同一个
#: Gradle 任务。同时跑会互相踩（参数被改写、两个 Gradle 抢同一份构建产物），
#: 而那种失败看起来像「JVM 坏了」。非阻塞获取——拿不到就直说，**不排队**：
#: 排队会让界面上的一张卡片转十几分钟，那比一句「另一个任务在跑」糟得多。
RUN_LOCK = threading.Lock()


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
    ARGS.write_text("\n".join(lines + [""]), encoding="utf-8", newline="\n")


def _run_launcher(timeout_min: int = 20) -> Tuple[int, float, str, str]:
    """拉一次 Gradle（阻塞）。返回 `(退出码, 墙钟秒, stdout, stderr)`。

    单独一个函数是为了让测试能把它换成假的——**不跑 Gradle 也能测组装逻辑**。
    """
    t0 = time.time()
    p = subprocess.run(
        ["cmd", "/c", str(LAUNCHER), ":app:testAppDebugUnitTest",
         "--tests", LAUNCHER_CLASS, "--rerun"],
        cwd=str(AGSVC), capture_output=True, text=True, errors="replace",
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


def run_jvm_debug(source: Dict[str, Any],
                  key: str = "我",
                  timeout: int = 60,
                  cookie: str = "",
                  cache: str = CACHE_AUTO,
                  proxy: str = "",
                  out_path: str = "") -> Dict[str, Any]:
    """跑一次 JVM 调试，返回与设备通道同形状的结果。

    ``out_path`` 只是给测试用的覆盖口（默认落在 `data/app_probe/jvm_debug.ndjson`）。
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
        out["error"] = ("另一个 JVM 任务在跑（跑批与调试共用同一个 Gradle 任务与参数文件），"
                        "等它跑完再来")
        return out
    saved = ARGS.read_text(encoding="utf-8") if ARGS.exists() else ""
    ndjson = out_path or data_path("app_probe", "jvm_debug.ndjson")
    # 源 JSON 落在 **out 同目录**：测试传临时 out 时它跟着走，不会写进真 `data/`
    src_file = str(pathlib.Path(ndjson).parent / "jvm_debug_src.json")
    meta_file = pathlib.Path(str(ndjson) + ".meta.json")
    code = -1
    cost = 0.0
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
        _, cost, _so, _se = _run_launcher()
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

    steps = build_steps(events_raw)
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

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
import shutil
import subprocess
import threading
import urllib.parse
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from core.app_debug import (build_steps, engine_pages, fetch_debug_pages, matched_map,
                            network_entries)
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


def _write_args(src_file: str, key: str, out_file: str, timeout: int, cookie: str = "",
                proxy: str = "", args_path: Optional[pathlib.Path] = None) -> None:
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
    # 浏览器那一侧的代理（Chrome 只认启动参数；与源 header 里那个 OkHttp 代理配套）。
    # **写进这个文件而不是环境变量**：常驻 daemon 早就起来了、环境变量它读不到；
    # 而这个文件每次运行都重写，`AppserviceEnv.loadArgs()` 现读现用
    if proxy:
        lines.append("proxy=%s" % proxy)
    target = args_path or ARGS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines + [""]), encoding="utf-8", newline="\n")


def _run_launcher(timeout_min: int = 20, args_path: Optional[pathlib.Path] = None) -> Tuple[int, float, str, str]:
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
           "LEGADO_APPSERVICE_ARGS": str(args_path or ARGS)}
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


def default_launcher(notes: List[str], args_path: Optional[pathlib.Path] = None) -> Callable[[], Tuple[int, float, str, str]]:
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
    target_args = args_path or ARGS
    fallback = _run_launcher if target_args == ARGS else (lambda: _run_launcher(args_path=target_args))

    try:
        if jvm_direct.dump_is_stale():
            return fallback
        dump = jvm_direct.load_dump(warn_stale=False)
    except Exception as e:                       # dump 读不了（权限/损坏）也别挡住调试
        notes.append("这次没能用常驻进程（读 dump 失败：%s），已改用 Gradle（启动慢一些）" % e)
        return fallback
    return jvm_daemon.launcher_from_args(
        dump, on_note=notes.append, fallback=fallback,
        args_file=str(target_args), fallback_name="Gradle")


def verify_generated(source: Dict[str, Any], keyword: str, detail_url: str = "",
                     timeout: int = 60, out_path: str = "") -> Dict[str, Any]:
    """**生成之后的验证**：跑一次本机引擎（十-5）。生成链与 Web 那条路**共用这一份**。

    为什么收在这里而不是各写一份：两条路（CLI 的 `services.add_source.run_add`、Web 的
    `backend/api/ops.py`）验的是同一件事——「这份刚生成的源在 App 的真引擎上跑得通吗」。
    各写一份就会漂，而漂的表现是**两边的结论不一样**，看的人不知道该信哪个（lessons §二十三
    那类）。本地回放器那条老链（`core.verify.verify_chain`）跑不了 JS、没有登录态，对 L2–L4
    的页面看的是**另一份材料**——它的「通过」与 App 的实际行为无关（lessons §七十七）。

    两条边界：

    - **引擎不可用不推翻生成结果**：没配 App 源码目录 / 另一个 JVM 任务在跑 / 零事件时，
      源照样生成，只把「这次没验成 + 为什么」带回去（AGENTS #4：原因要走到用户眼前）。
      生成的验证是**附加证据**，不是生成的必要条件。
    - **证据原文要剥掉**（`strip_evidence`）：Web 那条路的结果会写进 jobs 表并走 SSE，
      整页 HTML + 逐段原文会撑爆它；判定结论（verdict / detail / all_ok）完整保留。
      CLI 只打印，剥掉也无损——**一份行为，两处都安全**。

    调试目标：有搜索规则用关键词（搜索 → 详情 → 目录 → 正文整条链）；没有搜索规则的源
    （「仅发现」模式生成的）拿详情页当入口。**App 固定取第一条候选**，没有 pick 这个口。
    """
    from core.paths import data_path
    from core.verify import strip_evidence

    from core import settings_store

    src = dict(source or {})
    proxy = settings_store.resolve_proxy()          # 全局唯一的代理出入口
    key = (keyword if str(src.get("searchUrl") or "").strip()
           else (detail_url or str(src.get("bookSourceUrl") or "")))
    out = run_jvm_debug(src, key=key, timeout=timeout, proxy=proxy,
                        out_path=out_path or data_path("app_probe",
                                                       "quick_add_verify.ndjson"))
    if out.get("error") and not out.get("steps"):
        return {"steps": [], "pages": [], "all_ok": None, "skipped": True,
                "engine": "jvm", "source": "jvm", "error": out["error"]}
    # 事件流对「生成预览」没用，且它是这里最占体积的一块（判定要看的是 steps）
    out.pop("events", None)
    return strip_evidence(out)


def _header_map(source: Dict[str, Any]):
    """把源的 ``header`` 变成 dict——**App 认的就是这个形态**。

    `BaseSource.getHeaderMap()` 是 ``GSONStrict.fromJsonObject<Map<String,String>>(header)``：
    **JSON**。所以行式串（``User-Agent: x``）在 App 里解不出来、整块被跳过——实测库里 32 条
    行式历史的 UA 就没生效过，而我们自己生成的源也是行式（一起改，见 `core/build.py`）。

    三种返回：dict（能合并）、``None``（`@js:` / `<js>`——**脚本形态，别动它**：合并会改变它
    的语义，只能放弃注入并让调用方知道）、``{}``（空）。
    """
    raw = str((source or {}).get("header") or "").strip()
    if not raw:
        return {}
    if raw.lower().startswith(("@js:", "<js>")):
        return None
    if raw.startswith("{"):
        try:
            got = json.loads(raw)
            return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
        except Exception:
            return {}
    # 行式：`k: v` 一行一条（历史形态）。转成 dict 等于让它按作者的原意生效
    out = {}
    for ln in raw.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def apply_proxy_header(source: Dict[str, Any], proxy: str) -> Dict[str, Any]:
    """把代理写进**源的 header**（JSON），返回副本（不改调用方那份）。

    **为什么是 header**：上游 `AnalyzeUrl` 的 init 从 `source.getHeaderMap()` 取 ``proxy`` 键，
    取到就当这次请求的 OkHttp 代理（并从 header 里去掉，见 `AnalyzeUrl.kt` 的 init）——
    这是 App 唯一认的注入点，所以不用改 Kotlin。

    空代理 = 直连：**一个字都不动**（不写一个空值的 proxy 键——看起来像生效中的设置）。
    header 是 `@js:` / `<js>` 脚本时同样不动：那是脚本，我们合并不了（这一次就没有代理可用，
    调用方要知道——见返回体本身的形状）。
    """
    src = dict(source or {})
    proxy = str(proxy or "").strip()
    if not proxy:
        return src
    head = _header_map(src)
    if head is None:                       # @js / <js>：脚本形态，不碰
        return src
    head["proxy"] = proxy
    src["header"] = json.dumps(head, ensure_ascii=False)
    return src


def page_from_engine(url: str, *, timeout: int = 60, render: bool = True,
                     with_requests: bool = False):
    """**让引擎去取这一页，把 App 手上那份 HTML 交回来**（十-5 的编排用）。

    临时源是**一次性的**：`发现::<url>` 让 App 走「发现」分支去取这个地址，源里不需要任何
    规则——我们的目的只是那一页的 body（侧车里的 `engine_html`）。**不落库、不推送、
    不写 App 的任何数据**（与调试那条链同一条纪律）。

    **`render=True` 才是这条口的价值**：光按 URL 抓回来的还是同一份原文（我们的 fetch 就能
    做到），而 L2/L3/L4 要的是**渲染后**的页面——所以给这个地址挂上 App 自己的
    `,{"webView":true}` 选项，让它走 `BackstageWebView`（本机就是 A2 那条浏览器桥：
    真浏览器渲染 + 求值 `js` 选项），交回来的是页面脚本跑过之后的那份 DOM。

    **拿不到就抛，带原因**：调用方据此「这一段规则先不给」，**不退回我们抓的那份**——
    材料不对产出的规则是假成功（lessons §八十）。
    """
    from core.paths import data_path
    parts = urllib.parse.urlsplit(url)
    origin = "%s://%s" % (parts.scheme, parts.netloc) if parts.netloc else url
    probe = {"bookSourceName": "取页探针", "bookSourceUrl": origin,
             "bookSourceType": 0, "exploreUrl": url}
    # 选项挂在**key 的 URL** 上：App 拿它当 mUrl 交给 AnalyzeUrl，那里才认 `,{...}`
    key = "发现::" + url + (',{"webView":true}' if render else "")
    from core import settings_store

    out = run_jvm_debug(probe, key=key, timeout=timeout, proxy=settings_store.resolve_proxy(),
                        out_path=data_path("app_probe", "engine_page.ndjson"))
    pages = [p for p in (out.get("pages") or []) if p.get("origin") == "engine"]
    hit = next((p for p in pages if str(p.get("url") or "") == url), None) or (pages[0] if pages else None)
    if not hit:
        raise RuntimeError(out.get("error") or
                           ("引擎这次没把这一页交回来（%s）"
                            % (out.get("code_text") or "未见输出")))
    html = str(hit.get("html") or "")
    if with_requests:
        # L4 的材料：请求本身 +「为什么没留下」的计数（L4 说明见 `core/net_hunt`）。
        return html, list(out.get("network") or []), {
            "events": int(out.get("network_events") or 0),
            "types": str(out.get("network_types") or ""),
            "drops": str(out.get("network_drops") or ""),
        }
    return html


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

    ``out_path`` 只是给测试用的覆盖口；未指定时会在 `data/app_probe/runs/` 下创建
    本次调试的独立目录，完成后清理。

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
    # 代理走**源的 header**（App 唯一认的注入点）：只影响交给 App 的那份，
    # 调用方手里那份不动；`proxy` 同时用于我们自己的补抓（fetch_debug_pages）
    src = apply_proxy_header(source or {}, proxy)
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
    # Web 请求不再共用固定的输出/参数文件；测试或 CLI 显式给 `out_path` 时仍保留
    # 原来的覆盖口。每个真实请求有自己的目录，daemon 收到的路径也随请求走。
    owns_run_dir = not bool(out_path)
    if owns_run_dir:
        run_dir = pathlib.Path(data_path("app_probe", "runs")) / ("debug-" + uuid4().hex)
        run_dir.mkdir(parents=True, exist_ok=True)
        ndjson_path = run_dir / "debug.ndjson"
        args_path = run_dir / "args.properties"
        saved = ""
    else:
        ndjson_path = pathlib.Path(str(out_path))
        args_path = ARGS
        saved = ARGS.read_text(encoding="utf-8") if ARGS.exists() else ""
    ndjson = str(ndjson_path)
    src_file = str((run_dir if owns_run_dir else ndjson_path.parent) / "source.json")
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
        _write_args(src_file, key, ndjson, timeout, cookie, proxy, args_path=args_path)
        if launcher:
            launch = launcher
        elif owns_run_dir:
            launch = default_launcher(launch_notes, args_path=args_path)
        else:
            launch = default_launcher(launch_notes)
        _, cost, _so, _se = launch()
    except Exception:
        if owns_run_dir:
            shutil.rmtree(str(run_dir), ignore_errors=True)
        raise
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
    # L4 诊断：network 是过滤后的可用材料，下面三项说明「浏览器发过什么」以及
    # 「为什么材料没有留下」。只进排障，不参与调试结论。
    out["network_events"] = int(meta.get("network_events") or 0)
    out["network_types"] = str(meta.get("network_types") or "")
    out["network_drops"] = str(meta.get("network_drops") or "")
    # 事件：与设备通道**同形** `[{"t": 秒, "text": 原文}]`（抽屉按这个渲染）
    out["events"] = [{"t": round((e.get("elapsed_ms") or 0) / 1000.0, 3),
                      "text": e.get("text", "")} for e in events_raw]
    if not events_raw:
        # 侧车错误比“零事件”更具体：旧启动器即使仍写 code=2，
        # 只要留下 error 就不能把进程内异常洗成通用的零事件诊断。
        sidecar_error = str(meta.get("error") or "").strip()
        if sidecar_error:
            out["error"] = "本机引擎异常：" + sidecar_error
        elif code == 4:
            out["error"] = "本机引擎入参/输入错误：见侧车"
        else:
            out["error"] = "本机引擎一条事件都没收到。" + out["hint"]
        out["network"] = network_entries(meta.get("network"))
        if owns_run_dir:
            shutil.rmtree(str(run_dir), ignore_errors=True)
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
        # 引擎取回来的整页（侧车里的 `engine_html`，见 DebugService.EngineHtmlCollector）：
        # 有它就用它——**那是 App 手上的那份**，比我们另抓一遍真，而且不用再发请求
        out["pages"] = fetch_debug_pages(steps, src, proxy=proxy, cache=cache,
                                         engine_html=engine_pages(meta.get("engine_html")))
    except Exception as e:
        # 补证据失败绝不能把已经拿到的判定丢掉（与设备通道同一立场）
        if steps:
            steps[0]["notes"] = list(steps[0]["notes"]) + ["页面抓取整体失败：%s" % e]
            steps[0]["has_notes"] = True
    # 抓包（侧车 `network` 键）：L4 的材料。形状闸门在 `core/app_debug.network_entries`，
    # 它与 `pages[]` 一样是**证据**——坏了整块丢掉，但绝不带走已经拿到的判定
    out["network"] = network_entries(meta.get("network"))
    out["steps"] = steps
    out["all_ok"] = all(s["ok"] for s in steps)
    sidecar_error = str(meta.get("error") or "").strip()
    if sidecar_error:
        out["error"] = "本机引擎异常：" + sidecar_error
    elif code == 2:
        out["error"] = "本机引擎一条事件都没收到。" + out["hint"]
    elif code == 4:
        out["error"] = "本机引擎入参/输入错误：见侧车"
    elif code in (3, 5) and steps:
        # 超时/截断：**保留部分结果**，把状态写进第一条 step 的附注（同「补抓失败」
        # 那一手：结论比诊断重要）。`all_ok` 不因此变 False——它只说「有没有 fail」
        if steps:
            steps[0]["notes"] = list(steps[0]["notes"]) + [
                "本机调试：%s" % out["code_text"]]
            steps[0]["has_notes"] = True
    if owns_run_dir:
        shutil.rmtree(str(run_dir), ignore_errors=True)
    return out

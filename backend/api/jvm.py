# -*- coding: utf-8 -*-
"""JVM 校验服务的后端接口（S2）。

三个端点：
  GET  /api/jvm/selftest   环境自检（只读：推导 JDK/SDK/gradle-home，不装东西）
  POST /api/jvm/run        跑批（subprocess 调启动器；同步等完成——S2 先做一次性
                           调用形态，jobs 化留给确实嫌慢之后）
  GET  /api/jvm/results    最近一批结论（从 meta 读，供列表合并展示）

设计要点：
- 跑批走 **subprocess + 属性文件**，不 import JVM 侧任何东西——两条进程的生命
  周期完全独立，Robolectric 的堆/退出码都不影响后端进程。
- 结论读回（meta 表 jvm_check:<batch>:<url>）由 jvm_readback 逻辑内联在 run 里，
  跑完立即落库，失败也要留下已完成的部分。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from core.paths import data_dir
from core.store import Store
from core import settings_store
from core.jvm_env import selftest
from core.loader import _normalize_url

router = APIRouter()

_ROOT = Path(__file__).resolve().parents[2]
_AGSVC = _ROOT / "appservice"

#: 结论 state → 展示口径（来源阶梯：本地回放 < JVM < 真机）
STATE_LABEL = {
    "ok": "可用（JVM·App 引擎）",
    "no_result": "搜索无结果（JVM）",
    "empty_js_shell": "无法验证·需浏览器（JVM）",
    #: A3：搜索为空 + 搜索页出现登录提示 → 需登录。**不是源坏了**，是这次没带登录态
    #: （结论里的 `cookie_len` 会说明带没带；`reason` 里有命中的那个词）
    "login_wall": "需登录（JVM）",
    "timeout": "超时（JVM）",
    "error": "执行失败（JVM）",
    "invalid": "源 JSON 无效（JVM）",
}


def _launcher() -> Path:
    p = _AGSVC / "legado-gradle.bat"
    if not p.exists():
        raise HTTPException(500, "找不到启动器 appservice/legado-gradle.bat")
    return p


def _write_args(keyword: str, timeout: int, concurrency: int, limit: int,
                out_path: Path, source_file: Path, depth: str = "search") -> None:
    """把跑批参数写进 appservice/args.properties（Launcher 的唯一参数入口）。"""
    from core.jvm_env import selftest  # 局部导入避免循环
    repo = settings_store.load().get("jvm", {}).get("app_repo", "").strip()
    st_conf = settings_store.load().get("jvm", {})
    if not repo:
        raise HTTPException(400, "JVM 校验未配置：设置里缺少 App 源码目录")
    lines = [
        "# 由后端 /api/jvm/run 生成（手工跑批时也可自己改）",
        "file=%s" % source_file.as_posix(),
        "keyword=%s" % (keyword or st_conf.get("keyword", "我")),
        "out=%s" % out_path.as_posix(),
        "timeout=%d" % (timeout or st_conf.get("timeout", 25)),
        "concurrency=%d" % (concurrency or st_conf.get("concurrency", 8)),
        # 深度也进参数文件（Launcher 读它转发给 --depth）。**不硬编码 search**：
        # 设置里选了什么就跑什么，跑批结论里的 stage 才与实际一致
        "depth=%s" % (depth or st_conf.get("depth", "search")),
    ]
    if limit:
        lines.append("limit=%d" % limit)
    # newline="\n" 是必须的：这是 **git 跟踪的文件**，而 write_text 在 Windows 上
    # 把 \n 翻成 \r\n——跑一次批工作区就脏一次（内容与 HEAD 逐字节相同，只差行尾，
    # git diff 连内容都不显示，只在 git add 时冒一句 warning）。同 agent-write-safety §三。
    (_AGSVC / "args.properties").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _export_sources_file(st) -> Path:
    """把管理库在用源导出成 JVM 侧可吃的 JSON 文件（与 S1 手工导出同一形状）。"""
    import io
    out = []
    for view in st.export_sources():
        if not view.get("enabled", 1):
            continue
        raw = view.get("raw_json")
        if isinstance(raw, str):
            try:
                d = json.loads(raw)
            except Exception:
                continue
        else:
            d = raw
        out.append({k: d.get(k) for k in (
            "bookSourceName", "bookSourceUrl", "searchUrl", "exploreUrl",
            "ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent",
            "header", "bookSourceType", "enabled", "bookSourceGroup") if k in d})
    # **必须绝对路径**：这两个路径是写给**另一个进程**用的——启动器会 `pushd` 到
    # App 仓库根再跑 Gradle，测试 JVM 的 CWD 就是那里。相对路径于是解析到
    # `<App 仓库>/data/...`：轻则后端 `out_path.exists()` 找不到（报「启动器没有
    # 产出结果文件」），重则**往 App 仓库里写目录**——那是零入侵红线（AGENTS 的
    # 「App 仓库 git status 必须为空」）。`core.paths.data_dir()` 是仓库自己的解析口。
    probe_dir = data_dir() / "app_probe"
    path = probe_dir / "jvm_batch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def _run_gradle(timeout_min: int = 90) -> int:
    """调启动器跑批（阻塞直到 Gradle 退出）。返回退出码。"""
    exe = _launcher()
    proc = subprocess.run(
        ["cmd", "/c", str(exe), ":app:testAppDebugUnitTest",
         "--tests", "io.legado.app.service.ValidateServiceLauncher", "--rerun"],
        cwd=str(_AGSVC), capture_output=True, text=True,
        timeout=timeout_min * 60, errors="replace",
    )
    return proc.returncode


def _read_results(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def _write_meta(rows: List[Dict[str, Any]]) -> str:
    """结论写 meta（jvm_check:<batch>:<url>），幂等。返回 batch id。"""
    from core.store import Store
    batch = time.strftime("%Y%m%d_%H%M%S")
    st = Store()
    try:
        for r in rows:
            url = _normalize_url(str(r.get("url", "") or ""))
            if not url:
                continue
            st.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("jvm_check:%s:%s" % (batch, url), json.dumps(r, ensure_ascii=False)))
        st.conn.commit()
    finally:
        st.close()
    return batch


@router.get("/selftest")
def jvm_selftest():
    conf = settings_store.load().get("jvm", {})
    return selftest(conf.get("app_repo", ""))


@router.post("/run")
async def jvm_run():
    conf = settings_store.load().get("jvm", {})
    if not conf.get("app_repo"):
        raise HTTPException(400, "JVM 校验未配置：请先在设置里填 App 源码目录并自检")

    # 自检不过就不开跑——跑一半 OOM/路径错只会浪费时间
    st_conf = selftest(conf.get("app_repo", ""))
    if not st_conf["ok"]:
        return {"started": False, "selftest": st_conf}

    st = Store()
    src_file = _export_sources_file(st)
    st.close()
    limit = int(conf.get("limit", 0) or 0)
    if limit > 0:
        data = json.loads(src_file.read_text(encoding="utf-8"))[:limit]
        src_file.write_text(json.dumps(data, ensure_ascii=False),
                            encoding="utf-8", newline="\n")
    # 绝对路径的理由同 _export_sources_file：这个路径是给**另一个进程**（CWD = App
    # 仓库根）用的，相对路径会落到 App 仓库里去
    out_path = data_dir() / "app_probe" / "jvm_results.jsonl"
    if out_path.exists():
        out_path.unlink()
    _write_args(conf.get("keyword", "我"), int(conf.get("timeout", 25)),
                int(conf.get("concurrency", 8)), limit, out_path, src_file,
                str(conf.get("depth", "search")))

    def _blocking() -> Dict[str, Any]:
        code = _run_gradle()
        if not out_path.exists():
            return {"started": True, "ok": False, "exit": code,
                    "reason": "启动器没有产出结果文件（看 Gradle 输出定位）"}
        rows = _read_results(out_path)
        batch = _write_meta(rows)
        dist = Counter(r.get("state") for r in rows)
        return {"started": True, "ok": code == 0, "exit": code, "batch": batch,
                "count": len(rows), "dist": dict(dist)}

    # FastAPI 的线程池里跑，不卡事件循环（全量 17 分钟，HTTP 超时是客户端的事）
    result = await run_in_threadpool(_blocking)
    return result


@router.get("/results")
def jvm_results():
    """每源**最深**的一条 JVM 结论，供列表/面板展示。

    取舍规则收在 ``Store.latest_jvm_conclusions``（深者胜、同深取新）——
    这里**不再自己实现一遍**：列表回填读的是同一张表，两处各写一次就会漂
    （lessons §二十三 的「同一件事两个实现」已经栽过两次）。
    """
    st = Store()
    try:
        latest = st.latest_jvm_conclusions()
    finally:
        st.close()
    dist = Counter(d.get("state") for d in latest.values())
    stage_dist = Counter(d.get("stage") for d in latest.values())
    batches = sorted({str(d.get("_batch") or "") for d in latest.values()}, reverse=True)
    return {
        "count": len(latest),
        "batches": [b for b in batches if b][:5],
        "dist": dict(dist),
        "stage_dist": dict(stage_dist),
        "label": STATE_LABEL,
        "items": [{"url": u, **d} for u, d in latest.items()],
    }

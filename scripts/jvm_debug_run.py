# -*- coding: utf-8 -*-
"""本机引擎调试的 CLI（S5-A1 起就在跑它；A4 起只是 `core/jvm_debug` 的一层壳）。

    python scripts/jvm_debug_run.py -i <源 JSON> -k <调试目标> [--cookie ...] [--timeout 60]

**逻辑全在 `core/jvm_debug.py`**：界面（`POST /api/rules/jvm-debug`）走的是同一份。
两处各写一遍参数拼装与结果解析就会漂，而漂的表现是「界面上跑出来的和命令行跑出来的
不一样」——排查时根本想不到是两份实现（lessons §二十三、§六十四）。

产物两个，都落在 `-o`（默认 `data/app_probe/debug_run.ndjson`）旁边：

    <out>              NDJSON 事件流，**与设备 WS 逐事件同构**
    <out>.meta.json    侧车诊断：退出码含义 / 事件数 / payload 数 / 是否收到终止 / 超时 /
                       **cookie_len**（这次带没带登录态）/ 错误

退出码沿用 DebugService 的分档，**直接透传**（0 正常 / 2 零事件 / 3 超时 /
4 入参错误 / 5 事件流被截断）——非 0 时本脚本也非 0，好当验收闸门用。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.jvm_debug import run_jvm_debug  # noqa: E402


def load_source(path: str, index: int):
    text = pathlib.Path(path).read_text(encoding="utf-8").strip()
    data = json.loads(text)
    if isinstance(data, list):
        if not data:
            raise SystemExit("源文件里没有源: %s" % path)
        if index >= len(data):
            raise SystemExit("--index %d 越界（文件里 %d 条）" % (index, len(data)))
        return data[index]
    return data


def require_source(src, path, index):
    """**先确认这真是个书源**，别把「喂错文件」拖到 JVM 里去报错。

    实测踩过：喂了带包装的中间产物（`{"url":..., "raw":{...}}`），DebugService 因为
    解析不出 BookSource 直接退 4，连侧车都不产出——调用方只看到一个没有上下文的
    「退出码 -1」，得回到 Kotlin 那边才知道发生了什么。
    """
    if not isinstance(src, dict) or not (src.get("bookSourceUrl") or src.get("bookSourceName")):
        raise SystemExit(
            "这不是书源 JSON（缺 bookSourceUrl/bookSourceName）：%s[%d]\n"
            "  —— 喂原始书源数组，不要喂带包装的中间产物" % (path, index))
    return src


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True, help="源 JSON（数组或单个对象）")
    ap.add_argument("-k", "--key", required=True, help="调试目标：关键字 / 发现::URL / ++URL / --URL / 绝对URL")
    ap.add_argument("--index", type=int, default=0, help="数组里取第几条（默认 0）")
    ap.add_argument("--timeout", type=int, default=60, help="整次调试的墙钟上限秒（默认 60）")
    ap.add_argument("-o", "--out", default="data/app_probe/debug_run.ndjson")
    ap.add_argument("--cookie", default="",
                    help="手工注入的一条 cookie（A3）。不给就按源 URL 从浏览器 profile 读；"
                         "登录墙的源要先在那个 profile 里登录一次（scripts/jvm_login.py）")
    args = ap.parse_args()

    src = require_source(load_source(args.input, args.index), args.input, args.index)
    # **绝对路径**：`out` 是写给另一个进程的（启动器 pushd 到 App 仓库根再跑 Gradle），
    # 相对路径会解析到那里去——轻则找不到产物，重则往 App 仓库里写（零入侵红线）
    out = pathlib.Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    print("调试: %s  key=%s  timeout=%ds  cookie=%s"
          % (src.get("bookSourceName") or src.get("bookSourceUrl"), args.key,
             args.timeout, ("%d 字符" % len(args.cookie)) if args.cookie else "无（按 profile 读）"))
    r = run_jvm_debug(src, key=args.key, timeout=args.timeout, cookie=args.cookie,
                      out_path=str(out))
    if r.get("error") and not r.get("steps"):
        # 环境不可用 / 另一个任务在跑 / 零事件：**说清楚**，别让人对着空结果猜
        print("错误: %s" % r["error"])
        return 2 if "零事件" in r["error"] else 4

    code = int(r.get("code", -1))
    print("退出码=%d(%s)  墙钟=%.1fs  事件=%d  cookie_len=%s"
          % (code, r.get("code_text"), r.get("cost_sec") or 0.0,
             len(r.get("events") or []), r.get("cookie_len")))
    for s in r.get("steps") or []:
        print("  %-9s %-8s %s" % (s["name"], s.get("verdict"),
                                  "；".join(s.get("notes") or [])[:90]))
    if r.get("hint"):
        print("提示: %s" % r["hint"])
    if r.get("error"):
        print("错误: %s" % r["error"])
    # 非 0 就非 0：好让它能当验收闸门（与 Gradle 的退出码无关——那条链只报「测试过没过」）
    return code if code != 0 else 0


if __name__ == "__main__":
    sys.exit(main())

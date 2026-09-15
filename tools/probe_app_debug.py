# -*- coding: utf-8 -*-
"""手工探测：连阅读 App 的书源调试 WebSocket，把事件与聚合结果打出来。

协议实现已经搬到 ``core/app_debug.py``（Web 端走的就是同一份代码），
本文件只剩一个**给人看的 CLI**——排查「App 到底推了什么」时用它，
它会把每条事件原文打出来，这是核心库不做的。

用法：
    .venv/Scripts/python.exe tools/probe_app_debug.py <IP> [书源URL] [调试目标]

例：
    .venv/Scripts/python.exe tools/probe_app_debug.py 192.168.28.182
    .venv/Scripts/python.exe tools/probe_app_debug.py 192.168.28.182 "https://www.52shuku.net/"
    .venv/Scripts/python.exe tools/probe_app_debug.py 192.168.28.182 "https://www.52shuku.net/" "++https://.../book/1/"

前提：App 里打开「Web 服务」。**WS 端口 = HTTP 端口 + 1**（默认 1122/1123）。
连不上时看 App 通知栏显示的地址；端口可用环境变量 LEGADO_WS_PORT 覆盖。

书源 URL **必须是导入原文**（导入 JSON 里的 bookSourceUrl 原样），
用规范化过的 URL（尾部斜杠被去掉等）时 App 查不到源，会**静默无响应**。

调试目标三种格式：
    关键字      从搜索开始跑完整链路
    ++<URL>    从目录页开始（跳过搜索+详情）
    --<URL>    从正文页开始（跳过搜索+详情+目录）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.app_debug import (  # noqa: E402  （必须在 sys.path 调整之后导入）
    DEFAULT_DEBUG_PORT, WS_PATH, build_steps, collect_debug_events,
    fetch_debug_pages,
)

#: 分类用的前缀标记（App 用这些符号标记行类型），只为打印时可读
MARKS = (("︽", "步骤完成"), ("︾", "步骤开始"), ("└", "值/行"),
         ("┌", "字段名"), ("≡", "信息"), ("◇", "统计"), ("⇒", "入口"))


def classify(text: str) -> str:
    for mark, name in MARKS:
        if text.startswith(mark):
            return name
    return "其它"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    source_url = sys.argv[2] if len(sys.argv) > 2 else ""
    key = sys.argv[3] if len(sys.argv) > 3 else "我"
    port = int(os.getenv("LEGADO_WS_PORT", str(DEFAULT_DEBUG_PORT)))

    print("连 ws://%s:%d%s" % (host, port, WS_PATH))
    print("  tag = %r    key = %r" % (source_url, key))
    print("-" * 72)

    try:
        events = collect_debug_events(host, source_url, key, port=port)
    except Exception as e:
        print("【连接失败】%s: %s" % (type(e).__name__, e))
        print("检查：App 的「Web 服务」是否打开、手机与电脑是否同一局域网、"
              "端口是否为 HTTP 端口 + 1。")
        return 1

    for i, ev in enumerate(events, 1):
        text = ev["text"]
        print("[%6.2fs] #%-3d %6d字符 [%s] %s"
              % (ev["t"], i, len(text), classify(text),
                 text[:300].replace("\n", "\\n")))
        if len(text) > 300:
            print("            …（本条共 %d 字符，已截断展示）" % len(text))

    print("-" * 72)
    print("共 %d 条事件" % len(events))
    if not events:
        print("\n【零事件】两种可能：")
        print("  a) tag 查不到源 —— App 的 `getBookSource(tag)?.let{}` 查不到就"
              "**静默不响应**。先确认用的是**导入原文**。")
        print("  b) key 格式不对。")
        return 0

    steps = build_steps(events)
    print("\n聚合出的步骤：")
    for s in steps:
        print("  %-8s %-8s %s" % (s["name"], s["verdict"], s["detail"]))
        print("           url=%s" % (s["url"] or "(无)"))
        for n in s["notes"]:
            print("           note: %s" % n)

    print("\n按段抓页面（书源 header/charset 未提供，用默认头抓）：")
    for p in fetch_debug_pages(steps):
        print("  %-8s %7d 字符 %s" % (p["id"], p["len"], p["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

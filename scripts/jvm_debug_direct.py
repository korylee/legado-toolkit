# -*- coding: utf-8 -*-
"""JVM 调试的**直起 / 常驻**试验台：D0 量入场费，D1 验收常驻。

    python scripts/jvm_debug_direct.py --refresh                     # 生成/刷新测试 JVM 环境 dump
    python scripts/jvm_debug_direct.py -i <源 JSON> -k <key>          # 不起 Gradle 直起一次
    python scripts/jvm_debug_direct.py --daemon -i <源> -k <key>      # 走常驻 daemon 一次
    python scripts/jvm_debug_direct.py --measure -i <源> -k <key>     # D0：① bat / ② 直起 / ②b 空转
    python scripts/jvm_debug_direct.py --daemon-measure -i <源> -k <key>  # D1：对拍 + 第二次 ≤2s
    python scripts/jvm_debug_direct.py --daemon-status | --daemon-stop

## D0 的三个数（判 daemon 值不值，已量：入场费 10.7s → 做）

    ①  完整 `legado-gradle.bat`（`run_jvm_debug` 的 cost_sec）
    ②  直起：同一份 classpath / 系统属性，只是不经过 Gradle
    ②b 空转：一条**连不上**的源走直起，量「JVM + Robolectric + App 初始化」
        ——②b 才是 daemon 的真收益（①−② 是 Gradle 配置阶段，②−②b 是站点耗时）

## D1 的验收

同一源连跑两次，比三件事：**第二次墙钟 ≤ 2s**；与「各起一次 JVM」的结果**逐字段一致**；
两次之间没有串味（事件数/分段/退出码/cookie_len 全等）。

**参数拼装与结果解析一行都不自己写**：走 `core.jvm_debug.run_jvm_debug(launcher=...)`
（D0 留的口）——三条拉起方式（Gradle / 直起 / 常驻）共用同一份解析，否则
「命令行跑出来的和界面上跑出来的不一样」（同 lessons §二十三）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import jvm_daemon  # noqa: E402
from core.jvm_debug import run_jvm_debug  # noqa: E402
from core.jvm_direct import (dump_path, direct_launcher, load_dump,  # noqa: E402
                             refresh, run_direct)
from core.paths import data_path  # noqa: E402

#: `--boot-only` / ②b 用的源：地址是 `127.0.0.1:9`（discard 端口，必然连不上），
#: 于是它把「初始化」跑完就失败——量到的就是入场费本身。
BOOT_SOURCE = {
    "bookSourceName": "D0 空转（不可达地址，只为量初始化）",
    "bookSourceUrl": "http://127.0.0.1:9/",
    "bookSourceType": 0,
    "enabled": True,
    "enabledCookieJar": False,
    "searchUrl": "http://127.0.0.1:9/search?q={{key}}",
    "ruleSearch": {"bookList": ".item", "name": ".name@text",
                   "author": ".author@text", "bookUrl": "a@href"},
}


# ---------------------------------------------------------------- 对拍

def load_ndjson(path: str):
    f = pathlib.Path(path)
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def fingerprint(result: dict, ndjson: str = "") -> dict:
    """一次调试的「形状指纹」——两条拉起路径要对的就是它。

    事件原文里带**时间前缀**（`[00:00.020]`）与站点内容，逐字比必然不相等；比的是
    **事件序列的结构**（kind 序列）与产品真正看的那几项（退出码 / cookie_len / 每段 ok）。
    """
    fp = {
        "code": result.get("code"),
        "cookie_len": result.get("cookie_len"),
        "all_ok": result.get("all_ok"),
        "steps": [(s.get("name"), bool(s.get("ok"))) for s in (result.get("steps") or [])],
        "events": len(result.get("events") or []),
        "error": result.get("error") or "",
    }
    if ndjson:
        evs = load_ndjson(ndjson)
        fp["kinds"] = [e.get("kind") for e in evs]
    return fp


def diff_fp(a: dict, b: dict) -> list:
    return [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def show_fp(name: str, fp: dict) -> None:
    print("  %-10s code=%-3s cookie_len=%-5s all_ok=%-5s steps=%s 事件=%s%s"
          % (name, fp.get("code"), fp.get("cookie_len"), fp.get("all_ok"),
             fp.get("steps"), fp.get("events"),
             ("  错误=" + fp["error"][:60]) if fp.get("error") else ""))


def compare(tag: str, a: dict, b: dict) -> bool:
    bad = diff_fp(a, b)
    if bad:
        print("  差异字段：%s" % bad)
        print("  ⚠️  %s 形状不一致 → **不能拿这个数下判断**，先查为什么" % tag)
    else:
        print("  ✅ %s 形状逐字段一致（退出码 / cookie_len / 每段 ok / 事件数 / kind 序列）" % tag)
    return not bad


def load_source(path: str, index: int = 0) -> dict:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8").strip())
    if isinstance(data, list):
        if not data:
            raise SystemExit("源文件里没有源: %s" % path)
        return data[index]
    return data


def _run(src: dict, key: str, timeout: int, cookie: str, out: str, launcher=None,
         note=None) -> tuple:
    r = run_jvm_debug(src, key=key, timeout=timeout, cookie=cookie, out_path=out,
                      launcher=launcher)
    if note:
        note(r)
    return r, fingerprint(r, out)


# ---------------------------------------------------------------- D0

def measure(src: dict, key: str, timeout: int, cookie: str) -> int:
    """D0 的三个数：① bat 全链 / ② 直起 / ②b 空转。"""
    dump = load_dump()
    print("源：%s   key=%s   timeout=%ds" % (src.get("bookSourceName") or src.get("bookSourceUrl"),
                                            key, timeout))
    rows = []
    for i in range(2):
        out = str(pathlib.Path(data_path("app_probe", "d0_bat%d.ndjson" % i)))
        r, fp = _run(src, key, timeout, cookie, out)
        rows.append(("① bat 全链 #%d" % (i + 1), r.get("cost_sec") or 0.0, fp))
        show_fp("bat#%d" % (i + 1), fp)
    bat_fp = rows[-1][2]
    out = str(pathlib.Path(data_path("app_probe", "d0_direct.ndjson")))
    r, direct_fp = _run(src, key, timeout, cookie, out, launcher=direct_launcher(dump, timeout + 60))
    rows.append(("② 直起", r.get("cost_sec") or 0.0, direct_fp))
    show_fp("直起", direct_fp)
    out = str(pathlib.Path(data_path("app_probe", "d0_boot.ndjson")))
    r, _fp = _run(dict(BOOT_SOURCE), key, timeout, "", out,
                  launcher=direct_launcher(dump, timeout + 60))
    boot = r.get("cost_sec") or 0.0
    rows.append(("②b 空转", boot, _fp))

    print("\n=== D0 结果 ===")
    for name, cost, _ in rows:
        print("  %-14s %6.1fs" % (name, cost))
    print("\n=== 对拍（② 直起 vs ① bat）===")
    show_fp("bat", bat_fp)
    show_fp("直起", direct_fp)
    compare("直起 vs bat", bat_fp, direct_fp)
    print("\n=== 判据（TODO §一点八：②b ≥ 5s 就做 daemon）===")
    print("  ①−② = Gradle 配置阶段 %.1fs；②−②b = 站点耗时 %.1fs"
          % (rows[1][1] - rows[2][1], rows[2][1] - boot))
    print("  入场费 ②b = %.1fs → **%s**" % (boot, "做" if boot >= 5 else "不做（改量 matched_html）"))
    return 0


# ---------------------------------------------------------------- D1

def daemon_measure(src: dict, key: str, timeout: int, cookie: str) -> int:
    """D1 验收：常驻连跑两次 vs 各起一次 JVM。

    ⚠️ **基线必须在 daemon 停着时跑**：一个浏览器 profile 只能被一个 Chromium 占着，
    而两边都会在跑之前 `BrowserSession.get()`。daemon 常驻时再起一个 JVM，后者撞上占用
    → 自愈换临时 profile（**cookie 全丢**）→ 结论从「3 段」变成「只有搜索段」，
    还白等一次启动超时（实测 29s vs 11s）。那不是常驻的收益，那是测量串了。
    """
    dump = load_dump()
    name = src.get("bookSourceName") or src.get("bookSourceUrl")
    print("源：%s   key=%s   timeout=%ds" % (name, key, timeout))

    # 基线：两条**各自起一次 JVM**（先把 daemon 收掉，见上面那条提醒）
    jvm_daemon.stop()
    base = []
    for i in range(2):
        out = str(pathlib.Path(data_path("app_probe", "d1_fresh%d.ndjson" % i)))
        r, fp = _run(src, key, timeout, cookie, out, launcher=direct_launcher(dump, timeout + 60))
        base.append((r.get("cost_sec") or 0.0, fp))
        show_fp("新 JVM#%d" % (i + 1), fp)

    # 常驻：第一次含拉起（与直起同量级），第二次应当只剩站点时间
    notes: list = []
    launcher = jvm_daemon.launcher_from_args(dump, on_note=notes.append)
    daemon_costs = []
    daemon_fps = []
    for i in range(2):
        out = str(pathlib.Path(data_path("app_probe", "d1_daemon%d.ndjson" % i)))
        r, fp = _run(src, key, timeout, cookie, out, launcher=launcher)
        daemon_costs.append(r.get("cost_sec") or 0.0)
        daemon_fps.append(fp)
        show_fp("常驻#%d" % (i + 1), fp)

    print("\n=== D1 结果 ===")
    print("  各起一次 JVM：#1 %.1fs / #2 %.1fs（每次都付入场费）"
          % (base[0][0], base[1][0]))
    print("  常驻：        #1 %.1fs / #2 %.1fs  → 省下 %.1fs"
          % (daemon_costs[0], daemon_costs[1], base[1][0] - daemon_costs[1]))

    print("\n=== 对拍（常驻 #2 vs 新 JVM #2）===")
    show_fp("新 JVM", base[1][1])
    show_fp("常驻", daemon_fps[1])
    same = compare("常驻 vs 新 JVM", base[1][1], daemon_fps[1])
    print("\n=== 对拍（常驻 #2 vs 常驻 #1：**请求之间串没串味**）===")
    show_fp("常驻#1", daemon_fps[0])
    show_fp("常驻#2", daemon_fps[1])
    same = compare("常驻两次", daemon_fps[0], daemon_fps[1]) and same

    print("\n=== 判据（TODO §一点八 D1）===")
    ok_fast = daemon_costs[1] <= 2.0
    print("  第二次墙钟 %.1fs ≤ 2s：**%s**" % (daemon_costs[1], "达标" if ok_fast else "未达标"))
    print("  形状与「各起一次」一致：**%s**" % ("是" if same else "**否**"))
    if notes:
        print("  回落发生 %d 次：%s" % (len(notes), notes[:3]))
    print("  daemon 状态：%s" % json.dumps(jvm_daemon.status(), ensure_ascii=False)[:220])
    return 0 if (ok_fast and same and not notes) else 1


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="JVM 调试的直起 / 常驻试验台")
    ap.add_argument("--refresh", action="store_true", help="拉一次 Gradle 生成 dump（改了 Kotlin 后必做）")
    ap.add_argument("--probe", metavar="FQCN", help="直起某个测试类（不写参数文件）")
    ap.add_argument("--measure", action="store_true", help="D0：① bat / ② 直起 / ②b 空转")
    ap.add_argument("--daemon-measure", action="store_true", help="D1：常驻对拍 + 第二次 ≤2s")
    ap.add_argument("--daemon", action="store_true", help="走常驻 daemon 跑一次")
    ap.add_argument("--daemon-status", action="store_true", help="常驻还活着吗 / 版本对不对")
    ap.add_argument("--daemon-stop", action="store_true", help="停掉常驻")
    ap.add_argument("-i", "--input", help="源 JSON（数组或单个对象）")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("-k", "--key", default="我")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--cookie", default="")
    ap.add_argument("-o", "--out", default="data/app_probe/d0_direct.ndjson")
    ap.add_argument("--no-argfile", action="store_true", help="不走 @argfile（对照用）")
    a = ap.parse_args()

    if a.refresh:
        return refresh()
    if a.daemon_status:
        print(json.dumps(jvm_daemon.status(), ensure_ascii=False, indent=1))
        print("dump：%s" % dump_path())
        return 0
    if a.daemon_stop:
        print("已停：%s" % jvm_daemon.stop())
        return 0

    dump = load_dump()
    if a.probe:
        code, cost, so, se = run_direct(dump, a.probe, timeout=a.timeout,
                                        use_argfile=not a.no_argfile)
        print("直起 %s：退出码=%d 墙钟=%.1fs" % (a.probe, code, cost))
        for ln in ((so or "") + (se or "")).strip().splitlines()[-6:]:
            print("  | " + ln)
        return 0 if code == 0 else 1
    if a.measure:
        if not a.input:
            raise SystemExit("--measure 还要给 -i <源 JSON>")
        return measure(load_source(a.input, a.index), a.key, a.timeout, a.cookie)
    if a.daemon_measure:
        if not a.input:
            raise SystemExit("--daemon-measure 还要给 -i <源 JSON>")
        return daemon_measure(load_source(a.input, a.index), a.key, a.timeout, a.cookie)
    if not a.input:
        raise SystemExit("要么 --measure / --daemon-measure -i <源>，要么 --probe，要么 --refresh")

    src = load_source(a.input, a.index)
    out = str(pathlib.Path(a.out).resolve())
    notes: list = []
    launcher = None
    if a.daemon:
        launcher = jvm_daemon.launcher_from_args(dump, on_note=notes.append)
    else:
        launcher = direct_launcher(dump, timeout=a.timeout + 60)
    r, _fp = _run(src, a.key, a.timeout, a.cookie, out, launcher=launcher)
    print("退出码=%s(%s)  墙钟=%.1fs  事件=%d  cookie_len=%s"
          % (r.get("code"), r.get("code_text"), r.get("cost_sec") or 0.0,
             len(r.get("events") or []), r.get("cookie_len")))
    for s in r.get("steps") or []:
        print("  %-9s %-8s %s" % (s["name"], s.get("verdict"),
                                  "；".join(s.get("notes") or [])[:90]))
    for n in notes:
        print("注意: %s" % n)
    if r.get("error"):
        print("错误: %s" % r["error"])
    return 0 if int(r.get("code") or 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

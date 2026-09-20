# -*- coding: utf-8 -*-
"""预热登录（A3）：**在我们的固定 profile 里登一次**，之后 JVM 跑批/调试自动带上登录态。

    python scripts/jvm_login.py --url https://example.com/login
    python scripts/jvm_login.py -i data/app_probe/some_source.json      # 用源的 loginUrl

做三件事，**不碰你的账号密码、也不代你点任何东西**：

1. 用**我们的固定 profile** 起一个有界面的 Edge/Chrome，打开给出的地址；
2. 你在那个窗口里自己登录（脚本只等你按回车）；
3. 回车后脚本从浏览器里读出该 URL 的 cookie 并报告条数——**cookie 留在 profile 里**。

为什么是这个形态：App 的登录入口在 UI 层（`ui/login/SourceLoginViewModel`，要用户填凭据），
JVM 里没有那个界面；而上游自己在设备上「收登录态」那一步（`BackstageWebView.setCookie`）
我们**已经接通了**——`SourceCookies` 会按源 URL 从 profile 里读 cookie 注进 App 的
`CookieStore`（`enabledCookieJar` 默认 true，后续请求自动带上）。所以缺的只是「登一次」。

⚠️ 两点：① profile 是**独占**的——这个窗口开着时不要同时跑批/调试（会因 profile 被占用
而启动失败，那是实测踩过的坑）；本脚本按回车后会自己把浏览器关掉。
② cookie 会过期，过期了就再跑一次这个脚本。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 与 `appservice/…/BrowserSession.profileDir()` **同一套**默认：改一边必须改另一边，
#: 否则「预热登的」和「跑批读的」不是同一个 profile——现象是「明明登了却说没登录」。
BROWSER_CANDIDATES = (
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


def find_browser() -> str | None:
    for c in BROWSER_CANDIDATES:
        if pathlib.Path(c).is_file():
            return c
    return None


def profile_dir(explicit: str = "") -> pathlib.Path:
    """与 Kotlin 侧同序：参数 > 环境变量 > 默认（临时目录下的固定名）。"""
    if explicit:
        return pathlib.Path(explicit)
    env = os.getenv("LEGADO_BROWSER_PROFILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(tempfile.gettempdir()) / "legado-appservice-profile"


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_debug_port(port: int, timeout: float = 20.0) -> bool:
    """起没起来的**唯一判据是调试端口应答**（不是进程还在不在：Chromium 会移交进程）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json("http://127.0.0.1:%d/json/version" % port, timeout=2.0)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_port_gone(port: int, timeout: float = 10.0) -> bool:
    """调试端口是否已经消失——**收尾的判据是它，不是进程**。

    Chromium 启动后会把活**移交给另一个进程**，我们 Popen 的那个随时可能已经退出：
    按它的 pid `taskkill` 等于没杀（lessons §六十 就是这条，实测留过 2 个进程占着 profile）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json("http://127.0.0.1:%d/json/version" % port, timeout=2.0)
        except Exception:
            return True
        time.sleep(0.3)
    return False


async def cdp_cookies(port: int, url: str) -> list:
    """读该 URL 的 cookie（`Network.getCookies`；HttpOnly 也拿得到）。"""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get("http://127.0.0.1:%d/json/list" % port) as r:
            targets = await r.json()
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            return []
        async with s.ws_connect(page["webSocketDebuggerUrl"], heartbeat=None) as ws:
            await ws.send_json({"id": 1, "method": "Network.getCookies",
                                "params": {"urls": [url]}})
            async for msg in ws:
                data = msg.json()
                if data.get("id") == 1:
                    return (data.get("result") or {}).get("cookies") or []
    return []


async def cdp_close(port: int) -> None:
    """协议级关掉浏览器（`Browser.close`，与 BrowserBridge 同一条路）。通常不回响应就断连。"""
    import aiohttp
    try:
        info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=3.0)
    except Exception:
        return
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url, heartbeat=None) as ws:
                await ws.send_json({"id": 1, "method": "Browser.close"})
                await ws.receive(timeout=3)
    except Exception:
        pass


def url_of_source(path: str) -> str:
    """从源 JSON 里取「该去哪个地址登录」：`loginUrl` 优先，否则 `bookSourceUrl`。"""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    src = data[0] if isinstance(data, list) and data else data
    if not isinstance(src, dict):
        raise SystemExit("读不出书源对象：%s" % path)
    url = (src.get("loginUrl") or src.get("bookSourceUrl") or "").strip()
    if not url:
        raise SystemExit("这条源既没有 loginUrl 也没有 bookSourceUrl：%s" % path)
    if url.startswith("@js:") or url.startswith("<js>"):
        raise SystemExit("这条源的 loginUrl 是一段 JS（不是地址），没法当登录页打开：%s" % url[:60])
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="要打开的登录页地址")
    ap.add_argument("-i", "--input", default="", help="源 JSON：自动取 loginUrl（否则 bookSourceUrl）")
    ap.add_argument("--profile", default="", help="浏览器 profile 目录（默认与 JVM 侧同一套）")
    ap.add_argument("--no-wait", action="store_true", help="不等回车，读一次 cookie 就收尾（自检用）")
    args = ap.parse_args()

    if not args.url and not args.input:
        ap.error("至少给一个：--url 或 -i <源 JSON>")
    url = args.url or url_of_source(args.input)
    exe = find_browser()
    if exe is None:
        print("找不到 Edge/Chrome，无法预热登录")
        return 2
    profile = profile_dir(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()

    print("浏览器 : %s" % exe)
    print("profile: %s" % profile)
    print("地址   : %s" % url)
    print("（这个 profile 是独占的：现在不要同时跑批/调试）")
    # **有界面**：这一步就是要人自己登（我们不在 CDP 里代登录，也不碰账号密码）
    proc = subprocess.Popen(
        [exe, "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % profile,
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_debug_port(port):
            print("浏览器没起来（调试端口 %d 没应答）——先看窗口有没有报错" % port)
            return 3
        if args.no_wait:
            print("--no-wait：直接读一次 cookie")
        else:
            print()
            print(">>> 在弹出的窗口里登录。登完回到这里按回车 <<<")
            try:
                input()
            except EOFError:
                pass

        import asyncio
        cookies = asyncio.run(cdp_cookies(port, url))
        print()
        print("Cookie: %d 条" % len(cookies))
        for c in cookies[:8]:
            print("  %s=%s  domain=%s" % (c.get("name"), str(c.get("value"))[:16], c.get("domain")))
        if len(cookies) > 8:
            print("  …（还有 %d 条）" % (len(cookies) - 8))
        if cookies:
            print()
            print("已留在 profile 里。之后跑批/调试会按**源 URL** 自动读出来注入——"
                  "源用得到的域要和这里登的一致。")
        else:
            print()
            print("该 URL 一条 cookie 都没有：确认一下登的是这个域，或登录没成功。")
    finally:
        import asyncio
        asyncio.run(cdp_close(port))
        if wait_port_gone(port):
            print("浏览器已关闭。")
        else:
            # 没关掉就**明说**（它占着 profile）：不要说「已关闭」，等下一次跑批
            # 报「浏览器不可用」才发现——那是把「我们的收尾没做」读成环境问题
            print("⚠ 浏览器还在（调试端口仍应答）——请手动关掉那个窗口：")
            print("   它占着 profile，跑批/调试会因 profile 被占用而启动失败。")
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""浏览器桥的 Python 侧小件：**同一个 profile、同一个浏览器**，让「人过一次」这条路可复用。

这里的每一样都只为一件事服务：**我们不代用户过验证、也不碰凭据**——只把那个 profile 的
窗口开在要过的那一页上，等他自己弄完，凭据就留在 profile 里（桥每渲染完一次都会把 cookie
收进 `CookieStore`，见 A3），之后引擎的请求自然带着它。

**与上游同形**：App 里对应的是 `SourceVerificationHelp.startBrowser` /
`java.startBrowserAwait(url, title)`（打开内置浏览器让人过，过完接着走）——它们的立场同样是
「问人」，不是「骗过」那道墙。

**profile 是独占的**：这个窗口开着时不要同时跑批 / 调试（会被占用而启动失败，实测踩过）。
本模块负责在收尾时关掉它，关不掉会**明说**（见 `close_browser`）。

这些函数原本长在 `scripts/jvm_login.py` 里，抽出来是因为生成链也要用（`human_gate`）。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Dict, List, Optional

#: 与 `appservice/…/BrowserSession.profileDir()` **同一套**默认：改一边必须改另一边，
#: 否则「预热登的」和「跑批读的」不是同一个 profile——现象是「明明登了却说没登录」
#: （契约测试 `tests/test_jvm_debug_contract.py::TestBrowserProfileParity` 钉着）。
PROFILE_DIR_NAME = "legado-appservice-profile"

#: 与 `BrowserBridge.CANDIDATES` 同一批（Edge 优先，Chrome 兜底）。**两边要一致**：
#: 浏览器换了，profile 也读不回来。
BROWSER_CANDIDATES = (
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


def find_browser() -> Optional[str]:
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
    return pathlib.Path(tempfile.gettempdir()) / PROFILE_DIR_NAME


def free_port() -> int:
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
    按它的 pid `taskkill` 等于没杀（lessons §六十，实测留过 2 个进程占着 profile）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json("http://127.0.0.1:%d/json/version" % port, timeout=2.0)
        except Exception:
            return True
        time.sleep(0.3)
    return False


async def cdp_cookies(port: int, url: str) -> List[Dict[str, Any]]:
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


def close_browser(port: int, proc: Optional[subprocess.Popen] = None) -> bool:
    """关掉浏览器；关不掉就**明说**（它占着 profile，不说就会被读成环境问题）。"""
    import asyncio
    asyncio.run(cdp_close(port))
    if wait_port_gone(port):
        return True
    print("⚠ 浏览器还在（调试端口仍应答）——请手动关掉那个窗口：")
    print("   它占着 profile，跑批/调试会因 profile 被占用而启动失败。")
    if proc is not None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    return False


def open_for_human(url: str, prompt: str = "", profile: str = "",
                   read_cookies: bool = True) -> bool:
    """**在桥那个 profile 里开一个有界面的浏览器**，让人自己过验证 / 登录；回车表示弄完了。

    返回「人说过完了吗」——回车为真；`EOFError`（没有人在终端那侧）与找不到浏览器都为假。
    **阻塞**是有意的（与上游 `startBrowserAwait` 同形）：这一步的调用方就是「人正坐在屏幕前」
    的那条路（CLI / 交互式生成），后台 job 不走它。

    `read_cookies=True` 时顺手报一下 cookie 条数——用户要看得出「这次过的东西有没有留下」。
    """
    exe = find_browser()
    if exe is None:
        print("找不到 Edge/Chrome，没法开浏览器过验证")
        return False
    prof = profile_dir(profile)
    prof.mkdir(parents=True, exist_ok=True)
    port = free_port()
    print("浏览器 : %s" % exe)
    print("profile: %s" % prof)
    print("地址   : %s" % url)
    print("（这个 profile 是独占的：现在不要同时跑批/调试）")
    proc = subprocess.Popen(
        [exe, "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % prof,
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    done = False
    try:
        if not wait_debug_port(port):
            print("浏览器没起来（调试端口 %d 没应答）——先看窗口有没有报错" % port)
            return False
        print()
        print(">>> %s" % (prompt or "在弹出的窗口里把那道验证 / 登录过一下。弄完回到这里按回车"))
        print(">>> 过了之后这一页就能拿到真实内容了（凭据留在 profile 里）")
        try:
            input()
            done = True
        except EOFError:
            print("（没有可用的终端输入，当作没人工过）")
        if read_cookies:
            import asyncio
            cookies = asyncio.run(cdp_cookies(port, url))
            print("Cookie: %d 条（留在 profile 里，之后的请求会带上）" % len(cookies))
    finally:
        close_browser(port, proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return done

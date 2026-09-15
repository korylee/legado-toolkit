# -*- coding: utf-8 -*-
"""连 App 调试：借「阅读」App 内建的调试 WebSocket 跑一次完整链路。

为什么需要它
------------
本项目是给「阅读」App 产源的**离线**工具，但实测 56.5% 的源含 JS 规则
（``<js>`` / ``@js:``），静态回放不了（那是 core/rules/replayer.py 的能力边界，
不是源坏了）。而 App 内建了一个**给 PC 用的调试 WebSocket**：Rhino JS、cookie、
webView、正文分页全都在。我们只当客户端——**App 一行源码都不用改**。

协议（已实测，探测工具见 tools/probe_app_debug.py）
---------------------------------------------------
- 连接 ``ws://<手机IP>:1123/bookSourceDebug``（**WS 端口 = App 的 HTTP 端口 + 1**）
- 请求 ``{"tag": <书源导入原文>, "key": <调试目标>}``
- **``tag`` 必须用 ``raw_json.bookSourceUrl``（导入原文）**：我们库里的
  ``source_url`` 是规范化过的（``strip().rstrip("/").lower()``，见
  core/loader.py），实测与原文有 20.7% 不一致（主要是尾部斜杠）。用错时 App 的
  ``getBookSource(tag)?.let{}`` 查不到源就**什么都不做**——表现为静默无响应，
  极难排查。所以本模块的任何调用方都必须传原文，别图省事传库里的键。
- ``key`` 四种格式：``关键字`` 从搜索开始 / ``发现::<URL>`` 从发现页开始 /
  ``++<URL>`` 从目录页开始 / ``--<URL>`` 从正文页开始
- 响应是一串带 ``[mm:ss.SSS]`` 相对耗时前缀的文本消息，对端跑完主动关闭
  （``CloseReason`` "调试结束"）

本模块做两件事：

1. **调试 WS**：收事件 → 聚合成 steps[] → 抓页面补 pages[]，产出与
   ``core/verify.py:verify_chain`` 同形状的结果，供前端抽屉直接消费。
2. **App 的 HTTP 接口**（见下方「HTTP 接口」一节）：问 App 有没有某个源、
   把源推过去。用来把「调试 WS 对未知 tag 静默无响应」这个坑变成可判定的状态。

**纯标准库实现**（不装 websockets）：项目不为这一个用途加依赖，
WS 客户端代码在 tools/probe_app_debug.py 里已实测跑通，原样搬过来。
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import quality as Q
from core.fetch import fetch, parse_source_header

# ------------------------------------------------------------------ 常量

#: 调试 WebSocket 的路径（App 的 ``BookSourceDebug`` 固定路由）
WS_PATH = "/bookSourceDebug"
#: App 的 HTTP 端口(1122) + 1。App 通知栏里显示的是 HTTP 地址，别拿它当 WS 端口
DEFAULT_DEBUG_PORT = 1123
#: 建立 TCP 连接的等待上限（秒）。局域网内连不上会很快拒连，不用等太久
CONNECT_TIMEOUT = 10
#: 单帧等待上限（秒）。对端跑完会主动 close，这里只是兜底
RECV_TIMEOUT = 20
#: 最多抓几个页面。**这个上限是按「去重后的 page_id 个数」算的，不是按段数**：
#:   - 关键字模式：搜索 / 详情 / 正文 → 3 个 id（目录页与详情页共用 detail，见下）
#:   - 发现模式：发现 / 详情 / 正文 → 3 个 id（同样是 3，正文页不会被挤掉。
#:     发现页→详情页→目录页→正文页 是 4 个段，但目录页仍与详情页共用 detail）
#: 但**余量为零**：若将来有人把 ``PAGE_IDS["toc"]`` 拆成独立 id，发现模式就会
#: 出现第 4 个 id，而 ``fetch_debug_pages`` 的上限判断在循环**开头**——被丢掉的
#: 恰恰是排在最后的正文页（最不该丢的一页）。改 ``PAGE_IDS`` 前先看
#: ``tests/test_app_debug.py:TestFetchPages`` 里的两条页面数用例。
MAX_PAGES = 3

#: App 的段名 → ``steps[].name``。抽屉的 STEP_LABELS 认的就是这 5 个名字
#: （``explore`` 是本次新增的第 5 个），拼错会退化成显示原始 name
#: （不影响功能，但没必要）
SEGMENT_NAMES = {"搜索": "search", "发现": "explore", "详情": "bookUrl",
                 "目录": "toc", "正文": "content"}

#: ``steps[].name`` → ``pages[].id``。**目录页与详情页共用 detail**：Legado 在
#: ``ruleBookInfo.tocUrl`` 为空时复用 book URL 解析目录（Debug.kt:318-322），
#: 两者本就是同一个页面的可能性最大；共用后最多正好 3 页，正文页不会被挤掉。
#:
#: **发现页必须单独一个 id，不能并进 detail**：发现页是分类/榜单页，详情页是
#: 某本书的页，两者是**不同的 URL**。共用 id 的话 ``quality.new_page`` 的
#: 「先到先得」会让先抓到的发现页 HTML 顶掉详情页（或反过来），抽屉里两页都
#: 失真——那正是「看源码改规则」失去地基。多出的这一个 id 不破 MAX_PAGES，
#: 因为发现模式里没有搜索段（理由见 MAX_PAGES 的注释）。
PAGE_IDS = {"search": "search", "explore": "explore", "bookUrl": "detail",
            "toc": "detail", "content": "chapter"}

# ------------------------------------------------------------------ 正则

#: App 给每条消息加的 ``[mm:ss.SSS]`` 相对耗时前缀
_PREFIX_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]\s*")
#: 段起始：``︾开始解析搜索页`` / ``︾开始解析发现页``
_START_RE = re.compile(r"^︾开始解析(搜索|发现|详情|目录|正文)页")
#: 段结束：``︽搜索页解析完成`` / ``︽发现页解析完成``
_DONE_RE = re.compile(r"^︽(搜索|发现|详情|目录|正文)页解析完成")
#: 抓取成功：``≡获取成功:https://...``
_URL_RE = re.compile(r"^≡获取成功[:：](.+)$")
#: 入口事件：``⇒开始搜索关键字:我`` / ``⇒开始访目录页:<URL>`` /
#: ``⇒开始访问发现页:<URL>``（发现是「访问」而不是「访」，故 ``问`` 可选）
_ENTRY_RE = re.compile(r"^⇒开始(?:搜索关键字|访(?:问)?(搜索|发现|详情|目录|正文)页)")
#: 统计行前缀（``◇目录总数:108``）
_STAT_MARK = "◇"

#: 错误行信号词。**只在事件文本行首匹配**，理由：
#:   - 正文全文也是一条事件，里面出现「失败」二字完全可能（小说情节），
#:     行首匹配不会命中它——正文那条以 ``└`` 开头
#:   - 代价是 ``≡获取失败:...`` 这类带标记的错误行识别不到，此时该段会落到
#:     ``unknown``（= 我们的判定不了），而不是被误判成 ``fail``。按「不误杀」
#:     的立场，这个方向的漏判是可接受的
ERROR_PREFIXES = (
    "✕", "✖", "❌", "×",
    "异常", "错误", "失败",
    "Error", "error", "Exception",
    # Rhino 抛错时 App 会把异常栈打过来，首行是异常类全名（如
    # ``java.lang.NullPointerException``）。**只认 java./javax. 这两个包前缀**：
    # 再宽的 ``com.`` / ``org.`` 收益极小（栈里那些 ``at ...`` 行不是首行），
    # 却会凭空多出误判成 fail 的机会——按「不误杀」的立场不划算
    "java.", "javax.",
)

#: 空事件流的提示。零事件几乎只有两种原因，直接写清楚，省得下一个人重新踩
_ZERO_EVENT_HINT = (
    "App 没有推出任何事件。最常见的原因是 tag 与 App 里的书源对不上——"
    "tag 必须用导入原文 bookSourceUrl（规范化过的 URL 对不上时会静默无响应）；"
    "其次是 key 格式不合法。"
)

# ------------------------------------------------------------------ HTTP 接口
# App 的 Web 服务（同一个 KtorServer）除了调试 WS，还开着一组 HTTP 接口，
# 端口 = WS 端口 - 1（WebService.kt:166 起 WS 用的就是 webPort + 1）。
#
# 为什么需要它们：调试 WS 的 tag 是拿去 App 库里**精确匹配** bookSourceUrl
# （BookSourceDao.kt:277），App 里没有这个源时 `getBookSource(tag)?.let{}`
# 什么都不做——表现为静默无响应。有了 HTTP 侧，「App 里有没有这个源」就能
# 明确问出来，也能把源直接推进去，省掉
# 「保存 → 导出 → 扫码 → App 导入 → 才轮到调试」的整圈往返。
#
# 依据 legado-with-MD3：
#   KtorServer.kt:57            POST /saveBookSource   body = 单个书源裸 JSON
#   KtorServer.kt:121           GET  /getBookSource?url=<bookSourceUrl>
#   BookSourceController.kt:33  @Insert(onConflict = REPLACE) → 推送是幂等的
#   ReturnData.kt               {isSuccess, errorMsg, data}
#: App 的 HTTP 端口（= 默认 WS 端口 1123 - 1）
DEFAULT_HTTP_PORT = 1122
#: HTTP 侧的等待上限（秒）。局域网内不通会很快拒连
HTTP_TIMEOUT = 8


def http_port_for(ws_port: Optional[int]) -> int:
    """WS 端口 → HTTP 端口。传 0/None 时按默认值推。"""
    port = int(ws_port or 0)
    return (port - 1) if port > 1 else DEFAULT_HTTP_PORT


def _app_http(host: str, path_and_query: str, ws_port: Optional[int],
              data: Optional[bytes] = None, timeout: int = HTTP_TIMEOUT) -> dict:
    """调一次 App 的 HTTP 接口，返回解析后的 ReturnData。

    连不上/超时/返回不是 JSON 都会抛——调用方决定怎么翻译成给用户的话。
    """
    base = "http://%s:%d" % (str(host or "").strip(), http_port_for(ws_port))
    req = urllib.request.Request(
        base + path_and_query, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise ValueError("App 返回的不是预期结构: %s" % raw[:200])
    return parsed


def push_source(host: str, source: Dict[str, Any], ws_port: Optional[int] = None,
                timeout: int = 15) -> Tuple[bool, str]:
    """把书源推进 App 的库（幂等：App 侧 insert 是 REPLACE）。

    返回 ``(是否成功, 错误信息)``。**这会改动用户 App 里的数据**——必须由
    用户显式触发，不要在流程里自动调用。名字或 URL 为空时 App 会拒绝，
    这里先拦一道，省得把"转换源失败"这种模糊报错丢给用户。
    """
    name = str((source or {}).get("bookSourceName", "") or "").strip()
    url = str((source or {}).get("bookSourceUrl", "") or "").strip()
    if not name or not url:
        return False, "源名称和 URL 不能为空"
    body = json.dumps(source, ensure_ascii=False).encode("utf-8")
    try:
        d = _app_http(host, "/saveBookSource", ws_port, data=body, timeout=timeout)
    except Exception as e:
        return False, "推送失败（%s: %s）" % (type(e).__name__, e)
    ok = bool(d.get("isSuccess"))
    return ok, "" if ok else str(d.get("errorMsg") or "App 未接受该源")


def app_has_source(host: str, source_url: str, ws_port: Optional[int] = None,
                   timeout: int = HTTP_TIMEOUT) -> bool:
    """问 App 里有没有这个源（按 ``bookSourceUrl`` 精确匹配）。

    连不上会抛异常——「连不上」和「连上了但没有这个源」是两件事，别合并。
    """
    q = urllib.parse.urlencode({"url": source_url or ""})
    d = _app_http(host, "/getBookSource?" + q, ws_port, timeout=timeout)
    return bool(d.get("isSuccess"))


def preflight(host: str, source_url: str, ws_port: Optional[int] = None,
              timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    """连 App 调试前的预检：把「静默无响应」拆成能对症下药的状态。

    返回 ``{"state": ..., "error": ...}``，state 取值：

      - ``unreachable``：HTTP 侧就连不上。App 的「Web 服务」没开、IP 不对、
        或手机不在同一局域网。
      - ``missing``：连上了，但 App 库里没有这个源（调试 WS 会静默不响应）。
        最常见的原因是 tag 用了规范化过的 URL，而不是导入原文。
      - ``ready``：连上了且源在库里，可以调试。
    """
    if not str(host or "").strip():
        return {"state": "unreachable", "error": "没有填 App 的 IP"}
    if not str(source_url or "").strip():
        return {"state": "unreachable", "error": "源没有 bookSourceUrl，无法调试"}
    try:
        has = app_has_source(host, source_url, ws_port, timeout=timeout)
    except Exception as e:
        return {"state": "unreachable",
                "error": "连不上 App（%s: %s）。请确认 App 里已打开「Web 服务」、"
                         "手机与电脑在同一局域网、端口填的是 App 显示的 HTTP 端口。"
                         % (type(e).__name__, e)}
    if not has:
        return {"state": "missing",
                "error": "已连上 App，但它的书源里没有这个源——调试 WebSocket 对"
                         "查不到的 tag 会静默不响应。可以用「推送到 App」把它发过去。"}
    return {"state": "ready", "error": ""}


# ------------------------------------------------------------------ WS 客户端
# 以下四个函数是从 tools/probe_app_debug.py 的实测版本**原样搬过来**的
# （那个 CLI 现在反过来调本模块）。**不要顺手「优化」握手/分帧细节**——
# 它们对上的是 App 里那个 WebSocket 服务端，改错了本地验证不出来。


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节。对端关闭（读回空）时抛 ConnectionError。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭")
        buf += chunk
    return buf


def _handshake(sock: socket.socket, host: str, port: int, path: str) -> None:
    """RFC6455 握手。响应首行不是 101 即抛 ConnectionError。"""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
    )
    sock.sendall(req.encode())
    # 读响应头（到 \r\n\r\n）
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1)
    text = head.decode("latin-1")
    if "101" not in text.split("\r\n")[0]:
        raise ConnectionError("握手失败：%s" % text.split("\r\n")[0])


def _send_text(sock: socket.socket, payload: str) -> None:
    """发包。客户端帧必须掩码（RFC6455 5.3），否则对端会直接断连。"""
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    n = len(data)
    if n < 126:
        header = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    sock.sendall(header + mask + masked)


def _recv_frame(sock: socket.socket):
    """收一帧，返回 ``(opcode, payload_bytes)``。"""
    h = _recv_exact(sock, 2)
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mkey = _recv_exact(sock, 4) if masked else b""
    data = _recv_exact(sock, n) if n else b""
    if masked:
        data = bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
    return opcode, data


def collect_debug_events(host: str, tag: str, key: str,
                         port: Optional[int] = None,
                         timeout: int = 60) -> List[Dict[str, Any]]:
    """连上 App 跑一次调试，返回事件列表 ``[{"t": 秒, "text": 原文}, ...]``。

    ``t`` 取 App 前缀里的相对耗时（与 App 日志时间轴一致）；没有前缀时退回本地
    计时。``text`` 是**收到的原文**（含前缀）——聚合时再剥，这样 events 保留
    完整证据，values 又能按前端习惯去掉噪音。

    连不上 / 握手失败会抛异常（带原因），由调用方 decide 怎么呈现；
    正常结束（对端 close 或一段时间无新帧）返回已收到的事件。
    """
    host = str(host or "").strip()
    if not host:
        raise ValueError("缺少 App 的 IP")
    port = int(port or DEFAULT_DEBUG_PORT)
    if not 0 < port < 65536:
        raise ValueError("端口不合法：%s" % port)

    key = str(key or "").strip() or "我"
    events: List[Dict[str, Any]] = []
    # 连接超时单独给：局域网拒连/不可达时能快速失败，而不是让用户等满 timeout
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(min(timeout, RECV_TIMEOUT))
        _handshake(sock, host, port, WS_PATH)
        _send_text(sock, json.dumps({"tag": str(tag or ""), "key": key},
                                    ensure_ascii=False))
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                opcode, data = _recv_frame(sock)
            except socket.timeout:
                break               # 一段时间没动静：当作结束（正常是收到 close 帧）
            except (ConnectionError, OSError):
                break               # 对端硬关闭（没发 close 帧）
            if opcode == 0x8:       # close：App 跑完主动关，正常路径
                break
            if opcode == 0x9:       # ping → 必须回 pong，否则对端会判死
                sock.sendall(struct.pack("!BB", 0x8A, 0x80) + os.urandom(4))
                continue
            if opcode == 0xA:       # pong
                continue
            text = data.decode("utf-8", "replace")
            events.append(_make_event(text, time.time() - t0))
    finally:
        sock.close()
    return events


def _prefix_seconds(match: "re.Match") -> float:
    """把 ``[mm:ss.SSS]`` 的匹配结果换成秒。"""
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    milli = match.group(3) or "0"
    # 只取 3 位小数：``[00:01.5]`` 是 0.5 秒而不是 0.005 秒
    return minutes * 60 + seconds + int(milli.ljust(3, "0")[:3]) / 1000.0


def _make_event(text: str, fallback_t: float = 0.0) -> Dict[str, Any]:
    """组装一条事件（原文 + 相对耗时）。"""
    m = _PREFIX_RE.match(text)
    t = _prefix_seconds(m) if m else float(fallback_t)
    return {"t": round(t, 3), "text": text}


def _strip_prefix(text: str) -> str:
    """去掉 App 的 ``[mm:ss.SSS]`` 前缀，保留事件正文。"""
    return _PREFIX_RE.sub("", str(text or ""))


def _is_error(body: str) -> bool:
    """该行是不是错误行（口径见 ``ERROR_PREFIXES`` 的注释）。"""
    return bool(body) and body.startswith(ERROR_PREFIXES)


def _infer_entry_name(texts: Sequence[str]) -> str:
    """从入口事件猜段名（``⇒开始访目录页`` → toc）。

    只在**完全没有分段信号**时用作兜底：``--URL`` / ``++URL`` 这类单段运行，
    如果 App 没推 ``︾开始解析X页``（或我们漏收了），至少还能凭入口把段名定下来。
    """
    for text in texts:
        m = _ENTRY_RE.match(text)
        if m:
            # 「⇒开始搜索关键字」没有段名分组，它只可能是搜索
            return SEGMENT_NAMES.get(m.group(1) or "", "search")
    return ""


# ------------------------------------------------------------------ 聚合（纯逻辑）

def _split_segments(texts: Sequence[str]) -> List[Dict[str, Any]]:
    """按 ``︾开始解析X页`` / ``︽X页解析完成`` 把事件切成段。

    - 首段之前的事件（``⇒开始搜索关键字`` / ``⇒开始访目录页``）**归入第一段**：
      它们描述的就是这次运行从哪一段开始，天然属于紧随其后的那一段
    - **一个分段信号都没有时**（``--URL`` / ``++URL`` 的单段运行可能如此），
      全部事件归到一个段，段名从入口事件推断，推不出就按 ``content``
    - 段起始行与结束行都算本段的事件：这样 values 就是该段的完整流水，
      与实测抓到的文本块逐行对得上
    """
    segments: List[Dict[str, Any]] = []
    pending: List[str] = []          # 首个 ︾ 之前的事件
    for raw in texts:
        text = _strip_prefix(raw)
        m = _START_RE.match(text)
        if m:
            segments.append({"name": SEGMENT_NAMES[m.group(1)], "values": [text]})
            continue
        if segments:
            segments[-1]["values"].append(text)
        else:
            pending.append(text)

    if not segments:
        # 空事件流不产段：凭空造一个空的「正文」步会让抽屉显示一个假的正文步，
        # 调用方（run_app_debug）对零事件本来就有专门的提示
        if not pending:
            return []
        name = _infer_entry_name(pending) or "content"
        segments = [{"name": name, "values": list(pending)}]
    elif pending:
        # 浅拷贝后再拼：不改调用方持有的列表（与 verify.py 的附注处理同一纪律）
        segments[0]["values"] = list(pending) + segments[0]["values"]
    return segments


def build_steps(events: Sequence[Any]) -> List[Dict[str, Any]]:
    """**纯函数**：事件列表 → ``steps[]``（只做分段与判定，不抓页面）。

    ``events`` 可以是 ``collect_debug_events`` 的返回值（``{"t","text"}``），
    也可以是裸文本列表——聚合只用到文本，这样测试不必构造事件字典。

    每段产出与 ``verify_chain`` 完全同形的 step（经
    ``quality.Judgement.as_step_dict`` 摊平，**不在这里抄一份字典字面量**）：
      - ``name``：search / explore / bookUrl / toc / content
        （``explore`` 只在 key 为 ``发现::<URL>`` 时出现，见 ``SEGMENT_NAMES``）
      - ``verdict``：段内有错误行 → fail；有 ``︽X页解析完成`` → pass；否则 unknown
      - ``values``：该段内所有事件原文（已去耗时前缀，保留 ┌/└ 成对结构）
      - ``url``：段内 ``≡获取成功:<URL>`` 的 URL（没有则空）
      - ``matched_html``：**恒为空串**——App 只推文本，不给 HTML
      - ``notes``：该段里的 ``◇`` 统计行
    """
    texts = [(_event_text(e)) for e in (events or [])]
    steps: List[Dict[str, Any]] = []
    for seg in _split_segments(texts):
        values = seg["values"]
        name = seg["name"]
        done = ""
        error = ""
        for line in values:
            if _DONE_RE.match(line):
                done = line
            elif not error and _is_error(line):
                error = line
        notes = [v for v in values if v.startswith(_STAT_MARK)]
        url = _first_url(values)
        evidence = Q.build_evidence(values)
        if error:
            # reason 只取前 200 字符：完整的错误行仍在 values 里，
            # 但失败原因要能一眼看完，不能是一条 2000 字的 Java 栈
            j = Q.Judgement(Q.VERDICT_FAIL, error[:200], Q.SHAPE_TEXT, notes, evidence)
        elif done:
            j = Q.Judgement(Q.VERDICT_PASS, "", Q.SHAPE_TEXT, notes, evidence)
        else:
            j = Q.Judgement(Q.VERDICT_UNKNOWN, "该段没有解析完成信号",
                            Q.SHAPE_TEXT, notes, evidence)
        # 卡片上那行小字：优先失败原因，其次 ◇ 统计（比「20 条事件」有信息量）
        detail = j.reason or ("；".join(notes) if notes else "%d 条事件" % len(values))
        steps.append(j.as_step_dict(
            name, url=url, page_id=PAGE_IDS.get(name, ""),
            values=values, matched_html="", detail=detail,
        ))
    return steps


def _event_text(event: Any) -> str:
    """从事件里取文本。兼容裸字符串（测试与手工构造的场景）。"""
    if isinstance(event, dict):
        return str(event.get("text", "") or "")
    return str(event or "")


def _first_url(values: Sequence[str]) -> str:
    """段内第一个 ``≡获取成功:<URL>`` 的 URL，没有则空串。"""
    for line in values:
        m = _URL_RE.match(line)
        if m:
            return m.group(1).strip()
    return ""


# ------------------------------------------------------------------ 页面抓取

def fetch_debug_pages(steps: Sequence[Dict[str, Any]], source: Optional[Dict[str, Any]] = None,
                      proxy: str = "", timeout: int = 15) -> List[Dict[str, Any]]:
    """按 steps 抓页面，返回 ``pages[]``（与 verify_chain 同形状）。

    只抓「搜索页 / 详情页 / 正文页 / 发现页」各一个——按 ``page_id`` 去重后
    天然 ≤ ``MAX_PAGES`` 个（发现模式是 发现/详情/正文，关键字模式是
    搜索/详情/正文，不会同时出现；上限的余量分析见 ``MAX_PAGES`` 的注释）。

    **必须用书源自己的 header / charset**——与 verify.py 一致：不带书源头抓回来
    的 HTML 是失真的，「看源码改规则」就失去地基。

    **抓失败不抛**：App 那边的链路已经跑完了，一份页面抓不回来只该在对应 step
    的 notes 里说明，不能让整次调试的结果崩掉（用户要看的判定仍然是有效的）。
    """
    src = source or {}
    headers, header_why = parse_source_header(str(src.get("header", "") or ""))
    charset = str(src.get("charset", "") or "").strip()
    pages: Dict[str, Dict[str, Any]] = {}

    def _note(step: Dict[str, Any], text: str) -> None:
        step["notes"] = list(step.get("notes", [])) + [text]
        step["has_notes"] = True

    # 去重看**已尝试过的 URL**，不是「已登记的 page_id」。
    # 只看 page_id 的话，第一次抓超时（没登记）会让后面共用同一 URL 的步
    # **再抓一次**——实测详情页就是这样被抓了两遍，白等一个超时。
    tried: set = set()
    for step in steps:
        if len(pages) >= MAX_PAGES:
            break
        url = str(step.get("url", "") or "")
        page_id = str(step.get("page_id", "") or "")
        if not url or not page_id or page_id in pages:
            continue            # 没抓到 URL / 该页已登记（如目录页与详情页同 id）
        if url in tried:
            # 同一个页面已经抓过（成功或失败），别重复抓——把结果共享给它
            for sid, p in pages.items():
                if p.get("url") == url:
                    step["page_id"] = sid
                    break
            continue
        tried.add(url)
        try:
            # 传 source：最多补抓 3 页，也该遵守源声明的 concurrentRate
            html = fetch(url, headers=headers, charset=charset, proxy=proxy,
                         source=source)
        except Exception as e:
            _note(step, "页面抓取失败（%s），本步判定不受影响" % e)
            continue
        Q.new_page(pages, page_id, url, html, charset=charset)
    return list(pages.values())


# ------------------------------------------------------------------ 对外入口

def run_app_debug(host: str, source_url_raw: str, key: str,
                  port: Optional[int] = None, timeout: int = 60,
                  source: Optional[Dict[str, Any]] = None,
                  proxy: str = "") -> Dict[str, Any]:
    """连 App 跑一次调试，返回与 ``verify_chain`` 同形状的结果，供前端抽屉直接消费。

    返回 ``{"source": "app", "steps": [...], "pages": [...], "all_ok": bool,
    "events": [{"t": 秒, "text": 原文}], "error": ""}``。

    ``source_url_raw`` 必须是**导入原文** ``bookSourceUrl``（理由见模块头注释）；
    ``source`` 传完整书源 dict 时才会用上它自己的 header / charset 抓页面，
    不传也能跑（页面用默认头抓，可能与 App 看到的略有差异）。
    """
    out: Dict[str, Any] = {
        "source": "app",
        "steps": [],
        "pages": [],
        "all_ok": True,
        "events": [],
        "error": "",
    }
    host = str(host or "").strip()
    try:
        events = collect_debug_events(host, source_url_raw, key,
                                      port=port, timeout=timeout)
    except Exception as e:
        # 连不上/超时**不是 500**：端口、局域网、App 的「Web 服务」开关都是
        # 用户侧能自己修的事，错误信息里要把排查方向说清楚
        out["error"] = (
            "连不上 App 的调试端口 ws://%s:%s%s（%s: %s）。"
            "请确认：App 里已打开「Web 服务」、手机与电脑在同一局域网、"
            "端口填的是 App 显示的 HTTP 端口 + 1。"
            % (host or "?", port or DEFAULT_DEBUG_PORT, WS_PATH,
               type(e).__name__, e))
        return out

    out["events"] = events
    if not events:
        out["error"] = _ZERO_EVENT_HINT
        return out

    steps = build_steps(events)
    try:
        pages = fetch_debug_pages(steps, source, proxy=proxy)
    except Exception as e:
        # 抓页面本就被设计成不抛（逐页 try）；这里是最后一道保险——
        # 绝不能因为「补证据」失败而把已经拿到的判定结果丢掉
        pages = []
        if steps:
            steps[0]["notes"] = list(steps[0]["notes"]) + ["页面抓取整体失败：%s" % e]
            steps[0]["has_notes"] = True
    out["steps"] = steps
    out["pages"] = pages
    # 与 verify_chain 同一口径：只有 fail 会让 all_ok 变 False（unknown 不算坏）
    out["all_ok"] = all(s["ok"] for s in steps)
    return out

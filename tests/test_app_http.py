# -*- coding: utf-8 -*-
"""App HTTP 侧（预检 / 推送 / 查源）与「离线重放单步」的测试。

全部离线：用一个本地假 App 服务器对上 App 的 ``ReturnData`` 契约
（``{isSuccess, errorMsg, data}``）与 ``REPLACE`` 语义，不联网、不依赖真机。

契约的一手依据在 legado-with-MD3：
  KtorServer.kt:57            POST /saveBookSource
  KtorServer.kt:121           GET  /getBookSource?url=
  BookSourceController.kt:33  @Insert(onConflict = REPLACE)
  WebService.kt:166           WS 端口 = webPort + 1
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from core.app_debug import (
    DEFAULT_HTTP_PORT,
    app_get_source,
    app_has_source,
    http_port_for,
    preflight,
    push_source,
)
from core.verify import replay_step


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _dead_ws_port(excluding: int = 0) -> int:
    """找一个「它自己和它 -1 都没人监听」的 WS 端口，用来测「连不上」。

    只挑 ws 端口本身空闲是不够的：``http_port_for`` 会去连 ``ws - 1``，
    而系统分配的端口常常是连号——下一个空闲端口很可能正好是被排除的那个，
    于是请求打到假 App 上，得到 missing 而不是 unreachable。
    """
    for _ in range(50):
        port = _free_port()
        if port - 1 == excluding:
            continue
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", port - 1))   # 能绑上 = 没人在听
            probe.close()
            return port
        except OSError:
            continue
    raise RuntimeError("找不到可用的空闲端口对")


class _FakeApp:
    """最小的假 App：只实现调试要用的两个 HTTP 接口，行为照一手源码。"""

    def __init__(self) -> None:
        self.store: dict = {}
        self.requests = 0          # 收到的请求数，用来验证「本地拦下了、压根没发」
        self.http_port = _free_port()
        # 真机上 HTTP 与 WS 端口就是差 1，这里保持同一关系才能验到端口推导
        self.ws_port = self.http_port + 1
        self._server = None

    def start(self) -> "_FakeApp":
        store = self.store

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):    # 别把请求日志打到测试输出里
                pass

            def _count(self):
                fake.requests += 1

            def _send(self, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._count()
                parsed = urlparse(self.path)
                if parsed.path == "/getBookSource":
                    url = (parse_qs(parsed.query).get("url") or [""])[0]
                    if url in store:
                        return self._send({"isSuccess": True, "errorMsg": "",
                                           "data": store[url]})
                    return self._send({"isSuccess": False, "data": None,
                                       "errorMsg": "未找到源，请检查书源地址"})
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                self._count()
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                if self.path != "/saveBookSource":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    source = json.loads(raw)
                except Exception:
                    return self._send({"isSuccess": False, "data": None,
                                       "errorMsg": "转换源失败"})
                if not source.get("bookSourceName") or not source.get("bookSourceUrl"):
                    return self._send({"isSuccess": False, "data": None,
                                       "errorMsg": "源名称和URL不能为空"})
                store[source["bookSourceUrl"]] = source    # REPLACE 语义
                self._send({"isSuccess": True, "errorMsg": "", "data": ""})

        fake = self
        self._server = HTTPServer(("127.0.0.1", self.http_port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def make_source(url: str = "https://t.example/") -> dict:
    return {"bookSourceName": "测试站", "bookSourceUrl": url, "bookSourceType": 0,
            "searchUrl": "https://t.example/s?q={{key}}",
            "ruleSearch": {"bookList": "class.item", "bookUrl": "tag.a@href"},
            "ruleToc": {"chapterList": "class.chapters@tag.a"},
            "ruleContent": {"content": "id.content"}}


class HttpPortTests(unittest.TestCase):
    def test_ws_port_minus_one(self):
        self.assertEqual(http_port_for(1123), 1122)
        self.assertEqual(http_port_for(9001), 9000)

    def test_defaults_when_unset(self):
        # 0 / None 都要落到默认值，而不是算出 -1 或 0 这种不可用端口
        for value in (None, 0, "", 1):
            self.assertEqual(http_port_for(value), DEFAULT_HTTP_PORT)


class AppHttpTests(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp().start()

    def tearDown(self):
        self.app.stop()

    def test_push_then_has_source_then_preflight_ready(self):
        url = "https://t.example/"
        src = make_source(url)
        # 推送前：连得上但库里没有 → missing（这正是调试静默无响应的原因）
        self.assertEqual(self.app.store.get(url), None)
        self.assertEqual(preflight("127.0.0.1", src, self.app.ws_port)["state"],
                         "missing")

        ok, err = push_source("127.0.0.1", src, self.app.ws_port)
        self.assertTrue(ok, err)
        self.assertEqual(preflight("127.0.0.1", src, self.app.ws_port)["state"],
                         "ready")

    def test_preflight_reports_stale_when_app_has_older_rules(self):
        # 本地改了规则但还没推 → App 里是旧版本。**这一档最坑**：直接调试跑的是
        # App 那份，结果看着正常、答的却不是你在改的东西
        url = "https://t.example/"
        push_source("127.0.0.1", make_source(url), self.app.ws_port)
        edited = make_source(url)
        edited["ruleToc"] = {"chapterList": "#changed@tag.a"}
        self.assertEqual(preflight("127.0.0.1", edited, self.app.ws_port)["state"],
                         "stale")

    def test_preflight_ignores_fields_the_app_adds_or_we_do_not_compare(self):
        # App 存的是它自己的实体：会补上它自己的默认字段、丢掉我们多给的键。
        # 比对只认那几个规则字段，否则每次都判成「不同」，白覆盖一遍
        url = "https://t.example/"
        src = make_source(url)
        push_source("127.0.0.1", src, self.app.ws_port)
        self.app.store[url] = {**src, "customOrder": 7, "enabled": True,
                               "ruleSearch": {**src["ruleSearch"], "name": "额外的"}}
        self.assertEqual(preflight("127.0.0.1", src, self.app.ws_port)["state"],
                         "ready")

    def test_push_is_idempotent(self):
        # App 侧 insert 是 REPLACE，重复推送必须照样成功（不改语义）
        url = "https://t.example/"
        source = make_source(url)
        for _ in range(3):
            ok, err = push_source("127.0.0.1", source, self.app.ws_port)
            self.assertTrue(ok, err)
        self.assertEqual(len(self.app.store), 1)

    def test_push_rejects_source_without_name(self):
        # 名字为空时 App 会拒绝，本地先拦一道，别把「转换源失败」丢给用户。
        # 断言请求数为 0 —— 只看错误文案的话，把拦截删掉也照样"通过"
        # （App 的报错里也有「名称」二字），测不出这条预检有没有生效
        ok, _err = push_source("127.0.0.1", {"bookSourceUrl": "https://x/"},
                               self.app.ws_port)
        self.assertFalse(ok)
        self.assertEqual(self.app.requests, 0)
        self.assertEqual(self.app.store, {})

    def test_has_source_is_false_for_unknown_url(self):
        self.assertFalse(app_has_source("127.0.0.1", "https://nope.example/",
                                        self.app.ws_port))

    def test_get_source_returns_the_app_copy(self):
        url = "https://t.example/"
        push_source("127.0.0.1", make_source(url), self.app.ws_port)
        got = app_get_source("127.0.0.1", url, self.app.ws_port)
        self.assertEqual(got["bookSourceUrl"], url)
        self.assertIsNone(app_get_source("127.0.0.1", "https://nope.example/",
                                        self.app.ws_port))

    def test_preflight_unreachable_does_not_raise(self):
        # 端口不通要翻译成状态，不能让异常冒到接口层变成 500
        dead = _dead_ws_port(excluding=self.app.http_port)
        src = make_source()
        result = preflight("127.0.0.1", src, dead)
        self.assertEqual(result["state"], "unreachable")
        self.assertTrue(result["error"])
        # 原始异常要留痕（detail），但**不能混进 error**——那是给用户看的动作指引，
        # 摆异常类名属于开发者视角
        self.assertTrue(result["detail"])
        self.assertNotIn("Error", result["error"])

    def test_preflight_without_host_or_url(self):
        self.assertEqual(preflight("", make_source())["state"], "unreachable")
        self.assertEqual(preflight("127.0.0.1", {})["state"], "unreachable")

    def test_preflight_uses_raw_url_not_normalized(self):
        # App 是按 bookSourceUrl 精确匹配的：尾部斜杠不同就是两个 key。
        # 这条守的是「别把规范化过的 URL 传进去」这个坑
        push_source("127.0.0.1", make_source("https://t.example"), self.app.ws_port)
        self.assertEqual(
            preflight("127.0.0.1", make_source("https://t.example"),
                      self.app.ws_port)["state"],
            "ready")
        self.assertEqual(
            preflight("127.0.0.1", make_source("https://t.example/"),
                      self.app.ws_port)["state"],
            "missing")


class ReplayStepTests(unittest.TestCase):
    """离线重放单步：改完规则不发请求就能看判定变化。"""

    HTML = ('<div class="book"><a href="/b/1">诡秘之主</a></div>'
            '<ul id="toc"><li>第一章</li><li>第二章</li></ul>'
            '<div id="content">正文内容，一段足够长的中文文本用于通过长度判定。</div>')

    def test_list_step_pass(self):
        r = replay_step(self.HTML, "class.book@tag.a@text", "search", 0)
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(r["values"], ["诡秘之主"])

    def test_toc_step_pass(self):
        r = replay_step(self.HTML, "#toc@tag.li@text", "toc", 0)
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(len(r["values"]), 2)

    def test_content_step_pass(self):
        r = replay_step(self.HTML, "#content@text", "content", 0)
        self.assertEqual(r["verdict"], "pass")

    def test_js_rule_is_unknown_not_fail(self):
        # 能力边界 ≠ 源坏了。判 fail 会误杀（lessons 第一条）
        r = replay_step(self.HTML, "<js>java.ajax(x)</js>", "toc", 0)
        self.assertEqual(r["verdict"], "unknown")
        self.assertTrue(r["rule_error"])

    def test_empty_rule_is_fail(self):
        # 空规则是源的配置错误，与「回放不了」性质不同
        r = replay_step(self.HTML, "", "toc", 0)
        self.assertEqual(r["verdict"], "fail")

    def test_list_step_uses_list_judge(self):
        # 列表步骤必须走 judge_list_step——「同源不同判」是本项目反复在消灭的东西。
        # 断言报错文案是列表语义：判成 judge_content 时会变成「正文提取为空」。
        # （只断言 verdict 的话两种情况都是 fail，测不出走错了分支）
        r = replay_step("<html></html>", "#toc@tag.li@text", "toc", 0)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("解析结果为空", r["reason"])

    def test_search_step_uses_list_judge(self):
        r = replay_step("<html></html>", "class.book@tag.a@text", "search", 0)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("解析结果为空", r["reason"])

    def test_step_name_is_normalized(self):
        # 大写笔误会把本该 toc 语义的结果带偏（quality.STEP_TOC 的注释）
        r = replay_step(self.HTML, "#toc@tag.li@text", "TOC", 0)
        self.assertEqual(r["verdict"], "pass")

    def test_download_type_skips_toc(self):
        # 下载源不解析目录（Legado 的豁免），重放要保持同一口径
        r = replay_step(self.HTML, "", "toc", 3)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("文件类", r["reason"])

    def test_empty_html_does_not_raise(self):
        r = replay_step("", "#toc@tag.li@text", "toc", 0)
        self.assertEqual(r["verdict"], "fail")


# ---------------------------------------------------------------- 变异记录
# 惯例：新增用例后把源码改坏跑一遍，确认对应条数变红，再改回来。以下均为实测。
#
#  M1  http_port_for 改成裸 `return port - 1`（去掉 >1 兜底）
#        → test_defaults_when_unset 红
#  M2  push_source 去掉名称/URL 预检
#        → test_push_rejects_source_without_name 红
#        （**这条最初守不住**：原先只断言错误文案含「名称」，而 App 的报错里
#          也有这两个字，删掉预检照样通过。改成断言「没发出请求」才守住。）
#  M3  preflight 在连不上时也返回 ready
#        → test_preflight_unreachable_does_not_raise 红
#  M3b preflight 把「App 里没有」也判成 ready（`if theirs is None` 改成 if False）
#        → test_push_then_has_source_then_preflight_ready
#          test_preflight_uses_raw_url_not_normalized 红
#  M3c 原始异常不记（detail 恒空）        | test_preflight_unreachable_does_not_raise 红
#  M5  preflight 不比规则（`if not _rules_equal` 改成 if False，永不 stale）
#        → test_preflight_reports_stale_when_app_has_older_rules 红
#  M6  _rules_equal 改成整份 dict 相等（`ours == theirs`）
#        → test_preflight_ignores_fields_the_app_adds_or_we_do_not_compare 红
#        —— 这条守的是「别拿 loader.fingerprint 比」：App 存的是它自己的实体，
#           字段集合本就不同，整份比必然每次都判「不同」，白覆盖一遍
#  M4  replay_step 的 `if step_key in _LIST_STEPS` 改成 `if False`（一律走
#      judge_content）
#        → **第一次跑仍全绿**——下载源上两个 judge 都返回 unknown，短文本两个都
#          判 pass，只断言 verdict 测不出分支走错。补了断言 reason 的
#          test_list_step_uses_list_judge / test_search_step_uses_list_judge
#          之后才变红。
#        → 教训：只断言 verdict 的用例，守不住「同源不同判」这类分支错误。

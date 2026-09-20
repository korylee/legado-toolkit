# -*- coding: utf-8 -*-
"""常驻调试 daemon 的客户端（S5-A 第二期 D1）：协议、版本键、回落。

**不跑 JVM**：daemon 那一侧用**真 socket 的假服务**（一个线程 accept、读一行请求、
回一行 JSON）——协议本身才是要钉的东西，而它横跨 Kotlin/Python 两侧，形状漂了不会报错，
只会「连上去没人应」。

守四件事：

1. **请求形状**：`file/key/out/timeout/cookie` 逐字段过去（少一个的后果是 daemon 用它
   的默认值跑**另一条源**，而那看起来像「调试结果不对」）。
2. **身份**：`ping` 只在真是我们的 daemon 时返回（端口上可能是别的程序）；
   不认路的连接一律 `None`，由调用方回落。
3. **回落**（D1 的降级路径）：daemon 起不来/半路死 → **这一次**回落直起，并留下一条 note；
   下一次调用还会再试 daemon（不是「一旦失败就永久降级」）。
4. **优雅停止**：`stop()` 必须先发 `op=stop`——只 kill 进程的话，常驻里刻意留着的
   浏览器会活下来继续占着 profile，之后所有抓页都「自愈」换临时 profile、cookie 静默全丢。
"""

from __future__ import annotations

import json
import pathlib
import socket
import tempfile
import threading
import unittest
from unittest import mock

from core import jvm_daemon
from core.jvm_daemon import DaemonError


class _FakeDaemon:
    """假 daemon：一个线程 accept、读一行、按 `op` 回一行。**够真来验协议**。"""

    def __init__(self, sig: str = "sig-a", pid: int = 4242,
                 honor_stop: bool = True) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = int(self.sock.getsockname()[1])
        self.received: list = []
        self.sig, self.pid = sig, pid
        self.honor_stop = honor_stop
        self._alive = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while self._alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("r", encoding="utf-8", newline="\n").readline()
                if not line.strip():
                    continue
                req = json.loads(line)
                self.received.append(req)
                op = req.get("op")
                resp = {"id": req.get("id"), "code": 0, "cost_ms": 7, "error": ""}
                if op == "ping":
                    resp.update(pid=self.pid, sig=self.sig)
                if op == "stop" and self.honor_stop:
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    self._alive = False
                    self.sock.close()
                    return
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_daemon_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        jvm_daemon.reset_for_tests()
        for name, val in (("info_path", lambda: self.tmp / "info.json"),
                          ("log_path", lambda: self.tmp / "daemon.log")):
            p = mock.patch.object(jvm_daemon, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.fake = _FakeDaemon()
        self.addCleanup(self.fake.stop)


class ProtocolTests(_Base):
    def test_request_carries_every_field(self) -> None:
        cfg = {"file": "D:/x/src.json", "key": "斗破", "out": "D:/x/o.ndjson",
               "timeout": 45, "cookie": "a=1"}
        r = jvm_daemon.request(cfg, self.fake.port, timeout=10)
        self.assertEqual(r["code"], 0)
        got = self.fake.received[-1]
        for k, v in cfg.items():
            self.assertEqual(got[k], v, "请求里 %s 必须原样过去" % k)

    def test_ping_reports_pid_and_sig(self) -> None:
        got = jvm_daemon.ping(self.fake.port)
        self.assertEqual(got["pid"], 4242)
        self.assertEqual(got["sig"], "sig-a")

    def test_ping_returns_none_for_a_foreign_server(self) -> None:
        """端口上可能是**别的程序**（应答不是我们的形状）——必须判成「不是我们的」。"""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        port = int(s.getsockname()[1])

        def _serve():
            conn, _ = s.accept()
            with conn:
                conn.makefile("r", encoding="utf-8", newline="\n").readline()
                conn.sendall(b'{"hello":"world"}\n')

        threading.Thread(target=_serve, daemon=True).start()
        self.assertIsNone(jvm_daemon.ping(port))

    def test_ping_returns_none_when_nothing_listens(self) -> None:
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = int(dead.getsockname()[1])
        dead.close()
        self.assertIsNone(jvm_daemon.ping(port))


class ParamsTests(_Base):
    def test_params_from_args_properties(self) -> None:
        f = self.tmp / "args.properties"
        f.write_text("# 注释行\nfile=D:/x/src.json\nkey=斗破 苍穹\nout=D:/x/o.ndjson\n"
                     "timeout=45\ncookie=a=1; b=2\n", encoding="utf-8", newline="\n")
        got = jvm_daemon.params_from_args(str(f))
        self.assertEqual(got["file"], "D:/x/src.json")
        self.assertEqual(got["key"], "斗破 苍穹")
        self.assertEqual(got["timeout"], 45)
        # value 里带 `=` 与分号：**只在第一个 = 处切**，否则 cookie 会被截断
        self.assertEqual(got["cookie"], "a=1; b=2")

    def test_missing_cookie_line_is_empty(self) -> None:
        f = self.tmp / "args.properties"
        f.write_text("file=a\nkey=b\nout=c\ntimeout=60\n", encoding="utf-8", newline="\n")
        self.assertEqual(jvm_daemon.params_from_args(str(f))["cookie"], "")


class SigTests(_Base):
    def test_sig_changes_when_kotlin_changes(self) -> None:
        """版本键的**唯一职责**：改了 Kotlin 就别用旧进程（进程里是旧类，看起来像
        「规则改了没生效」）。临时目录里造两个 .kt，改一个就该换 sig。"""
        app = self.tmp / "appservice"
        (app / "test").mkdir(parents=True)
        (app / "test" / "A.kt").write_text("class A", encoding="utf-8")
        with mock.patch.object(jvm_daemon.jvm_direct, "AGSVC", app), \
             mock.patch.object(jvm_daemon.jvm_direct, "dump_path", lambda: self.tmp / "dump.json"):
            first = jvm_daemon.source_sig()
            self.assertEqual(first, jvm_daemon.source_sig(), "同一份文件两次要一样")
            (app / "test" / "B.kt").write_text("class B", encoding="utf-8")
            self.assertNotEqual(first, jvm_daemon.source_sig(), "多了一个文件就得变")
            (app / "test" / "A.kt").write_text("class A2 extends Object", encoding="utf-8")
            self.assertNotEqual(first, jvm_daemon.source_sig(), "内容变了就得变")


class StopTests(_Base):
    def test_stop_prefers_graceful_op(self) -> None:
        info = {"pid": self.fake.pid, "port": self.fake.port, "sig": "sig-a"}
        jvm_daemon.info_path().write_text(json.dumps(info), encoding="utf-8", newline="\n")
        self.assertTrue(jvm_daemon.stop())
        self.assertEqual(self.fake.received[-1].get("op"), "stop",
                         "**必须先发 op=stop**：只 kill 的话浏览器会活下来占着 profile")
        self.assertFalse(jvm_daemon.info_path().exists(), "停完要把 info 清掉")

    def test_stop_does_not_kill_a_foreign_pid(self) -> None:
        """info 里的 pid 与端口上应答的 pid 不一致 → 不动手（pid 会被系统复用）。"""
        info = {"pid": 999999, "port": self.fake.port, "sig": "sig-a"}
        jvm_daemon.info_path().write_text(json.dumps(info), encoding="utf-8", newline="\n")
        with mock.patch.object(jvm_daemon.os, "kill") as k:
            jvm_daemon.stop()
        k.assert_not_called()
        self.assertEqual([r for r in self.fake.received if r.get("op") == "stop"], [])

    def test_stop_reports_false_when_the_daemon_ignores_it(self) -> None:
        """**「有应答」不等于「停了」**：不认 `op=stop` 的 daemon（跑旧类那种）会把指令
        当调试请求回一句「缺参数」，那也是应答。实测踩过——当时报的是「已停：True」，
        而进程还在跑。所以停止要**验端口真的静了**，验不过就老实报 False。"""
        fake = _FakeDaemon(honor_stop=False)
        self.addCleanup(fake.stop)
        info = {"pid": fake.pid, "port": fake.port, "sig": "sig-a"}
        jvm_daemon.info_path().write_text(json.dumps(info), encoding="utf-8", newline="\n")
        with mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None):
            self.assertFalse(jvm_daemon.stop(), "没真停就必须报 False")


class FallbackTests(_Base):
    """D1 的降级路径：daemon 不可用**只影响这一次**，而且要说出来。"""

    def setUp(self) -> None:
        super().setUp()
        self.args = self.tmp / "args.properties"
        self.args.write_text("file=a\nkey=b\nout=c\ntimeout=30\n", encoding="utf-8", newline="\n")
        p = mock.patch.object(jvm_daemon, "params_from_args",
                              lambda *a, **kw: {"file": "a", "key": "b", "out": "c",
                                                "timeout": 30, "cookie": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_falls_back_and_notes_it(self) -> None:
        notes: list = []
        direct_calls: list = []
        with mock.patch.object(jvm_daemon, "ensure", side_effect=DaemonError("起不来")), \
             mock.patch.object(jvm_daemon.jvm_direct, "direct_launcher",
                               lambda dump, timeout=0: (lambda: (direct_calls.append(1), (0, 3.3, "", ""))[1])):
            run = jvm_daemon.launcher_from_args({}, on_note=notes.append)
            rc, cost, _so, _se = run()
        self.assertEqual(direct_calls, [1], "daemon 挂了就必须回落直起")
        self.assertEqual(rc, 0)
        self.assertTrue(any("没能用常驻进程" in n for n in notes),
                        "没走成常驻必须说出来，不能静默：%s" % notes)

    def test_uses_daemon_when_available(self) -> None:
        notes: list = []
        with mock.patch.object(jvm_daemon, "ensure", return_value={"port": self.fake.port}), \
             mock.patch.object(jvm_daemon.jvm_direct, "direct_launcher",
                               lambda *a, **kw: (lambda: (_ for _ in ()).throw(AssertionError("不该回落")))):
            run = jvm_daemon.launcher_from_args({}, on_note=notes.append)
            rc, cost, _so, _se = run()
        self.assertEqual(rc, 0)
        self.assertEqual(notes, [])
        self.assertEqual(self.fake.received[-1]["key"], "b")

    def test_bad_response_also_falls_back(self) -> None:
        """应答形状不对（少了 code）也算不可用——**别把「没跑」当成「跑完了」**。"""
        notes: list = []
        with mock.patch.object(jvm_daemon, "ensure", return_value={"port": self.fake.port}), \
             mock.patch.object(jvm_daemon, "request", side_effect=DaemonError("应答缺 code")), \
             mock.patch.object(jvm_daemon.jvm_direct, "direct_launcher",
                               lambda dump, timeout=0: (lambda: (0, 1.0, "", ""))):
            run = jvm_daemon.launcher_from_args({}, on_note=notes.append)
            rc, _cost, _so, _se = run()
        self.assertEqual(rc, 0)
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()

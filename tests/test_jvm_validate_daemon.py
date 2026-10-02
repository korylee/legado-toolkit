"""Validate daemon 的 Python 协议层测试，不启动真实 JVM、不联网。"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import threading
import unittest
from unittest import mock

from core import jvm_validate_daemon


class ValidateDaemonProtocolTests(unittest.TestCase):
    def test_params_from_args_keeps_validate_arguments(self) -> None:
        path = pathlib.Path(".tmp_validate_args.properties")
        try:
            path.write_text(
                "file=C:/source.json\nkeyword=我\nout=C:/result.jsonl\n"
                "concurrency=1\ntimeout=25\nlimit=1\ndepth=content\n"
                "cookie=a=b\nnoStripWebview=1\nprofile=C:/profile\n",
                encoding="utf-8",
            )
            got = jvm_validate_daemon.params_from_args(str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(got["file"], "C:/source.json")
        self.assertEqual(got["out"], "C:/result.jsonl")
        self.assertEqual(got["depth"], "content")
        self.assertTrue(got["no_strip_webview"])
        self.assertEqual(got["profile"], "C:/profile")

    def test_request_sends_validate_payload_and_reads_response(self) -> None:
        seen = {}
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            with server:
                conn, _ = server.accept()
                with conn:
                    seen["payload"] = json.loads(conn.recv(8192).decode("utf-8"))
                    conn.sendall(b'{"id":7,"code":0,"cost_ms":3,"error":""}\n')

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            result = jvm_validate_daemon.request(
                {"file": "C:/a.json", "out": "C:/a.jsonl", "keyword": "我",
                 "concurrency": 1, "timeout": 25, "depth": "search"},
                port,
                2,
            )
        finally:
            thread.join(timeout=2)
        self.assertEqual(result["code"], 0)
        self.assertEqual(seen["payload"]["file"], "C:/a.json")
        self.assertEqual(seen["payload"]["out"], "C:/a.jsonl")
        self.assertEqual(seen["payload"]["concurrency"], 1)
        self.assertEqual(seen["payload"]["depth"], "search")

    def test_ping_rejects_other_daemon_kind(self) -> None:
        with mock.patch.object(jvm_validate_daemon, "_exchange",
                               return_value={"sig": "x", "kind": "debug"}):
            self.assertIsNone(jvm_validate_daemon.ping(1234))

    def test_reset_for_tests_does_not_remove_user_info_file(self) -> None:
        with mock.patch.object(jvm_validate_daemon, "_PROC", object()), \
             mock.patch.object(jvm_validate_daemon, "_LOG", object()):
            jvm_validate_daemon.reset_for_tests()
        self.assertFalse(jvm_validate_daemon._PROC)
        self.assertIsNone(jvm_validate_daemon._LOG)


class PrepareTests(unittest.TestCase):
    """批量冷启动准备（``prepare``）的分类语义。

    核心钉子：**busy 一档绝不触碰 ensure / start / _kill_proc / _stop_port**——
    ping 不通可能只是 daemon 在串行忙别人的 op，杀掉会伤及在跑的请求；判「死」
    的唯一依据是 info 里的 pid 已不在（``_pid_alive``）。
    """

    _DUMP = {"workingDir": "C:/repo"}
    _INFO = {"pid": 1, "port": 9999, "sig": "s", "started_at": 1.0}

    def setUp(self) -> None:
        jvm_validate_daemon.reset_for_tests()

    def test_prepare_reuses_hot_daemon_without_restart(self) -> None:
        """热 daemon 且签名匹配 → ready，不动任何进程（同签名不重启的钉子）。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping",
                               return_value={"sig": "s", "kind": "validate"}), \
             mock.patch.object(jvm_validate_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "start") as start, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill, \
             mock.patch.object(jvm_validate_daemon, "_stop_port") as stop:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "ready")
        self.assertEqual(got["info"]["port"], 9999)
        ensure.assert_not_called()
        start.assert_not_called()
        kill.assert_not_called()
        stop.assert_not_called()

    def test_prepare_restarts_stale_daemon_via_ensure(self) -> None:
        """ping 通但签名不符 → 交给 ensure（优雅停旧起新），outcome=started。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping",
                               return_value={"sig": "old", "kind": "validate"}), \
             mock.patch.object(jvm_validate_daemon, "source_sig", return_value="new"), \
             mock.patch.object(jvm_validate_daemon, "ensure",
                               return_value={"pid": 2, "port": 2, "sig": "new"}) as ensure, \
             mock.patch.object(jvm_validate_daemon, "start") as start:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "started")
        self.assertEqual(got["info"]["sig"], "new")
        ensure.assert_called_once()
        start.assert_not_called()

    def test_ensure_stops_mismatched_daemon_before_start(self) -> None:
        """真 ensure 的钉子（prepare 现在承载批量）：签名不符必须**先**优雅停、
        再 start——顺序反了会出现新旧两个 daemon 抢端口。"""
        calls = []
        with tempfile.TemporaryDirectory(prefix="validate_daemon_") as tmp, \
             mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "info_path",
                               return_value=pathlib.Path(tmp) / "validate_daemon.json"), \
             mock.patch.object(jvm_validate_daemon, "ping",
                               return_value={"sig": "old", "kind": "validate"}), \
             mock.patch.object(jvm_validate_daemon, "source_sig", return_value="new"), \
             mock.patch.object(jvm_validate_daemon, "_stop_port",
                               side_effect=lambda port: calls.append(("stop", port))), \
             mock.patch.object(jvm_validate_daemon, "start",
                               side_effect=lambda dump, **kw:
                                   calls.append(("start",)) or {"pid": 3, "port": 3}):
            got = jvm_validate_daemon.ensure(self._DUMP)
        self.assertEqual(got, {"pid": 3, "port": 3})
        self.assertEqual(calls, [("stop", 9999), ("start",)])

    def test_prepare_reports_busy_without_killing(self) -> None:
        """进程还活着、ping 没应答 = 忙/不确定 → 不杀、不启（本任务的核心钉子）。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "start") as start, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill, \
             mock.patch.object(jvm_validate_daemon, "_stop_port") as stop:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("不杀", got["reason"])
        self.assertIn("pid=1", got["reason"])
        ensure.assert_not_called()
        start.assert_not_called()
        kill.assert_not_called()
        stop.assert_not_called()

    def test_prepare_reports_busy_when_info_has_no_pid(self) -> None:
        """info 缺 pid（外来/残缺）→ 无法判死活，同样不杀不启。"""
        info = dict(self._INFO)
        del info["pid"]
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=info), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("pid", got["reason"])
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_prepare_restarts_when_pid_dead(self) -> None:
        """pid 已不在 = 旧 daemon 确实死了（info 是残骸）→ 清掉重启。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=False), \
             mock.patch.object(jvm_validate_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "started")
        ensure.assert_called_once()
        kill.assert_not_called()

    def test_prepare_starts_when_no_info(self) -> None:
        """info 缺失 → 连 ping 都不做（无 port 直接短路），交给 ensure 启动。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info", return_value={}), \
             mock.patch.object(jvm_validate_daemon, "ping") as ping, \
             mock.patch.object(jvm_validate_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure:
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "started")
        ping.assert_not_called()
        ensure.assert_called_once()

    def test_prepare_failure_keeps_reason_and_does_not_raise(self) -> None:
        """启动失败 → outcome=failed 且原因逐字保留；prepare 对调用方永不抛。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info", return_value={}), \
             mock.patch.object(jvm_validate_daemon, "ensure",
                               side_effect=jvm_validate_daemon.ValidateDaemonError(
                                   "Validate daemon 180s 内没起来")):
            got = jvm_validate_daemon.prepare(self._DUMP)
        self.assertEqual(got["outcome"], "failed")
        self.assertEqual(got["reason"], "Validate daemon 180s 内没起来")

    def test_pid_alive_distinguishes_self_from_fabricated_dead(self) -> None:
        """``_pid_alive`` 用真进程钉住判死：自己活着，编造的 pid 不在。
        （判死不靠端口行为：Windows 对已关闭端口的 connect 抛超时而非拒绝。）"""
        self.assertTrue(jvm_validate_daemon._pid_alive(os.getpid()))
        self.assertFalse(jvm_validate_daemon._pid_alive(4194304))
        self.assertFalse(jvm_validate_daemon._pid_alive(0))


if __name__ == "__main__":
    unittest.main()

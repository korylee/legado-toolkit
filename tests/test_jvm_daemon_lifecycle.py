"""Validate daemon 的 Python 协议层测试，不启动真实 JVM、不联网。"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import jvm_daemon


class ValidateDaemonProtocolTests(unittest.TestCase):
    """Validate 请求记录可关联到独一的 OS owner 进程实例。"""

    def test_params_from_args_keeps_validate_arguments(self) -> None:
        path = pathlib.Path(".tmp_validate_args.properties")
        try:
            path.write_text(
                "file=C:/source.json\nkeyword=我\nout=C:/result.jsonl\n"
                "concurrency=1\ntimeout=25\nlimit=1\ndepth=content\n"
                "cookie=a=b\nnoStripWebview=1\nprofile=C:/profile\n",
                encoding="utf-8",
            )
            got = jvm_daemon.validate_params_from_args(str(path))
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
            result = jvm_daemon._validate_request(
                {"file": "C:/a.json", "out": "C:/a.jsonl", "keyword": "我",
                 "concurrency": 1, "timeout": 25, "depth": "search",
                 "owner_job_id": "job-1", "owner_chunk": "2/4",
                 "owner_source_name": "example source"},
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
        self.assertEqual(seen["payload"]["owner_job_id"], "job-1")
        self.assertEqual(seen["payload"]["owner_chunk"], "2/4")
        self.assertEqual(seen["payload"]["owner_source_name"], "example source")

    def test_request_failure_includes_daemon_process_and_log_context(self) -> None:
        scan = {"port": 58966, "got": {"kind": "engine", "sig": "s", "pid": 52096},
                "owner": 52096, "occupied": {}, "free": 0, "blind": False}
        with mock.patch.object(jvm_daemon, "_exchange",
                               side_effect=jvm_daemon.DaemonError(
                                   "连不上 Validate daemon(port=58966): reset")), \
             mock.patch.object(jvm_daemon, "_scan", return_value=scan), \
             mock.patch.object(jvm_daemon, "_read_current_operation",
                               return_value={"job_id": "job-1", "chunk": "1/2", "pid": 4000}), \
             mock.patch.object(jvm_daemon, "_log_tail", return_value="last daemon line"), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            with self.assertRaises(jvm_daemon.DaemonError) as caught:
                jvm_daemon._validate_request({
                    "file": "source.json", "keyword": "x", "concurrency": 1,
                    "timeout": 10, "depth": "search", "owner_job_id": "job-1",
                    "owner_chunk": "1/2",
                }, 58966, 10)
        message = str(caught.exception)
        self.assertIn("daemon pid=52096 port=58966（应答）", message)
        self.assertIn("owner job=job-1 chunk=1/2 pid=4000", message)
        self.assertIn("last daemon line", message)

    def test_run_tracks_and_clears_operation_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validate_owner_") as root:
            marker = pathlib.Path(root) / "current.json"
            owner = {"job_id": "job-7", "chunk": "3/8",
                     "source_count": 25, "source_name": "example source"}

            def fake_request(cfg, port, timeout):
                current = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(current["job_id"], "job-7")
                self.assertTrue(current.get("process_created"))
                self.assertTrue(current.get("operation_id"))
                self.assertEqual(current["chunk"], "3/8")
                self.assertEqual(cfg["owner_source_name"], "example source")
                return {"code": 0}

            with mock.patch.object(jvm_daemon, "current_operation_path",
                                   return_value=marker), \
                 mock.patch.object(jvm_daemon, "validate_params_from_args",
                                   return_value={"timeout": 25}), \
                 mock.patch.object(jvm_daemon, "resolve",
                                   return_value={"port": 1234}), \
                 mock.patch.object(jvm_daemon, "_validate_request",
                                   side_effect=fake_request):
                result = jvm_daemon.run_validate(
                    "args.properties", socket_timeout=90, owner=owner)
            self.assertEqual(result["code"], 0)
            self.assertFalse(marker.exists())

    def test_current_operation_records_os_process_identity(self) -> None:
        marker = pathlib.Path("validate_identity_test_current.json")
        with mock.patch.object(jvm_daemon, "current_operation_path",
                               return_value=marker), \
             mock.patch.object(jvm_daemon, "process_created",
                               return_value="process-start-1"), \
             mock.patch.object(jvm_daemon, "uuid4",
                               return_value=mock.Mock(hex="operation-1")), \
             mock.patch.object(pathlib.Path, "write_text") as write_text:
            operation_id = jvm_daemon._begin_current_operation(
                {"job_id": "job-9", "chunk": "1/1"})
            recorded = json.loads(write_text.call_args.args[0])
        self.assertEqual(recorded["operation_id"], "operation-1")
        self.assertEqual(recorded["process_created"], "process-start-1")
        self.assertEqual(recorded["pid"], os.getpid())

    def test_stop_idle_cleans_info_for_exited_daemon_without_identity_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validate_stopped_") as tmp:
            info_file = pathlib.Path(tmp) / "validate_daemon.json"
            info_file.write_text(json.dumps({"pid": 42, "port": 1234}), encoding="utf-8")
            info = {"pid": 42, "port": 1234}
            state = {"process_alive": False, "current_operation": None,
                     "pid": 42, "port": 1234}
            with mock.patch.object(jvm_daemon, "status", return_value=state), \
                 mock.patch.object(jvm_daemon, "_read_info", return_value=info), \
                 mock.patch.object(jvm_daemon, "info_path", return_value=info_file), \
                 mock.patch.object(jvm_daemon, "_pid_alive", return_value=False), \
                 mock.patch.object(jvm_daemon, "process_created") as process_created:
                result = jvm_daemon.stop_idle()
                self.assertFalse(info_file.exists())
        self.assertTrue(result["stopped"], result)
        self.assertTrue(result["already_stopped"])
        process_created.assert_not_called()

    def test_stop_idle_refuses_unresponsive_or_unverified_process(self) -> None:
        state = {"process_alive": True, "responsive": False,
                 "identity_verified": False, "current_operation": None}
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_exchange") as exchange:
            result = jvm_daemon.stop_idle()
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

    def test_stop_idle_refuses_active_operation_owner(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True,
                 "current_operation": {"operation_id": "op-1", "job_id": "job-1"}}
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_exchange") as exchange:
            result = jvm_daemon.stop_idle(owner_inactive=False)
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

    def test_stop_idle_rejects_daemon_pid_instance_change(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True, "current_operation": None,
                 "pid": 42, "port": 1234}
        info = {"pid": 42, "port": 1234}
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_daemon, "process_created",
                               side_effect=["old-instance", "new-instance"]), \
             mock.patch.object(jvm_daemon, "_exchange") as exchange:
            result = jvm_daemon.stop_idle()
        self.assertFalse(result["stopped"])
        self.assertIn("身份变化", result["reason"])
        exchange.assert_not_called()

    def test_stop_idle_rejects_reused_pid_after_stop_request(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True, "current_operation": None,
                 "pid": 42, "port": 1234}
        info = {"pid": 42, "port": 1234}
        alive = [True, True, True, True, True]
        identities = ["daemon-instance", "daemon-instance", "replacement-instance"]
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_daemon, "_pid_alive",
                               side_effect=lambda _pid: alive.pop(0)), \
             mock.patch.object(jvm_daemon, "process_created",
                               side_effect=lambda _pid: identities.pop(0)), \
             mock.patch.object(jvm_daemon, "_exchange",
                               return_value={"code": 0}) as exchange:
            with mock.patch.object(jvm_daemon.time, "monotonic",
                                   side_effect=[0, 0, 11]):
                result = jvm_daemon.stop_idle()
        self.assertFalse(result["stopped"])
        self.assertIn("被其他进程复用", result["reason"])
        exchange.assert_called_once_with({"id": 0, "op": "stop"}, 1234, timeout=10)

    def test_stop_idle_refuses_changed_operation_owner(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True,
                 "current_operation": {"operation_id": "new-op", "job_id": "job-2"}}
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_exchange") as exchange:
            result = jvm_daemon.stop_idle(
                owner_inactive=True, expected_operation_id="old-op")
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

        info = {"pid": 42, "port": 1234}
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True, "current_operation": None,
                 "pid": 42, "port": 1234}
        with mock.patch.object(jvm_daemon, "status", return_value=state), \
             mock.patch.object(jvm_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_daemon, "_pid_alive",
                               return_value=False), \
             mock.patch.object(jvm_daemon, "process_created",
                               return_value="daemon-start"), \
             mock.patch.object(jvm_daemon, "_PROC", None), \
             mock.patch.object(jvm_daemon, "_LOG", None), \
             mock.patch.object(jvm_daemon, "_exchange",
                               return_value={"code": 0}) as exchange:
            with mock.patch.object(jvm_daemon.time, "monotonic",
                                   side_effect=[0, 11]):
                result = jvm_daemon.stop_idle()
        self.assertTrue(result["stopped"], result)
        exchange.assert_called_once_with({"id": 0, "op": "stop"}, 1234, timeout=10)

    def test_ping_rejects_other_daemon_kind(self) -> None:
        """kind 不是 engine 的应答（别的程序、或合并前的旧 daemon）→「不是我们的」。"""
        with mock.patch.object(jvm_daemon, "_exchange",
                               return_value={"sig": "x", "kind": "validate"}):
            self.assertIsNone(jvm_daemon.ping(1234))

    def test_reset_for_tests_does_not_remove_user_info_file(self) -> None:
        with mock.patch.object(jvm_daemon, "_PROC", object()), \
             mock.patch.object(jvm_daemon, "_LOG", object()):
            jvm_daemon.reset_for_tests()
        self.assertFalse(jvm_daemon._PROC)
        self.assertIsNone(jvm_daemon._LOG)


class PrepareTests(unittest.TestCase):
    """批量冷启动准备（``prepare``）的分类语义。

    核心钉子：**busy 一档绝不触碰 ensure / start / _kill_proc / _stop_port**——
    端口被占但没应答可能只是 daemon 在串行忙别人的 op，杀掉会伤及在跑的请求；
    判「忙」= 缓存里那个端口还在 LISTEN 且属主进程活着，判「死」= 端口/进程都没了。
    扫描一律打桩（``_scan``）：真 netstat 的解析与窗口形状由 ``ScanTests`` 钉。
    """

    # dump 形状常量已随直起退场；lifecycle 测试不再需要完整 dump（ensure 无参）。
    _DUMP = {"sig": "s"}
    _INFO = {"pid": 1, "port": 9999, "sig": "s", "started_at": 1.0}
    #: 窗口里只有缓存那个端口（``_hint_port`` 要求提示端口在窗口内）
    _WINDOW = [9999]

    def setUp(self) -> None:
        jvm_daemon.reset_for_tests()
        win = mock.patch.object(jvm_daemon, "_daemon_window", lambda: list(self._WINDOW))
        win.start()
        self.addCleanup(win.stop)

    @staticmethod
    def _scan_result(got=None, occupied=None, free=9998, port=0, owner=0):
        return {"port": port, "got": got, "owner": owner,
                "occupied": dict(occupied or {}), "free": free}

    def test_prepare_reuses_hot_daemon_without_restart(self) -> None:
        """热 daemon 且签名匹配 → ready，不动任何进程（同签名不重启的钉子）。"""
        got = {"sig": "s", "kind": "engine", "pid": 1}
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(got=got, port=9999, owner=1)), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_daemon, "start") as start, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill, \
             mock.patch.object(jvm_daemon, "_stop_port") as stop:
            got_out = jvm_daemon.prepare()
        self.assertEqual(got_out["outcome"], "ready")
        self.assertEqual(got_out["info"]["port"], 9999)
        ensure.assert_not_called()
        start.assert_not_called()
        kill.assert_not_called()
        stop.assert_not_called()

    def test_prepare_restarts_stale_daemon_via_ensure(self) -> None:
        """ping 通但签名不符 → 交给 ensure（优雅停旧起新），outcome=started。"""
        got = {"sig": "old", "kind": "engine", "pid": 1}
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(got=got, port=9999, owner=1)), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="new"), \
             mock.patch.object(jvm_daemon, "ensure",
                               return_value={"pid": 2, "port": 2, "sig": "new"}) as ensure, \
             mock.patch.object(jvm_daemon, "start") as start:
            got_out = jvm_daemon.prepare()
        self.assertEqual(got_out["outcome"], "started")
        self.assertEqual(got_out["info"]["sig"], "new")
        ensure.assert_called_once()
        start.assert_not_called()

    def test_ensure_stops_mismatched_daemon_before_start(self) -> None:
        """真 ensure 的钉子（prepare 现在承载批量）：签名不符必须**先**优雅停、
        再 start——顺序反了会出现新旧两个 daemon 抢同一份浏览器 profile。"""
        calls = []
        stale = self._scan_result(got={"sig": "old", "kind": "engine", "pid": 1},
                                  port=9999, owner=1)
        empty = self._scan_result(free=4242)
        with tempfile.TemporaryDirectory(prefix="validate_daemon_") as tmp, \
             mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "info_path",
                               return_value=pathlib.Path(tmp) / "validate_daemon.json"), \
             mock.patch.object(jvm_daemon, "_scan", side_effect=[stale, empty]), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="new"), \
             mock.patch.object(jvm_daemon, "_stop_port",
                               side_effect=lambda port, pid=0:
                                   calls.append(("stop", port)) or
                                   {"stopped": True, "reason": ""}), \
             mock.patch.object(jvm_daemon, "start",
                               side_effect=lambda **kw:
                                   calls.append(("start",)) or {"pid": 3, "port": 3}):
            got = jvm_daemon.ensure()
        self.assertEqual(got, {"pid": 3, "port": 3})
        self.assertEqual(calls, [("stop", 9999), ("start",)])

    def test_ensure_reports_the_daemon_words_when_the_old_engine_will_not_stop(self) -> None:
        """停不掉**就抛**，不"先起一个再说"，而且 daemon 的原话要一路带出来（AGENTS #4）。

        两个引擎会抢同一份浏览器 profile：后起的那个静默换临时 profile，cookie 全丢。
        """
        stale = self._scan_result(got={"sig": "old", "kind": "engine", "pid": 1},
                                  port=9999, owner=1)
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan", return_value=stale), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="new"), \
             mock.patch.object(jvm_daemon, "_stop_port",
                               return_value={"stopped": False,
                                             "reason": "daemon 拒绝停止（code=2：缺参数 op）"}), \
             mock.patch.object(jvm_daemon, "start") as start:
            with self.assertRaises(jvm_daemon.DaemonError) as ctx:
                jvm_daemon.ensure()
        start.assert_not_called()
        self.assertIn("code=2", str(ctx.exception))
        self.assertIn("缺参数 op", str(ctx.exception))

    def test_prepare_reports_busy_without_killing(self) -> None:
        """等待窗口内一直没空闲 → 不杀、不启（本任务的核心钉子）。"""
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 1})), \
             mock.patch.object(jvm_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_daemon, "start") as start, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill, \
             mock.patch.object(jvm_daemon, "_stop_port") as stop:
            got = jvm_daemon.prepare(busy_wait_sec=0)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("不杀", got["reason"])
        self.assertIn("pid=1", got["reason"])
        ensure.assert_not_called()
        start.assert_not_called()
        kill.assert_not_called()
        stop.assert_not_called()

    def test_prepare_busy_reason_includes_current_operation_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validate_busy_owner_") as tmp:
            marker = pathlib.Path(tmp) / "validate_daemon.current.json"
            marker.write_text(json.dumps({
                "operation_id": "op-1", "job_id": "job-8", "chunk": "4/7",
                "source_name": "example source", "started_at": time.time() - 3,
            }), encoding="utf-8")
            with mock.patch.object(jvm_daemon, "current_operation_path",
                                   return_value=marker), \
                 mock.patch.object(jvm_daemon, "_read_info",
                                   return_value=dict(self._INFO)), \
                 mock.patch.object(jvm_daemon, "_scan",
                                   return_value=self._scan_result(occupied={9999: 1})), \
                 mock.patch.object(jvm_daemon, "ping", return_value=None), \
                 mock.patch.object(jvm_daemon, "_pid_alive", return_value=True):
                got = jvm_daemon.prepare(busy_wait_sec=0)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("job=job-8", got["reason"])
        self.assertIn("chunk=4/7", got["reason"])
        self.assertIn("source=example source", got["reason"])

        """忙是瞬时的：等待窗口内 ping 恢复应答 → ready，杀掉重启才是真损失。"""
        answers = [None, None, {"sig": "s", "kind": "engine"}]
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 1})), \
             mock.patch.object(jvm_daemon, "ping",
                               side_effect=lambda *a, **kw: answers.pop(0)), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill:
            got = jvm_daemon.prepare(busy_wait_sec=5)
        self.assertEqual(got["outcome"], "ready")
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_prepare_does_not_wait_for_dead_pid(self) -> None:
        """进程已消失时不该耗满等待窗口：立即判死并交给 ensure 重启。"""
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 1})), \
             mock.patch.object(jvm_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_daemon, "_pid_alive",
                               side_effect=[True, False]), \
             mock.patch.object(jvm_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure:
            got = jvm_daemon.prepare(busy_wait_sec=30)
        self.assertEqual(got["outcome"], "started")
        ensure.assert_called_once()

    def test_prepare_reports_wait_progress_while_busy(self) -> None:
        """忙等待期间按间隔回调上报——用户要看得见「在等引擎」，而不是进度不动。"""
        seen: list = []
        with mock.patch.object(jvm_daemon, "_BUSY_NOTICE_INTERVAL", 0.2), \
             mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 1})), \
             mock.patch.object(jvm_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon, "ensure") as ensure:
            got = jvm_daemon.prepare(busy_wait_sec=0.7,
                                              on_wait=seen.append)
        self.assertEqual(got["outcome"], "busy")
        self.assertTrue(seen, "等待期间必须上报进度")
        self.assertTrue(all(isinstance(v, float) for v in seen), seen)
        ensure.assert_not_called()

    def test_wait_callback_failure_does_not_break_prepare(self) -> None:
        """回调只管上报：它抛异常也不能让 prepare 违背「永不抛」（忙仍返回 busy）。"""
        def boom(_elapsed):
            raise RuntimeError("上报失败")

        with mock.patch.object(jvm_daemon, "_BUSY_NOTICE_INTERVAL", 0.1), \
             mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 1})), \
             mock.patch.object(jvm_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon, "ensure") as ensure:
            got = jvm_daemon.prepare(busy_wait_sec=0.4,
                                              on_wait=boom)
        self.assertEqual(got["outcome"], "busy")
        ensure.assert_not_called()

    def test_resolve_raises_on_busy_without_killing(self) -> None:
        """单条路径不许靠杀进程解决「忙」：resolve 只把原因抛出去，进程一个都不动。"""
        with mock.patch.object(jvm_daemon, "prepare",
                               return_value={"outcome": "busy",
                                             "reason": "进程还在但未空闲，本批不准备也不杀"}), \
             mock.patch.object(jvm_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill:
            with self.assertRaises(jvm_daemon.DaemonError) as ctx:
                jvm_daemon.resolve(busy_wait_sec=0)
        self.assertIn("未空闲", str(ctx.exception))
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_run_does_not_kill_a_busy_daemon(self) -> None:
        """``run`` 曾把忙当成死（ensure 杀进程重启）；现在必须抛原因、不碰进程。"""
        path = pathlib.Path(".tmp_validate_run_args.properties")
        path.write_text("file=C:/a.json\nout=C:/a.jsonl\nkeyword=我\ntimeout=5\nconcurrency=1\ndepth=search\n",
                        encoding="utf-8")
        try:
            with mock.patch.object(jvm_daemon, "prepare",
                                   return_value={"outcome": "busy",
                                                 "reason": "daemon 忙"}), \
                 mock.patch.object(jvm_daemon, "ensure") as ensure, \
                 mock.patch.object(jvm_daemon, "_kill_proc") as kill, \
                 mock.patch.object(jvm_daemon, "request") as request:
                with self.assertRaises(jvm_daemon.DaemonError):
                    jvm_daemon.run_validate(str(path), busy_wait_sec=0)
        finally:
            path.unlink(missing_ok=True)
        ensure.assert_not_called()
        kill.assert_not_called()
        request.assert_not_called()

    def test_concurrent_ensure_starts_only_one_daemon(self) -> None:
        """两个调用方同时 ensure 只能起一个 daemon：否则多占一个端口和一份堆，
        而且 ``_PROC`` 只记住后一个，前一个成孤儿。"""
        state = {"info": {}}
        started = []

        def fake_scan():
            if state["info"]:
                return self._scan_result(got={"sig": "s", "kind": "engine", "pid": 2},
                                         port=9999, owner=2)
            return self._scan_result(free=9998)

        def fake_start(idle_sec=1800, boot_timeout=180, **kw):
            time.sleep(0.2)          # 放大竞态窗口：没有锁时两个线程都会进来
            state["info"] = {"pid": 2, "port": 9999, "sig": "s"}
            started.append(1)
            return dict(state["info"])

        with mock.patch.object(jvm_daemon, "_read_info",
                               side_effect=lambda: dict(state["info"])), \
             mock.patch.object(jvm_daemon, "_scan", side_effect=fake_scan), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_daemon, "_kill_proc"), \
             mock.patch.object(jvm_daemon, "start",
                               side_effect=fake_start):
            threads = [threading.Thread(
                target=lambda: jvm_daemon.ensure())
                for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(1, len(started), "并发 ensure 只能启动一次")

    def test_prepare_reports_busy_for_a_held_port_without_an_answer(self) -> None:
        """端口被占但没应答我们 → busy：不杀不启，理由里带 netstat 看到的属主 pid。

        缓存从 2026-10-21 起只是**提示**（判「忙」看的是那个端口还在不在监听、属主进程
        还活不活），所以缓存里有没有 pid 都不影响这一档。
        """
        with mock.patch.object(jvm_daemon, "_read_info", return_value={"port": 9999}), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(occupied={9999: 24})), \
             mock.patch.object(jvm_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill:
            got = jvm_daemon.prepare(busy_wait_sec=0)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("pid=24", got["reason"])
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_prepare_restarts_when_the_window_is_empty(self) -> None:
        """窗口里没人监听 = 引擎真没了（缓存是残骸）→ 交给 ensure 起一个，不杀。"""
        with mock.patch.object(jvm_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_daemon, "_scan", return_value=self._scan_result()), \
             mock.patch.object(jvm_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure, \
             mock.patch.object(jvm_daemon, "_kill_proc") as kill:
            got = jvm_daemon.prepare()
        self.assertEqual(got["outcome"], "started")
        ensure.assert_called_once()
        kill.assert_not_called()

    def test_prepare_starts_when_no_info(self) -> None:
        """缓存缺失 → 窗口里又什么都没有 → 交给 ensure 启动（连 ping 都不做）。"""
        with mock.patch.object(jvm_daemon, "_read_info", return_value={}), \
             mock.patch.object(jvm_daemon, "_scan", return_value=self._scan_result()), \
             mock.patch.object(jvm_daemon, "ping") as ping, \
             mock.patch.object(jvm_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure:
            got = jvm_daemon.prepare()
        self.assertEqual(got["outcome"], "started")
        ping.assert_not_called()
        ensure.assert_called_once()

    def test_prepare_failure_keeps_reason_and_does_not_raise(self) -> None:
        """启动失败 → outcome=failed 且原因逐字保留；prepare 对调用方永不抛。"""
        with mock.patch.object(jvm_daemon, "_read_info", return_value={}), \
             mock.patch.object(jvm_daemon, "_scan", return_value=self._scan_result()), \
             mock.patch.object(jvm_daemon, "ensure",
                               side_effect=jvm_daemon.DaemonError(
                                   "Validate daemon 180s 内没起来")):
            got = jvm_daemon.prepare()
        self.assertEqual(got["outcome"], "failed")
        self.assertEqual(got["reason"], "Validate daemon 180s 内没起来")

    def test_pid_alive_distinguishes_self_from_fabricated_dead(self) -> None:
        """``_pid_alive`` 用真进程钉住判死：自己活着，编造的 pid 不在。
        （判死不靠端口行为：Windows 对已关闭端口的 connect 抛超时而非拒绝。）"""
        self.assertTrue(jvm_daemon._pid_alive(os.getpid()))
        self.assertFalse(jvm_daemon._pid_alive(4194304))
        self.assertFalse(jvm_daemon._pid_alive(0))


class BootWatchdogTests(unittest.TestCase):
    """boot 看门狗（``_hard_kill_boot``）：杀完必须验端口释放，残局不许静默。

    背景：2026-10-19 实测 Gradle daemon 脱离 cmd 进程树，``taskkill /T`` 杀不到，
    一次 boot 失败的僵尸 Gradle 挂了 50 分钟没人知道（日志里 BUILD FAILED in 50m）。
    """

    def test_hard_kill_reports_residue_when_port_still_held(self) -> None:
        """杀完端口仍被占 → 诊断非空（残局可见的钉子）。"""
        with mock.patch.object(jvm_daemon, "_port_owner_pid", return_value=4242), \
             mock.patch.object(jvm_daemon, "_kill_proc", return_value=True):
            residue = jvm_daemon._hard_kill_boot(49975, mock.Mock(pid=111))
        self.assertIn("仍被", residue)
        self.assertIn("4242", residue)

    def test_hard_kill_returns_empty_when_port_freed(self) -> None:
        """端口已释放 → 清场成功，诊断为空。"""
        with mock.patch.object(jvm_daemon, "_port_owner_pid", return_value=0), \
             mock.patch.object(jvm_daemon, "_kill_proc", return_value=True):
            residue = jvm_daemon._hard_kill_boot(49975, mock.Mock(pid=111))
        self.assertEqual(residue, "")

    def test_hard_kill_kills_port_owner_not_cmd_pid(self) -> None:
        """端口 owner 与 cmd pid 不同（正是 Gradle daemon 的情形）→ 补杀 owner。"""
        killed = []
        with mock.patch.object(jvm_daemon, "_port_owner_pid", return_value=999), \
             mock.patch.object(jvm_daemon, "_kill_proc", return_value=True), \
             mock.patch.object(jvm_daemon.subprocess, "run",
                               side_effect=lambda cmd, **kw: killed.append(cmd)) , \
             mock.patch.object(jvm_daemon.time, "sleep"):
            # 死循环防护：owner 恒为 999 且杀不掉 → 补杀一次后 time 到点退出；
            # 用真实时间窗口兜底（_KILL_VERIFY_SEC），这里靠 subprocess.run 侧效
            # 让第一次补杀后仍返回 999 —— 允许，验证点只是「999 被打过 taskkill」
            residue = jvm_daemon._hard_kill_boot(49975, mock.Mock(pid=111))
        self.assertTrue(any("999" in " ".join(c) for c in killed), killed)
        self.assertIn("仍被", residue)

    def test_hard_kill_without_proc_still_verifies_port(self) -> None:
        """proc 缺失（进程已自行退出但端口被占）→ 仍按端口补杀并验证。"""
        killed = []
        with mock.patch.object(jvm_daemon, "_port_owner_pid",
                               side_effect=[999, 999, 0]), \
             mock.patch.object(jvm_daemon.subprocess, "run",
                               side_effect=lambda cmd, **kw: killed.append(cmd)), \
             mock.patch.object(jvm_daemon.time, "sleep"):
            residue = jvm_daemon._hard_kill_boot(49975, None)
        self.assertTrue(any("999" in " ".join(c) for c in killed), killed)
        self.assertEqual(residue, "")

    def test_hard_kill_reports_when_the_listener_table_is_unreadable(self) -> None:
        """监听表读不到时不许说「清干净了」：那是**没法确认**，不是已释放（AGENTS #12）。"""
        with mock.patch.object(jvm_daemon, "_port_owner_pid", return_value=-1), \
             mock.patch.object(jvm_daemon, "_kill_proc", return_value=True):
            residue = jvm_daemon._hard_kill_boot(49975, mock.Mock(pid=111))
        self.assertIn("读不到", residue)


class DaemonIdentityTests(unittest.TestCase):
    """身份只认**端口窗口里的应答**（`identity_ok`：kind + sig）。

    背景（2026-10-20 实测）：缓存里的 pid 记的是**我们 spawn 的包装进程**（`cmd /c
    legado-gradle.bat …`），不是 daemon 的 JVM。拿它当身份 → 判据恒假 → 每次都冷启一个
    daemon（`rule_try` 每次 ~28s），而且 `_stop_port` 也永远停不掉旧的。缓存从 2026-10-21
    起只是缓存：`ensure` 不再往它写，「就地自愈」那条路整个不存在。
    """

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="daemon_identity_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        jvm_daemon.reset_for_tests()
        for name, val in (("log_path", lambda: self.tmp / "daemon.log"),
                          ("_daemon_window", lambda: [1234])):
            p = mock.patch.object(jvm_daemon, name, val)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _scan_result(**kw):
        base = {"port": 0, "got": None, "owner": 0, "occupied": {}, "free": 1234}
        base.update(kw)
        return base

    def test_start_records_the_pid_the_daemon_reports(self) -> None:
        proc = mock.Mock()
        proc.pid = 4242                      # cmd / Gradle 包装进程
        proc.poll.return_value = None
        written: dict = {}
        try:
            with mock.patch.object(jvm_daemon, "_read_info", return_value={}), \
                 mock.patch.object(jvm_daemon, "_scan",
                                   return_value=self._scan_result(free=1234)), \
                 mock.patch.object(jvm_daemon, "source_sig", return_value="s"), \
                 mock.patch.object(jvm_daemon, "ping",
                                   return_value={"kind": "engine", "sig": "s", "pid": 777}), \
                 mock.patch.object(jvm_daemon, "_write_info",
                                   side_effect=lambda info: written.update(info)), \
                 mock.patch.object(jvm_daemon.subprocess, "Popen", return_value=proc), \
                 mock.patch("core.jvm_env.readiness",
                            return_value={"ok": True, "checks": []}), \
                 mock.patch("core.jvm_env.process_environment", return_value={}):
                info = jvm_daemon.start()
        finally:
            jvm_daemon._PROC = None
            handle = jvm_daemon._LOG
            jvm_daemon._LOG = None
            if handle is not None:
                handle.close()          # 不关掉句柄，Windows 上删不掉这个临时日志
        self.assertEqual(info["pid"], 777, "记 daemon 自报的 pid，不是包装进程的 4242")
        self.assertEqual(written["pid"], 777)
        self.assertEqual(info["port"], 1234)

    def test_ensure_reuses_by_answer_and_ignores_a_stale_record_pid(self) -> None:
        """缓存里是旧 pid（包装进程）也要**按应答复用、不重启、不改写缓存**。

        旧版这里是「就地自愈」：把应答的 pid 写回缓存。身份不再读缓存之后，那条写路径
        整个不存在了——缓存错了不影响复用，`ensure` 也就不该有副作用（GET 状态更不该写盘）。
        """
        legacy = {"pid": 4242, "port": 1234, "sig": "s", "idle_sec": 1800}
        with mock.patch.object(jvm_daemon, "_read_info", return_value=dict(legacy)), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(
                                   port=1234, owner=777,
                                   got={"kind": "engine", "sig": "s", "pid": 777})), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_daemon, "_write_info") as write_info, \
             mock.patch.object(jvm_daemon, "start") as start:
            info = jvm_daemon.ensure()
        start.assert_not_called()
        write_info.assert_not_called()
        self.assertEqual(info["pid"], 777, "应答优先：缓存里的 4242 不参与")
        self.assertEqual(info["port"], 1234)

    def test_stop_reports_false_when_the_process_outlives_the_port(self) -> None:
        """端口静了但进程还在 → **报 False，且保留缓存**。

        「端口静 ≠ 进程没了」：优雅退出的收尾（关 server、收浏览器）可能还没走完。
        2026-10-21 实测踩到过——端口静了、JVM 还挂着，而当时报的是「已停：True」。
        """
        answers = [{"kind": "engine", "sig": "s", "pid": 777}, None]
        with mock.patch.object(jvm_daemon, "ping",
                               side_effect=lambda *a, **kw: answers.pop(0)), \
             mock.patch.object(jvm_daemon, "_exchange", return_value={"code": 0}), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            result = jvm_daemon._stop_port(1234, 777)
        self.assertFalse(result["stopped"], "进程还在就不算停好了")
        self.assertIn("仍没收尾", result["reason"])

        info_file = self.tmp / "legado-test-stop.info"
        info_file.write_text(json.dumps(
            {"pid": 777, "port": 1234, "sig": "s"}), encoding="utf-8", newline="\n")
        answers = [{"kind": "engine", "sig": "s", "pid": 777}, None]
        with mock.patch.object(jvm_daemon, "info_path", return_value=info_file), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value=self._scan_result(
                                   port=1234, owner=777,
                                   got={"kind": "engine", "sig": "s", "pid": 777})), \
             mock.patch.object(jvm_daemon, "ping",
                               side_effect=lambda *a, **kw: answers and answers.pop(0)), \
             mock.patch.object(jvm_daemon, "_exchange", return_value={"code": 0}), \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None), \
             mock.patch.object(jvm_daemon, "_PROC", None), \
             mock.patch.object(jvm_daemon, "_kill_proc"):
            self.assertFalse(jvm_daemon.stop(), "报 False 才对")
        self.assertTrue(info_file.exists(), "没停干净就不许清缓存（线索要留住）")
        self.assertIn("停止未完成", jvm_daemon.log_path().read_text(encoding="utf-8"),
                      "stop() 只回 bool，原因必须落到日志里（AGENTS #4）")

    def test_stop_reports_true_only_after_the_process_is_gone(self) -> None:
        """端口静 + 进程也消失 → stopped=True（现状保持，防回归）。"""
        answers = [{"kind": "engine", "sig": "s", "pid": 777}, None]
        gone = [False]                    # 第一次问「还在吗」就已经退出了
        with mock.patch.object(jvm_daemon, "ping",
                               side_effect=lambda *a, **kw: answers.pop(0)), \
             mock.patch.object(jvm_daemon, "_exchange", return_value={"code": 0}), \
             mock.patch.object(jvm_daemon, "_pid_alive",
                               side_effect=lambda _pid: gone.pop(0) if gone else False), \
             mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            result = jvm_daemon._stop_port(1234, 777)
        self.assertTrue(result["stopped"])

    def test_stop_port_stops_the_process_answering_on_that_port(self) -> None:
        """破坏性动作要更严：`_stop_port` 停的是**此刻应答的那个 pid**。

        调用方传应答里的 pid（`ensure` 的换签名分支、`stop()` 都从扫描拿）；
        传一个对不上的 pid 时必须**一个字节都不发**——那是「扫描之后应答者变了」，
        只能重扫，不能瞎杀（pid 会被系统复用）。
        """
        # 传应答里的 pid → 发 op=stop，并按端口静默确认
        answers = [{"kind": "engine", "sig": "s", "pid": 777}, None]
        with mock.patch.object(jvm_daemon, "ping",
                               side_effect=lambda *a, **kw: answers.pop(0)), \
             mock.patch.object(jvm_daemon, "_exchange",
                               return_value={"code": 0}) as exchange, \
             mock.patch.object(jvm_daemon, "_pid_alive", return_value=False), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            ok = jvm_daemon._stop_port(1234, 777)
        self.assertTrue(ok["stopped"])
        exchange.assert_called_once_with({"id": 0, "op": "stop"}, 1234, timeout=20)

        # 传一个与应答不符的 pid → 拒绝，且不发任何请求
        with mock.patch.object(jvm_daemon, "ping",
                               return_value={"kind": "engine", "sig": "s", "pid": 777}), \
             mock.patch.object(jvm_daemon, "_exchange") as exchange2, \
             mock.patch.object(jvm_daemon, "_PROC", None):
            refused = jvm_daemon._stop_port(1234, 4242)
        self.assertFalse(refused["stopped"])
        self.assertIn("拒绝误停", refused["reason"])
        exchange2.assert_not_called()

    def test_stop_port_keeps_the_daemon_words_when_it_refuses(self) -> None:
        """`op=stop` 应答 code≠0（旧类回「缺参数」）→ 不是「已停」，并把原话带出来。

        旧版只看 `_exchange` 没抛异常就当优雅停成功，daemon 的原话在这里被丢掉——
        于是一条「缺参数」被当成已停，而进程还在跑（2026-10-20 实测踩过）。
        """
        answer = {"kind": "engine", "sig": "s", "pid": 777}
        with mock.patch.object(jvm_daemon, "ping", return_value=dict(answer)), \
             mock.patch.object(jvm_daemon, "_exchange",
                               return_value={"code": 2, "error": "缺参数: op"}), \
             mock.patch.object(jvm_daemon, "process_created", return_value="inst-1"), \
             mock.patch.object(jvm_daemon.os, "kill"), \
             mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            result = jvm_daemon._stop_port(1234, 777)
        self.assertFalse(result["stopped"])
        self.assertIn("code=2", result["reason"])
        self.assertIn("缺参数: op", result["reason"])

    def test_stop_port_treats_a_malformed_answer_as_not_stopped(self) -> None:
        """应答 `code` 不是数字（形状漂了）→ 没停成，不许当成功，也不许抛出去。

        `stop()` 的签名是 bool 且**不许抛**（`backend/jobs/jvm_exec.py` 的输出停滞
        看门狗裸调它）；对 `code=None` 直接 `int()` 会抛 TypeError。
        """
        answer = {"kind": "engine", "sig": "s", "pid": 777}
        with mock.patch.object(jvm_daemon, "ping", return_value=dict(answer)), \
             mock.patch.object(jvm_daemon, "_exchange", return_value={"code": None}), \
             mock.patch.object(jvm_daemon, "process_created", return_value="inst-1"), \
             mock.patch.object(jvm_daemon.os, "kill"), \
             mock.patch.object(jvm_daemon.time, "sleep", lambda _s: None), \
             mock.patch.object(jvm_daemon, "_PROC", None):
            result = jvm_daemon._stop_port(1234, 777)
        self.assertFalse(result["stopped"])
        self.assertIn("code=None", result["reason"])

    def test_status_survives_a_non_numeric_pid_in_the_answer(self) -> None:
        """应答里的 pid 拿不出来（形状漂了）也不许抛：pid 用 netstat 属主兜底。"""
        scan = {"port": 43440, "got": {"kind": "engine", "sig": "s", "pid": "abc"},
                "owner": 111, "occupied": {}, "free": 0, "blind": False}
        with mock.patch.object(jvm_daemon, "_scan", return_value=scan), \
             mock.patch.object(jvm_daemon, "_read_info", return_value={}), \
             mock.patch.object(jvm_daemon, "source_sig", return_value="s"):
            state = jvm_daemon.status()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["pid"], 111)


class ScanTests(unittest.TestCase):
    """端口窗口与扫描：**身份的唯一入口**。

    窗口是推导出来的（base = 41000 + slot*20，slot 来自 data 目录），整段压在 Windows
    动态端口范围（49152–65535，出站连接会自动占用）下面；槽位按窗口对齐 ⇒ 不同槽的
    窗口不相交（同槽会共用窗口，见 `_daemon_base_port`）。真 netstat/lsof 的解析用文本
    钉住；两条命令都读不到的「盲区」单独钉——读不到 ≠ 窗口里没有（AGENTS #12）。
    """

    def test_window_is_derived_and_stays_out_of_the_ephemeral_range(self) -> None:
        with mock.patch("core.settings_store.load",
                        return_value={"jvm": {"daemon_port": 0}}):
            first = jvm_daemon._daemon_window()
            with mock.patch.object(jvm_daemon, "data_dir",
                                   return_value=pathlib.Path("D:/other-checkout/data")):
                second = jvm_daemon._daemon_window()
        self.assertEqual(len(first), jvm_daemon.PORT_WINDOW)
        self.assertEqual(first[0] % jvm_daemon.PORT_WINDOW, 0)
        self.assertLess(first[-1], 49152, "整段必须在动态端口范围下面")
        self.assertNotEqual(first[0], second[0], "不同 data 目录落不同槽位")
        self.assertTrue(set(first).isdisjoint(second), "不同槽的窗口不相交")
        with self.subTest("显式端口直接当起点"):
            with mock.patch("core.settings_store.load",
                            return_value={"jvm": {"daemon_port": 45001}}):
                self.assertEqual(jvm_daemon._daemon_window()[0], 45001)
        with self.subTest("起点太高时窗口整体往回挪，不许出现不存在的端口"):
            with mock.patch("core.settings_store.load",
                            return_value={"jvm": {"daemon_port": 65530}}):
                high = jvm_daemon._daemon_window()
            self.assertLessEqual(high[-1], 65535)

    def test_scan_finds_our_engine_then_the_first_free_port(self) -> None:
        engine = {"kind": "engine", "sig": "s", "pid": 777}

        def fake_ping(port, timeout=0.4):
            return dict(engine) if port == 43441 else None

        with mock.patch.object(jvm_daemon, "_daemon_window",
                               lambda: [43440, 43441, 43442]), \
             mock.patch.object(jvm_daemon, "_listen_map",
                               return_value={43440: 111, 43441: 222}), \
             mock.patch.object(jvm_daemon, "ping", side_effect=fake_ping):
            found = jvm_daemon._scan()
        self.assertEqual(found["port"], 43441)
        self.assertEqual(found["got"]["pid"], 777)
        self.assertEqual(found["owner"], 222)
        self.assertEqual(found["occupied"], {43440: 111}, "陌生占位要记账（解释为什么换端口）")
        self.assertEqual(found["free"], 0, "找到引擎时不再算空闲端口")
        self.assertFalse(found["blind"])

        with mock.patch.object(jvm_daemon, "_daemon_window",
                               lambda: [43440, 43441, 43442]), \
             mock.patch.object(jvm_daemon, "_listen_map", return_value={43442: 333}), \
             mock.patch.object(jvm_daemon, "ping", return_value=None):
            empty = jvm_daemon._scan()
        self.assertIsNone(empty["got"])
        self.assertEqual(empty["free"], 43440)
        self.assertEqual(empty["occupied"], {43442: 333})

    def test_parsers_read_both_platform_formats(self) -> None:
        """只认 LISTENING 行；IPv6 的 `[::1]:port` 也要解析出端口（旧实现 endswith 会漏）。"""
        netstat = (
            "  TCP    127.0.0.1:43440        0.0.0.0:0              LISTENING       111\n"
            "  TCP    [::1]:43441            [::]:0                 LISTENING       222\n"
            "  TCP    127.0.0.1:50000        127.0.0.1:443          ESTABLISHED     333\n"
            "  UDP    127.0.0.1:1900         *:*                                    444\n"
        )
        with self.subTest("netstat"):
            self.assertEqual(jvm_daemon._parse_netstat_listeners(netstat),
                             {43440: 111, 43441: 222})
        with self.subTest("lsof"):
            self.assertEqual(
                jvm_daemon._parse_lsof_listeners("p111\nn*:43440\np222\nn[::1]:43441\n"),
                {43440: 111, 43441: 222})

    def test_blind_scan_never_pretends_the_window_is_empty(self) -> None:
        """监听表读不到 → 只 ping 缓存提示的那个端口，并且**拒绝起新引擎**。

        把「读不到」当成「没有」，会让 `status` 报 stopped、让 `ensure` 去冷启一个撞上
        活引擎的端口——两处都不报错，只是行为莫名其妙（AGENTS #12）。
        """
        with mock.patch.object(jvm_daemon, "_daemon_window", lambda: [43440]), \
             mock.patch.object(jvm_daemon, "_read_info", return_value={"port": 43440}), \
             mock.patch.object(jvm_daemon, "_listen_map", return_value=None), \
             mock.patch.object(jvm_daemon, "ping",
                               return_value={"kind": "engine", "sig": "s", "pid": 777}):
            scan = jvm_daemon._scan()
        self.assertTrue(scan["blind"])
        self.assertEqual(scan["port"], 43440)
        self.assertEqual(scan["free"], 0, "读不到就不许猜空闲端口")

        with mock.patch.object(jvm_daemon, "_daemon_window", lambda: [43440]), \
             mock.patch.object(jvm_daemon, "_scan",
                               return_value={"port": 0, "got": None, "owner": 0,
                                             "occupied": {}, "free": 0, "blind": True}), \
             mock.patch("core.jvm_env.readiness",
                        return_value={"ok": True, "checks": []}), \
             mock.patch("core.jvm_env.process_environment", return_value={}), \
             mock.patch.object(jvm_daemon.subprocess, "Popen") as popen:
            with self.assertRaises(jvm_daemon.DaemonError) as ctx:
                jvm_daemon.start()
        popen.assert_not_called()
        self.assertIn("监听表读不到", str(ctx.exception))

    def test_ensure_refuses_to_boot_on_a_blind_scan(self) -> None:
        """监听表读不到 → 不起新引擎（盲起会撞上那个其实在监听的引擎），原因说到眼前。"""
        blind = {"port": 0, "got": None, "owner": 0, "occupied": {},
                 "free": 0, "blind": True}
        with mock.patch.object(jvm_daemon, "_scan", return_value=blind), \
             mock.patch.object(jvm_daemon, "_daemon_window", lambda: [43440]), \
             mock.patch.object(jvm_daemon, "start") as start:
            with self.assertRaises(jvm_daemon.DaemonError) as ctx:
                jvm_daemon.ensure()
        start.assert_not_called()
        self.assertIn("监听表读不到", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

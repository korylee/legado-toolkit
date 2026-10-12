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

from core import jvm_validate_daemon


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
        with mock.patch.object(jvm_validate_daemon, "_exchange",
                               side_effect=jvm_validate_daemon.ValidateDaemonError(
                                   "连不上 Validate daemon(port=58966): reset")), \
             mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value={"pid": 52096, "port": 58966}), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=False), \
             mock.patch.object(jvm_validate_daemon, "_read_current_operation",
                               return_value={"job_id": "job-1", "chunk": "1/2", "pid": 4000}), \
             mock.patch.object(jvm_validate_daemon, "_log_tail", return_value="last daemon line"), \
             mock.patch.object(jvm_validate_daemon, "_PROC", None):
            with self.assertRaises(jvm_validate_daemon.ValidateDaemonError) as caught:
                jvm_validate_daemon.request({
                    "file": "source.json", "keyword": "x", "concurrency": 1,
                    "timeout": 10, "depth": "search", "owner_job_id": "job-1",
                    "owner_chunk": "1/2",
                }, 58966, 10)
        message = str(caught.exception)
        self.assertIn("daemon pid=52096 port=58966 alive=False", message)
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

            with mock.patch.object(jvm_validate_daemon, "current_operation_path",
                                   return_value=marker), \
                 mock.patch.object(jvm_validate_daemon, "params_from_args",
                                   return_value={"timeout": 25}), \
                 mock.patch.object(jvm_validate_daemon, "resolve",
                                   return_value={"port": 1234}), \
                 mock.patch.object(jvm_validate_daemon, "request", side_effect=fake_request):
                result = jvm_validate_daemon.run(
                    {}, "args.properties", socket_timeout=90, owner=owner)
            self.assertEqual(result["code"], 0)
            self.assertFalse(marker.exists())

    def test_current_operation_records_os_process_identity(self) -> None:
        marker = pathlib.Path("validate_identity_test_current.json")
        with mock.patch.object(jvm_validate_daemon, "current_operation_path",
                               return_value=marker), \
             mock.patch.object(jvm_validate_daemon, "process_created",
                               return_value="process-start-1"), \
             mock.patch.object(jvm_validate_daemon, "uuid4",
                               return_value=mock.Mock(hex="operation-1")), \
             mock.patch.object(pathlib.Path, "write_text") as write_text:
            operation_id = jvm_validate_daemon._begin_current_operation(
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
            with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
                 mock.patch.object(jvm_validate_daemon, "_read_info", return_value=info), \
                 mock.patch.object(jvm_validate_daemon, "info_path", return_value=info_file), \
                 mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=False), \
                 mock.patch.object(jvm_validate_daemon, "process_created") as process_created:
                result = jvm_validate_daemon.stop_idle()
                self.assertFalse(info_file.exists())
        self.assertTrue(result["stopped"], result)
        self.assertTrue(result["already_stopped"])
        process_created.assert_not_called()

    def test_stop_idle_refuses_unresponsive_or_unverified_process(self) -> None:
        state = {"process_alive": True, "responsive": False,
                 "identity_verified": False, "current_operation": None}
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_exchange") as exchange:
            result = jvm_validate_daemon.stop_idle()
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

    def test_stop_idle_refuses_active_operation_owner(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True,
                 "current_operation": {"operation_id": "op-1", "job_id": "job-1"}}
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_exchange") as exchange:
            result = jvm_validate_daemon.stop_idle(owner_inactive=False)
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

    def test_stop_idle_rejects_daemon_pid_instance_change(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True, "current_operation": None,
                 "pid": 42, "port": 1234}
        info = {"pid": 42, "port": 1234}
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_validate_daemon, "process_created",
                               side_effect=["old-instance", "new-instance"]), \
             mock.patch.object(jvm_validate_daemon, "_exchange") as exchange:
            result = jvm_validate_daemon.stop_idle()
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
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive",
                               side_effect=lambda _pid: alive.pop(0)), \
             mock.patch.object(jvm_validate_daemon, "process_created",
                               side_effect=lambda _pid: identities.pop(0)), \
             mock.patch.object(jvm_validate_daemon, "_exchange",
                               return_value={"code": 0}) as exchange:
            with mock.patch.object(jvm_validate_daemon.time, "monotonic",
                                   side_effect=[0, 0, 11]):
                result = jvm_validate_daemon.stop_idle()
        self.assertFalse(result["stopped"])
        self.assertIn("被其他进程复用", result["reason"])
        exchange.assert_called_once_with({"id": 0, "op": "stop"}, 1234, timeout=10)

    def test_stop_idle_refuses_changed_operation_owner(self) -> None:
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True,
                 "current_operation": {"operation_id": "new-op", "job_id": "job-2"}}
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_exchange") as exchange:
            result = jvm_validate_daemon.stop_idle(
                owner_inactive=True, expected_operation_id="old-op")
        self.assertFalse(result["stopped"])
        exchange.assert_not_called()

        info = {"pid": 42, "port": 1234}
        state = {"process_alive": True, "responsive": True,
                 "identity_verified": True, "current_operation": None,
                 "pid": 42, "port": 1234}
        with mock.patch.object(jvm_validate_daemon, "status", return_value=state), \
             mock.patch.object(jvm_validate_daemon, "_read_info", return_value=info), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive",
                               return_value=False), \
             mock.patch.object(jvm_validate_daemon, "process_created",
                               return_value="daemon-start"), \
             mock.patch.object(jvm_validate_daemon, "_PROC", None), \
             mock.patch.object(jvm_validate_daemon, "_LOG", None), \
             mock.patch.object(jvm_validate_daemon, "_exchange",
                               return_value={"code": 0}) as exchange:
            with mock.patch.object(jvm_validate_daemon.time, "monotonic",
                                   side_effect=[0, 11]):
                result = jvm_validate_daemon.stop_idle()
        self.assertTrue(result["stopped"], result)
        exchange.assert_called_once_with({"id": 0, "op": "stop"}, 1234, timeout=10)

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
        """等待窗口内一直没空闲 → 不杀、不启（本任务的核心钉子）。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "start") as start, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill, \
             mock.patch.object(jvm_validate_daemon, "_stop_port") as stop:
            got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=0)
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
            with mock.patch.object(jvm_validate_daemon, "current_operation_path",
                                   return_value=marker), \
                 mock.patch.object(jvm_validate_daemon, "_read_info",
                                   return_value=dict(self._INFO)), \
                 mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
                 mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True):
                got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=0)
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("job=job-8", got["reason"])
        self.assertIn("chunk=4/7", got["reason"])
        self.assertIn("source=example source", got["reason"])

        """忙是瞬时的：等待窗口内 ping 恢复应答 → ready，杀掉重启才是真损失。"""
        answers = [None, None, {"sig": "s", "kind": "validate"}]
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping",
                               side_effect=lambda *a, **kw: answers.pop(0)), \
             mock.patch.object(jvm_validate_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill:
            got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=5)
        self.assertEqual(got["outcome"], "ready")
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_prepare_does_not_wait_for_dead_pid(self) -> None:
        """进程已消失时不该耗满等待窗口：立即判死并交给 ensure 重启。"""
        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive",
                               side_effect=[True, False]), \
             mock.patch.object(jvm_validate_daemon, "ensure",
                               return_value={"pid": 2, "port": 2}) as ensure:
            got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=30)
        self.assertEqual(got["outcome"], "started")
        ensure.assert_called_once()

    def test_prepare_reports_wait_progress_while_busy(self) -> None:
        """忙等待期间按间隔回调上报——用户要看得见「在等引擎」，而不是进度不动。"""
        seen: list = []
        with mock.patch.object(jvm_validate_daemon, "_BUSY_NOTICE_INTERVAL", 0.2), \
             mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure:
            got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=0.7,
                                              on_wait=seen.append)
        self.assertEqual(got["outcome"], "busy")
        self.assertTrue(seen, "等待期间必须上报进度")
        self.assertTrue(all(isinstance(v, float) for v in seen), seen)
        ensure.assert_not_called()

    def test_wait_callback_failure_does_not_break_prepare(self) -> None:
        """回调只管上报：它抛异常也不能让 prepare 违背「永不抛」（忙仍返回 busy）。"""
        def boom(_elapsed):
            raise RuntimeError("上报失败")

        with mock.patch.object(jvm_validate_daemon, "_BUSY_NOTICE_INTERVAL", 0.1), \
             mock.patch.object(jvm_validate_daemon, "_read_info",
                               return_value=dict(self._INFO)), \
             mock.patch.object(jvm_validate_daemon, "ping", return_value=None), \
             mock.patch.object(jvm_validate_daemon, "_pid_alive", return_value=True), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure:
            got = jvm_validate_daemon.prepare(self._DUMP, busy_wait_sec=0.4,
                                              on_wait=boom)
        self.assertEqual(got["outcome"], "busy")
        ensure.assert_not_called()

    def test_resolve_raises_on_busy_without_killing(self) -> None:
        """单条路径不许靠杀进程解决「忙」：resolve 只把原因抛出去，进程一个都不动。"""
        with mock.patch.object(jvm_validate_daemon, "prepare",
                               return_value={"outcome": "busy",
                                             "reason": "进程还在但未空闲，本批不准备也不杀"}), \
             mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
             mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill:
            with self.assertRaises(jvm_validate_daemon.ValidateDaemonError) as ctx:
                jvm_validate_daemon.resolve(self._DUMP, busy_wait_sec=0)
        self.assertIn("未空闲", str(ctx.exception))
        ensure.assert_not_called()
        kill.assert_not_called()

    def test_run_does_not_kill_a_busy_daemon(self) -> None:
        """``run`` 曾把忙当成死（ensure 杀进程重启）；现在必须抛原因、不碰进程。"""
        path = pathlib.Path(".tmp_validate_run_args.properties")
        path.write_text("file=C:/a.json\nout=C:/a.jsonl\nkeyword=我\ntimeout=5\nconcurrency=1\ndepth=search\n",
                        encoding="utf-8")
        try:
            with mock.patch.object(jvm_validate_daemon, "prepare",
                                   return_value={"outcome": "busy",
                                                 "reason": "daemon 忙"}), \
                 mock.patch.object(jvm_validate_daemon, "ensure") as ensure, \
                 mock.patch.object(jvm_validate_daemon, "_kill_proc") as kill, \
                 mock.patch.object(jvm_validate_daemon, "request") as request:
                with self.assertRaises(jvm_validate_daemon.ValidateDaemonError):
                    jvm_validate_daemon.run(
                        {"workingDir": "C:/repo"}, str(path), busy_wait_sec=0)
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

        def fake_start(dump, idle_sec=1800, boot_timeout=180):
            time.sleep(0.2)          # 放大竞态窗口：没有锁时两个线程都会进来
            state["info"] = {"pid": 2, "port": 2, "sig": "s"}
            started.append(1)
            return dict(state["info"])

        with mock.patch.object(jvm_validate_daemon, "_read_info",
                               side_effect=lambda: dict(state["info"])), \
             mock.patch.object(jvm_validate_daemon, "source_sig", return_value="s"), \
             mock.patch.object(jvm_validate_daemon, "ping",
                               return_value={"sig": "s"}), \
             mock.patch.object(jvm_validate_daemon, "_kill_proc"), \
             mock.patch.object(jvm_validate_daemon, "start",
                               side_effect=fake_start):
            threads = [threading.Thread(
                target=lambda: jvm_validate_daemon.ensure(self._DUMP))
                for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(1, len(started), "并发 ensure 只能启动一次")

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

"""Validate daemon 的 Python 协议层测试，不启动真实 JVM、不联网。"""

from __future__ import annotations

import json
import pathlib
import socket
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


if __name__ == "__main__":
    unittest.main()

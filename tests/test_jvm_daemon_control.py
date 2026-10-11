# -*- coding: utf-8 -*-
"""Validate daemon 状态展示与显式停止必须以 owner 和进程身份为准。"""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api import jvm as jvm_api


class _Store:
    def __init__(self, jobs=None, owner=None):
        self.jobs = list(jobs or [])
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_jobs(self, **_kwargs):
        return list(self.jobs)

    def get_job_summary(self, _job_id):
        return self.owner


class DaemonStatusTests(unittest.TestCase):
    def _status(self, daemon, jobs=None, owner=None):
        with mock.patch("core.jvm_validate_daemon.status", return_value=daemon), \
             mock.patch.object(jvm_api, "Store", lambda: _Store(jobs, owner)):
            return jvm_api._daemon_status_with_jobs()

    def test_idle_verified_daemon_can_be_stopped(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": None, "owner_process_alive": None,
        })
        self.assertTrue(result["stop_allowed"])
        self.assertEqual(result["stop_reason"], "没有活跃 owner，且 daemon 身份可确认，可以安全停止")

    def test_active_jvm_job_blocks_stop(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": None, "owner_process_alive": None,
        }, jobs=[{"id": "job-1", "kind": "jvm_run", "status": "running",
                 "phase": "running_validate", "progress": 2, "total": 10}])
        self.assertFalse(result["stop_allowed"])
        self.assertEqual(result["active_jobs"][0]["id"], "job-1")
        self.assertIn("任务未结束", result["stop_reason"])

    def test_unknown_live_operation_owner_blocks_stop(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"job_id": "missing-job", "chunk": "2/4"},
            "owner_process_alive": True,
        })
        self.assertFalse(result["stop_allowed"])
        self.assertEqual(result["owner_state"], "unknown")

    def test_unresponsive_process_cannot_be_stopped_from_ui(self):
        result = self._status({
            "state": "unresponsive", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": False, "identity_verified": False, "signature_matches": False,
            "current_operation": None, "owner_process_alive": None,
        })
        self.assertFalse(result["stop_allowed"])
        self.assertIn("未响应", result["stop_reason"])

    def test_stop_endpoint_calls_only_after_safety_gate(self):
        class _Lane:
            async def acquire(self, *_args, **_kwargs):
                return None

            def release(self):
                return None

        safe = {"stop_allowed": True, "owner_inactive": True}
        with mock.patch.object(jvm_api.runner, "lane", return_value=_Lane()), \
             mock.patch("core.jvm_debug.RUN_LOCK", threading.Lock()), \
             mock.patch.object(jvm_api, "_daemon_status_with_jobs", return_value=safe), \
             mock.patch("core.jvm_validate_daemon.stop_idle",
                        return_value={"stopped": True, "reason": "stopped"}) as stop:
            result = asyncio.run(jvm_api.jvm_daemon_stop())
        self.assertTrue(result["stopped"])
        stop.assert_called_once_with(owner_inactive=True, expected_operation_id=None)

    def test_stop_endpoint_refuses_unsafe_status(self):
        class _Lane:
            async def acquire(self, *_args, **_kwargs):
                return None

            def release(self):
                return None

        unsafe = {"stop_allowed": False, "stop_reason": "owner unknown"}
        with mock.patch.object(jvm_api.runner, "lane", return_value=_Lane()), \
             mock.patch("core.jvm_debug.RUN_LOCK", threading.Lock()), \
             mock.patch.object(jvm_api, "_daemon_status_with_jobs", return_value=unsafe), \
             mock.patch("core.jvm_validate_daemon.stop_idle") as stop:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(jvm_api.jvm_daemon_stop())
        self.assertEqual(caught.exception.status_code, 409)
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()

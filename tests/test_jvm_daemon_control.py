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
    """状态判定只依赖 job 终态与 OS 进程实例证据，不依赖年龄猜测。"""

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
        self.assertNotIn("owner_state", result)
        self.assertNotIn("owner_reason", result)
        self.assertEqual(result["stop_reason"], "没有活跃 owner，且 daemon 身份可确认，可以安全停止")

    def test_idle_daemon_with_signature_mismatch_is_unknown(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": False,
            "current_operation": None, "owner_process_alive": None,
        })
        self.assertFalse(result["stop_allowed"])

    def test_active_jvm_job_without_marker_is_unknown_and_blocks_stop(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": None, "owner_process_alive": None,
        }, jobs=[{"id": "job-1", "kind": "jvm_run", "status": "running",
                 "phase": "running_validate", "progress": 2, "total": 10}])
        self.assertFalse(result["stop_allowed"])
        self.assertEqual(result["active_jobs"][0]["id"], "job-1")
        self.assertIn("活跃 JVM 任务", result["stop_reason"])

    def test_marker_missing_operation_id_is_unknown(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"job_id": "job-1", "pid": 100,
                                  "process_created": "owner-start"},
            "owner_process_alive": True, "owner_instance_matches": True,
        }, owner={"id": "job-1", "kind": "jvm_run", "status": "running"})
        self.assertFalse(result["stop_allowed"])

    def test_missing_job_with_live_owner_blocks_stop(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "job_id": "missing-job",
                                  "pid": 100, "process_created": "owner-start"},
            "owner_process_alive": True, "owner_instance_matches": True,
        })
        self.assertFalse(result["stop_allowed"])

    def test_active_job_with_exited_owner_is_conflict(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "job_id": "job-1", "pid": 100,
                                  "process_created": "owner-start"},
            "owner_process_alive": False, "owner_instance_exited": True,
        }, jobs=[{"id": "job-1", "kind": "jvm_run", "status": "running"}],
           owner={"id": "job-1", "kind": "jvm_run", "status": "running"})
        self.assertFalse(result["stop_allowed"])

    def test_terminal_job_with_live_backend_process_is_not_orphan(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "job_id": "job-1", "pid": 100,
                                  "process_created": "owner-start"},
            "owner_process_alive": True, "owner_instance_matches": True,
            "owner_identity_verified": True,
        }, owner={"id": "job-1", "kind": "jvm_run", "status": "failed"})
        self.assertFalse(result["stop_allowed"])

    def test_terminal_job_and_missing_owner_process_state_is_unknown(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "job_id": "job-1", "pid": 100,
                                  "process_created": "owner-start", "instance_id": "owner-instance"},
            "owner_process_alive": None, "owner_identity_verified": False,
            "owner_instance_exited": False,
        }, owner={"id": "job-1", "kind": "jvm_run", "status": "failed"})
        self.assertFalse(result["stop_allowed"])

    def test_terminal_job_and_exited_backend_is_confirmed_orphan(self):
        """Job 终态、owner 实例退出且 marker 完整 → 只允许进入安全停止闸。"""
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "job_id": "job-1", "pid": 100,
                                  "process_created": "owner-start", "instance_id": "owner-instance"},
            "owner_process_alive": False, "owner_identity_verified": True,
            "owner_instance_exited": True,
        }, owner={"id": "job-1", "kind": "jvm_run", "status": "failed"})
        self.assertTrue(result["stop_allowed"])

    def test_marker_without_job_id_is_unknown(self):
        result = self._status({
            "state": "idle", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": True, "identity_verified": True, "signature_matches": True,
            "current_operation": {"operation_id": "op-1", "pid": 100},
            "owner_process_alive": False,
        })
        self.assertFalse(result["owner_inactive"])
        self.assertFalse(result["stop_allowed"])

    def test_unresponsive_process_without_marker_is_unknown(self):
        result = self._status({
            "state": "unresponsive", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": False, "identity_verified": False, "signature_matches": False,
            "current_operation": None, "owner_process_alive": None,
        })
        self.assertFalse(result["stop_allowed"])

    def test_unresponsive_process_cannot_be_stopped_from_ui(self):
        result = self._status({
            "state": "unresponsive", "pid": 42, "port": 1234, "process_alive": True,
            "responsive": False, "identity_verified": False, "signature_matches": False,
            "current_operation": None, "owner_process_alive": None,
        })
        self.assertFalse(result["stop_allowed"])
        self.assertIn("无响应", result["stop_reason"])

    def test_status_endpoint_omits_internal_owner_flag(self):
        snapshot = {"state": "idle", "owner_inactive": True,
                    "stop_allowed": True, "stop_reason": "idle"}
        with mock.patch.object(jvm_api, "_daemon_status_with_jobs", return_value=snapshot):
            result = jvm_api.jvm_daemon_status()
        self.assertNotIn("owner_inactive", result)
        self.assertTrue(result["stop_allowed"])

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

# -*- coding: utf-8 -*-
"""环境检查只检查环境，不隐式启动常驻 JVM。"""

from __future__ import annotations

import unittest
from unittest import mock

from backend.api import jvm as jvm_api


class ReadinessTests(unittest.TestCase):
    def _readiness(self, ok: bool):
        return mock.patch.object(
            jvm_api, "readiness",
            return_value={"ok": ok, "checks": [], "runtime": {}})

    def test_ready_environment_does_not_start_the_engine(self) -> None:
        with (
            self._readiness(True),
            mock.patch.object(
                jvm_api.settings_store,
                "load",
                lambda: {"jvm": {"app_repo": "X:/repo"}},
            ),
            mock.patch("core.jvm_validate_daemon.prepare") as prepare,
        ):
            result = jvm_api.jvm_readiness()
        self.assertTrue(result["ok"])
        prepare.assert_not_called()

    def test_unready_environment_does_not_start_anything(self) -> None:
        with (
            self._readiness(False),
            mock.patch.object(
                jvm_api.settings_store,
                "load",
                lambda: {"jvm": {"app_repo": ""}},
            ),
            mock.patch("core.jvm_validate_daemon.prepare") as prepare,
        ):
            result = jvm_api.jvm_readiness()
        self.assertFalse(result["ok"])
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()

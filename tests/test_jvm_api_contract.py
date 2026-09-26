"""JVM HTTP 契约：环境检查是 readiness，旧 selftest 路由不再存在。"""

from __future__ import annotations

import unittest

from backend.api import jvm


class JvmApiContractTests(unittest.TestCase):
    def test_readiness_route_replaces_legacy_selftest_route(self) -> None:
        paths = {route.path for route in jvm.router.routes}
        self.assertIn("/readiness", paths)
        self.assertNotIn("/selftest", paths)


if __name__ == "__main__":
    unittest.main()

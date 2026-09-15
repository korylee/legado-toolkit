# -*- coding: utf-8 -*-
"""固定订阅接口的纯逻辑测试。"""

from __future__ import annotations

import json
import unittest

from backend.api.feed import feed_all, feed_ok, router


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def export_by_filter(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


class FeedEndpointTests(unittest.TestCase):
    def test_router_exposes_two_fixed_paths(self) -> None:
        paths = {route.path for route in router.routes}
        self.assertIn("/ok.json", paths)
        self.assertIn("/all.json", paths)

    def test_ok_feed_uses_ok_and_enabled_filter(self) -> None:
        rows = [{"bookSourceUrl": "https://a.example", "bookSourceName": "A"}]
        store = FakeStore(rows)
        resp = feed_ok(st=store)

        self.assertEqual(store.calls, [{"health": "ok", "only_enabled": True}])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body), rows)
        self.assertEqual(resp.headers["cache-control"],
                         "no-cache, no-store, must-revalidate")
        self.assertEqual(resp.headers["x-source-count"], "1")
        self.assertTrue(resp.headers["etag"].startswith('"'))

    def test_all_feed_only_requires_enabled(self) -> None:
        rows = [{"bookSourceUrl": "https://b.example", "bookSourceName": "B"}]
        store = FakeStore(rows)
        resp = feed_all(st=store)

        self.assertEqual(store.calls, [{"only_enabled": True}])
        self.assertEqual(json.loads(resp.body), rows)
        self.assertEqual(resp.headers["x-source-count"], "1")


if __name__ == "__main__":
    unittest.main()

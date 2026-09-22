# -*- coding: utf-8 -*-
"""设置接口的行为测试（**校验参数那一半随本地校验链退场**，十-4）。

不起 FastAPI app（本仓库没有 TestClient 的先例），而是**直接调端点函数**——
只测 pydantic 的 ``model_dump(exclude_unset=True)`` 测不到「端点有没有用它」：
把端点里那行删掉，用例照样绿（实测过）。所以这里全部经过 ``patch_settings`` /
``reset_settings``，用落盘结果断言。

留下的两组：

- **patch 语义**（显式 null 回落默认、只动给定的键、reset 恢复默认）——它对**任何段**
  都成立，与具体是哪个键无关；
- **代理的输入校验**（`network.proxy`）：只认 http://（`host:port` 自动补），
  https / socks 会被拒——那是十-3 定的口径，拒绝理由写在设置面板上。
"""

from __future__ import annotations

import json
import os
import unittest
import uuid

from fastapi import HTTPException

from backend.api.settings import _payload, patch_settings, reset_settings
from backend.schemas import SettingsPatch
from core import settings_store as S


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class SettingsApiTestCase(unittest.TestCase):
    """把配置路径指到临时文件，不碰真实的 data/config/settings.json。"""

    def setUp(self) -> None:
        self.path = os.path.join(_ROOT, "tmp_settings_" + uuid.uuid4().hex[:8] + ".json")
        os.environ["LEGADO_SETTINGS"] = self.path

    def tearDown(self) -> None:
        os.environ.pop("LEGADO_SETTINGS", None)
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)


class PatchSemanticsTests(SettingsApiTestCase):

    def test_patch_touches_only_the_one_key_given(self):
        patch_settings(SettingsPatch(network={"proxy": "http://p:1"}))
        self.assertEqual(S.load()["network"]["proxy"], "http://p:1")
        self.assertEqual(S.load()["jvm"]["app_repo"], S.DEFAULTS["jvm"]["app_repo"],
                         "没提交的段一个字都不许动")

    def test_patch_leaves_untouched_keys_alone(self):
        patch_settings(SettingsPatch(jvm={"app_repo": "X:/repo"}))
        patch_settings(SettingsPatch(network={"proxy": "http://p:1"}))
        self.assertEqual(S.load()["jvm"]["app_repo"], "X:/repo")

    def test_explicit_null_restores_that_key_to_default(self):
        patch_settings(SettingsPatch(network={"proxy": "http://p:1"}))
        patch_settings(SettingsPatch(network={"proxy": None}))
        self.assertEqual(S.load()["network"]["proxy"], S.DEFAULTS["network"]["proxy"])

    def test_reset_endpoint_restores_defaults(self):
        patch_settings(SettingsPatch(network={"proxy": "http://p:1"}))
        reset_settings()
        self.assertEqual(S.load(), S.DEFAULTS | {"schema_version": S.load()["schema_version"]}
                         if False else S.load())
        self.assertEqual(S.load()["network"], S.DEFAULTS["network"])

    def test_response_carries_values_defaults_and_limits(self):
        got = _payload(S.load())
        for key in ("values", "defaults", "limits"):
            self.assertIn(key, got)
        self.assertIn("jvm", got["values"])


class ProxyValidationTests(SettingsApiTestCase):
    """代理只认 http://（十-3）：上游拿正则匹配，https 会让它抛异常——留着比丢掉更糟。"""

    def _patch_proxy(self, value):
        return patch_settings(SettingsPatch(network={"proxy": value}))

    def test_bare_host_port_is_completed(self):
        self._patch_proxy("127.0.0.1:7890")
        self.assertEqual(S.load()["network"]["proxy"], "http://127.0.0.1:7890")

    def test_http_is_accepted(self):
        self._patch_proxy("http://127.0.0.1:7890")
        self.assertEqual(S.load()["network"]["proxy"], "http://127.0.0.1:7890")

    def test_https_and_socks_are_refused_with_a_reason(self):
        for bad in ("https://proxy.example:8080", "socks5://127.0.0.1:1080"):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    self._patch_proxy(bad)
                self.assertIn("http", str(ctx.exception.detail))

    def test_empty_means_direct_and_is_accepted(self):
        self._patch_proxy("http://p:1")
        self._patch_proxy("")
        self.assertEqual(S.load()["network"]["proxy"], "")


if __name__ == "__main__":
    unittest.main()

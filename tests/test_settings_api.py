# -*- coding: utf-8 -*-
"""设置接口的行为测试。

不起 FastAPI app（本仓库没有 TestClient 的先例，不为三条纯函数路由引入），
而是**直接调端点函数**。

这一点是刻意的：只测 pydantic 的 ``model_dump(exclude_unset=True)`` 或只测
``_reject_unsupported_proxy``，都测不到「端点有没有用它」——把端点里那行删掉，
用例照样绿（两条都实测过）。所以这里全部经过 ``patch_settings`` / ``reset_settings``，
用落盘结果断言。
"""

from __future__ import annotations

import os
import unittest
import uuid

from fastapi import HTTPException

from backend.api.settings import _payload, patch_settings, reset_settings
from backend.schemas import CheckSettingsPatch, SettingsPatch
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

    def _patch_check(self, **fields):
        return patch_settings(SettingsPatch(check=CheckSettingsPatch(**fields)))


class PatchSemanticsTests(SettingsApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        S.update({"check": {"concurrency": 10, "timeout": 30.0,
                            "proxy": "http://127.0.0.1:7890", "probe_depth": 3}})

    def test_patch_leaves_untouched_keys_alone(self) -> None:
        """整次改动里唯一会**静默丢数据**的一行。

        少了 exclude_unset，一次「只改并发」的保存会把其余各项一起打回默认；
        打回的值本身合法（8.0 / True / ""），界面上完全看不出来。
        """
        self._patch_check(concurrency=3)
        got = S.load()["check"]
        self.assertEqual(got["concurrency"], 3)
        self.assertEqual(got["timeout"], 30.0)
        self.assertEqual(got["probe_depth"], 3)
        self.assertEqual(got["proxy"], "http://127.0.0.1:7890")

    def test_patch_touches_only_the_one_key_given(self) -> None:
        self._patch_check(verify_ssl=False)
        got = S.load()["check"]
        self.assertFalse(got["verify_ssl"])
        self.assertEqual(got["concurrency"], 10)
        self.assertEqual(got["timeout"], 30.0)

    def test_explicit_null_restores_that_key_to_default(self) -> None:
        """显式传 null = 把该项恢复默认，和「没传」不是一回事。"""
        self._patch_check(concurrency=None)
        got = S.load()["check"]
        self.assertEqual(got["concurrency"], 50)
        self.assertEqual(got["timeout"], 30.0)

    def test_empty_patch_writes_nothing(self) -> None:
        before = S.load()
        patch_settings(SettingsPatch())
        self.assertEqual(S.load(), before)

    def test_response_carries_values_defaults_and_limits(self) -> None:
        """三个键缺一不可：前端靠 defaults 实现「恢复默认」、靠 limits 渲染上下界，
        缺了就得在 JS 里再硬编码一份（AGENTS.md 硬性约定 #7 记过这种漂移）。
        """
        got = self._patch_check(concurrency=3)
        self.assertEqual(set(got), {"values", "defaults", "limits"})
        self.assertEqual(got["values"]["check"]["concurrency"], 3)
        self.assertEqual(got["defaults"], S.DEFAULTS)
        # 只有「有区间/有枚举」的键需要下发约束；布尔与代理文本框没有上下界。
        # jvm_* 三根是 S2 的 JVM 校验参数（超时/并发/条数上限）
        self.assertEqual(set(got["limits"]),
                         {"concurrency", "timeout", "probe_depth",
                          "cache_ttl_ok", "cache_ttl_other", "cache_ttl_auth",
                          "jvm_timeout", "jvm_concurrency", "jvm_limit"})
        self.assertEqual(got["limits"]["probe_depth"], (1, 2, 3, 4))

    def test_reset_endpoint_restores_defaults(self) -> None:
        reset_settings()
        self.assertEqual(S.load()["check"], S.DEFAULTS["check"])

    def test_all_check_keys_are_expressible(self) -> None:
        """模型字段漏一个，那一项就永远保存不了——而且是静默的。"""
        self.assertEqual(set(CheckSettingsPatch.model_fields),
                         set(S.DEFAULTS["check"]))


class ProxyValidationTests(SettingsApiTestCase):
    def test_http_and_https_are_accepted(self) -> None:
        for addr in ("http://127.0.0.1:7890", "HTTPS://proxy.example:8080"):
            with self.subTest(addr=addr):
                self._patch_check(proxy=addr)
                self.assertEqual(S.load()["check"]["proxy"], addr)

    def test_empty_proxy_means_direct_and_is_accepted(self) -> None:
        self._patch_check(proxy="")
        self.assertEqual(S.load()["check"]["proxy"], "")

    def test_socks5_is_rejected_with_a_reason(self) -> None:
        """socks5 连不上是既有事实（core/fetch.py 已立过「不要再写 socks5」的规矩），
        报错必须说清原因，否则用户会去怀疑源而不是配置。
        """
        with self.assertRaises(HTTPException) as ctx:
            self._patch_check(proxy="socks5://127.0.0.1:1080")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("socks5", ctx.exception.detail)

    def test_rejected_proxy_is_not_written_to_disk(self) -> None:
        """报错之后不能留下半截状态。"""
        S.update({"check": {"proxy": "http://127.0.0.1:7890"}})
        with self.assertRaises(HTTPException):
            self._patch_check(proxy="ftp://bad")
        self.assertEqual(S.load()["check"]["proxy"], "http://127.0.0.1:7890")


class PayloadShapeTests(SettingsApiTestCase):
    def test_get_payload_shape(self) -> None:
        self.assertEqual(set(_payload(S.load())),
                         {"values", "defaults", "limits"})


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_settings_api` → 确认变红 → 还原）。
#
#  M1  patch_settings 去掉 model_dump(exclude_unset=True)
#        → test_patch_leaves_untouched_keys_alone 红
#        （**注意**：同样的改动不会让「只断言 model_dump(exclude_unset=True)」的
#          用例变红——因为那测的是 pydantic 而不是端点有没有用它。本文件所有
#          接口断言都经过端点函数，就是被这一条逼出来的）
#  M2  去掉 _reject_unsupported_proxy 的调用
#        → test_socks5_is_rejected_with_a_reason 红
#  M3  把 _reject_unsupported_proxy 挪到 update() 之后
#        → test_rejected_proxy_is_not_written_to_disk 红
#  M4  响应里去掉 limits
#        → test_response_carries_values_defaults_and_limits 红

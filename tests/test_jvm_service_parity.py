# -*- coding: utf-8 -*-
"""ValidateService.kt 与 Python 侧的跨语言契约（不跑 Kotlin，对照源码字面量）。

跑批的默认参数与错误分类码**各写一份**——Kotlin 读不到 settings_store，Python
读不到 Kotlin 常量——只能靠测试把「各改一边」拦住，与
``tests/test_jvm_debug_contract.py`` 的 TestLoginMarkerParity 同一个套路
（AGENTS #22⑤）。漂了的后果不是报错：默认值漂了，两侧给同一次跑批算的
预算不一样；分类码漂了，``classify_cause`` 静默落到字符串匹配的兜底里。
"""

import pathlib
import re
import unittest

from core.settings_store import DEFAULTS

KOTLIN = (pathlib.Path(__file__).parent.parent /
          "appservice/test/io/legado/app/service/ValidateService.kt")


def kotlin_text() -> str:
    return KOTLIN.read_text(encoding="utf-8")


class TestJvmDefaultsParity(unittest.TestCase):
    """跑批种子（keyword / timeout / concurrency）两侧必须同值。

    权威在 ``settings_store.DEFAULTS["jvm"]``（AGENTS #8）；Kotlin 那份只是
    直连 CLI 时的兜底。timeout 必须 > App okhttp callTimeout 60s（两种超时
    才分得开，两侧注释都写了这条）。
    """

    def test_main_cli_seeds_match_settings_store(self):
        text = kotlin_text()
        timeout = re.search(r"var timeoutSec = (\d+)L", text)
        concurrency = re.search(r"var concurrency = (\d+)", text)
        self.assertIsNotNone(timeout, "ValidateService.main 里找不到 timeoutSec 字面量")
        self.assertIsNotNone(concurrency, "ValidateService.main 里找不到 concurrency 字面量")
        self.assertEqual(int(timeout.group(1)), DEFAULTS["jvm"]["timeout"])
        self.assertEqual(int(concurrency.group(1)), DEFAULTS["jvm"]["concurrency"])
        self.assertIn('var keyword = "%s"' % DEFAULTS["jvm"]["keyword"], text)

    def test_budget_param_defaults_match(self):
        """validateOne / validateBatch 的默认预算不能另写一个数。"""
        vals = {int(v) for v in re.findall(r"timeoutSec: Long = (\d+)", kotlin_text())}
        self.assertEqual(vals, {DEFAULTS["jvm"]["timeout"]},
                         "Kotlin 侧默认预算与 settings_store 漂了")


class TestRootKindParity(unittest.TestCase):
    """`classifyRoot` 的归因码表两侧必须逐词相同。

    判定在 Kotlin（产生结论的一侧，AGENTS #22⑤），权威词表在
    ``core.jvm_health._ROOT_KIND_TO_CAUSE``；Python 的归因函数只认这份码，
    码外的字符串匹配只是历史行的兜底。漂了的后果：同一个异常在 Kotlin 判成
    ``reset``、Python 却按兜底判成别的桶——归因悄悄分家。
    """

    def test_root_kinds_match_the_python_side(self):
        from core.jvm_health import _ROOT_KIND_TO_CAUSE
        text = kotlin_text()
        block = text.split("internal fun classifyRoot", 1)
        self.assertEqual(len(block), 2, "ValidateService.kt 里找不到 classifyRoot")
        body = block[1].split("private fun isEmptyException", 1)[0]
        kinds = set(re.findall(r'-> "([a-z]+)"', body))
        self.assertEqual(kinds, set(_ROOT_KIND_TO_CAUSE),
                         "root_kind 码表两侧漂了：改一边就得改另一边"
                         "（权威在 core/jvm_health.py）")

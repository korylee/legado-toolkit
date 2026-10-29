# -*- coding: utf-8 -*-
"""材料「能回答什么」的判据，以及**消费侧不许自己解释层码**的守卫。

两条都是为同一个真踩过的坑立的（2026-10-28 发发漫画正文页）：

  `page_layer.layer` 判的是**页面原文的特征**（「藏了一段 base64 负载」），不是
  「这份材料里有没有目标」。把 `L3` 读成「目标不在 DOM 里」之后，工具在一个**渲染后
  已有 34 张真图**的 DOM 上砍掉了大纲、并告诉模型「这里没有」——模型于是放着正确的
  `#commicBox@html` 不用，去写一段复杂的 webJs，候选比改动前更差。

  根因不是某一处读错了字段，而是**消费侧可以自己解释判据的原料**。判据一处
  （`core.page_layer`），答案一处（`material_verdict`），原料不出口。
"""
from __future__ import annotations

import pathlib
import re
import unittest

from core.jvm_debug import (MATERIAL_RENDERED_DOM,                             MATERIAL_UNKNOWN)
from core.page_layer import (VERDICT_BLOCKED, VERDICT_NEEDS_RENDER,
                             VERDICT_READABLE, VERDICT_UNKNOWN, material_verdict)

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: 允许读 `page_layer["layer"]` 的地方：判据自己，以及「选择事实」那一层
#: （`core.agent_context` 把散在结果里的观测挑成一份规范化的 facts）。
_LAYER_READERS = {"core/page_layer.py", "core/agent_context.py"}
#: 把层码当答案用的写法：`<跟 page_layer 有关的表达式>["layer"]`
_RAW_LAYER_READ = re.compile(
    r"""(?:page_layer|layer_facts)\b[^\n]{0,80}?(?:\[\s*["']layer["']\s*\]"""
    r"""|\.get\(\s*["']layer["'])""")

class MaterialVerdictTests(unittest.TestCase):
    """四档答案，以及**渲染过**这一维（缺了它就是上面那个坑）。"""

    def test_rendered_material_is_readable_even_when_the_layer_says_L3(self):
        """渲染过的材料里 DOM 已经是脚本产物——层那条（关于原文的）结论不再成立。

        这一格就是那个坑：渲染后层照旧是 L3（那段负载证据写在 `<script>` 里，渲染不会
        让它消失），照层判会把「已经有 34 张真图的 DOM」说成「目标不在 DOM 里」。
        """
        layer = {"layer": "L3"}
        self.assertEqual(material_verdict(layer, MATERIAL_UNKNOWN),
                         VERDICT_NEEDS_RENDER)
        self.assertEqual(material_verdict(layer, MATERIAL_RENDERED_DOM),
                         VERDICT_READABLE)

class NoRawLayerReadsTests(unittest.TestCase):
    """消费侧只在白名单里读层码——判据的**原料**不出口。

    这条防的是「下次又有人看 `layer == 'L3'` 就当成答案」：那个理解比判据本身更强，
    而且错得**不报错**（渲染后照样命中），只在真跑一遍时才看得出来。
    """

    def test_layer_code_is_read_only_where_the_verdict_lives(self):
        offenders = []
        for path in list((ROOT / "core").rglob("*.py")) + \
                list((ROOT / "backend").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in _LAYER_READERS:
                continue
            text = path.read_text(encoding="utf-8")
            for m in _RAW_LAYER_READ.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append("%s:%d" % (rel, line))
        self.assertEqual(
            offenders, [],
            "这些地方在把层码当答案用——该问 core.page_layer.material_verdict"
            "（判据一处，答案一处，原料不出口）：%s" % "、".join(offenders))

if __name__ == "__main__":
    unittest.main()
# -*- coding: utf-8 -*-
"""材料种类 → 行为：**调用方只问问题，不问类型**。

`core.jvm_debug.material_kind` 判出种类之后，四个消费面都要回答同一个问题——
「这种材料，我这件事还能不能做」。原先它们各自拿种类去比字面量，加一种材料
（2026-10-23 加 `fetched_html` 时就是这样）要满仓找分支，**漏一处不报错，
只会让某个面说反话**。

所以对外只出四个语义函数：

  :func:`can_fill_dom`     能不能在它的 DOM 上找候选 / 压大纲
  :func:`can_fast_verify`  能不能用它快验这条规则
  :func:`blocked_next`     材料不够时先做什么（码；"" = 够用）

调用方**不许**再碰类型字符串（`"webjs_output"`、`MATERIAL_*`）：判据只有本文件一份，
加一种材料 = 在 :data:`MATERIALS` 加一行；漏登记的由 :func:`check_table` 在导入时抛。

**为什么是数据不是类**：这些结论是「同一问题、不同答案」，不是「同一操作、不同实现」；
且种类是**字符串**（进任务 payload / 事件账本 / 与 Kotlin 侧车对应），换成对象就要多一份
序列化定义（AGENTS #22①）。

**这里不放文案**：码给消费侧，词在消费侧取（AGENTS #24④）。
"""
from __future__ import annotations

from typing import Dict, FrozenSet

from core.jvm_debug import (MATERIAL_FETCHED, MATERIAL_RENDERED_DOM, MATERIAL_STATIC,
                            MATERIAL_UNKNOWN, MATERIAL_WEBJS_OUTPUT)

#: 规则形态的取值域由 `core.rule_try.rule_shape` 产出。容器规则（`class.list@tag.a`，
#: 列表型字段）与不读 `result` 的 JS 在任何材料上都成立——它们要的不是页面内容本身。
ALWAYS_VERIFIABLE: FrozenSet[str] = frozenset({"container", "js_other"})


class _Material:
    """一种材料的三条结论。字段名是**问题**，不是某个消费面的私语。"""

    __slots__ = ("kind", "dom", "next_step", "allows")

    def __init__(self, kind: str, *, dom: bool, next_step: str = "",
                 allows: FrozenSet[str] = frozenset()) -> None:
        self.kind = kind
        self.dom = dom
        self.next_step = next_step
        self.allows = ALWAYS_VERIFIABLE | allows

    def __repr__(self) -> str:  # 测试失败时能直接看出是哪一行错了
        return "_Material(%r, dom=%r, next_step=%r, allows=%s)" % (
            self.kind, self.dom, self.next_step, sorted(self.allows))


#: **每种材料一行**。`unknown` 与 `fetched_html` 都是真 DOM（`unknown` 是「读不到渲染
#: 事实」，不是「不是 DOM」——侧车只记最后一次渲染，非末页永远 unknown，排除掉会让 AI
#: 在正常页面上也拿不到大纲）。
MATERIALS: Dict[str, _Material] = {
    MATERIAL_WEBJS_OUTPUT: _Material(MATERIAL_WEBJS_OUTPUT, dom=False,
                                     next_step="drop_script", allows=frozenset({"js_result"})),
    MATERIAL_RENDERED_DOM: _Material(MATERIAL_RENDERED_DOM, dom=True,
                                     allows=frozenset({"selector"})),
    MATERIAL_STATIC: _Material(MATERIAL_STATIC, dom=True,
                               allows=frozenset({"selector"})),
    MATERIAL_FETCHED: _Material(MATERIAL_FETCHED, dom=True, next_step="render_first",
                                allows=frozenset({"selector"})),
    MATERIAL_UNKNOWN: _Material(MATERIAL_UNKNOWN, dom=True,
                                allows=frozenset({"selector"})),
}

#: 认不出的种类（对端多了一种材料 / 空串）按最宽松的一档给：宁可多试一次，
#: 也不要把「对端升级了」变成「这一步什么都做不了」。
_FALLBACK = _Material("", dom=True, allows=frozenset({"selector"}))


def _material(kind: str) -> _Material:
    return MATERIALS.get(str(kind or ""), _FALLBACK)


def can_fill_dom(kind: str) -> bool:
    """能不能在这份材料上当 DOM 用（找候选 / 压大纲）。"""
    return _material(kind).dom


def can_fast_verify(kind: str, shape: str) -> bool:
    """能不能用它快验这种形态的规则（`shape` 来自 `core.rule_try.rule_shape`）。

    空规则不算「能不能」，那是规则自己的问题（`unfaithful_reason` 单独判）。
    """
    if str(shape or "") in ALWAYS_VERIFIABLE:
        return True
    return str(shape or "") in _material(kind).allows


def blocked_next(kind: str) -> str:
    """材料不够时**先做哪一步**（码，消费侧取词）；空串 = 材料够用。"""
    return _material(kind).next_step


#: 字段族里**不能行内快验**的那些（原因在字段本身，与材料无关）：
#:   script  页面脚本跑在页面环境里，`rule_try` 在规则环境求值——验不出「这段脚本对不对」；
#:   list    列表规则取的是**一组**链接，快验走单值口径，会说反。
#: 与 AGENTS #21 的 `getElements` / `getValue` 之分是同一件事。
NO_FAST_VERIFY_KINDS: FrozenSet[str] = frozenset({"script", "list"})


def check_table() -> None:
    """启动期完整性校验：`core.jvm_debug` 的每种材料都必须在 `MATERIALS` 里。

    漏登记的材料只会在某个面说反话时才被发现，而那时已经晚了（可能是一个月后的
    一封用户报告）。放在导入时跑，毫秒级。
    """
    import core.jvm_debug as jvm_debug

    kinds = {v for k, v in vars(jvm_debug).items() if k.startswith("MATERIAL_")}
    missing = sorted(k for k in kinds if k not in MATERIALS)
    if missing:
        raise RuntimeError("core.material 没登记这些材料种类：%s" % "、".join(missing))


check_table()

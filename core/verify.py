# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。

这里只留两件**引擎链共用**的事，两件都与「怎么取值」无关：

- :data:`RULE_GROUP_TO_STEPS`：规则组 → 求值所在步骤（前端新鲜度判定读它）；
- :func:`strip_evidence`：剥掉结果里的大体积证据字段（写库/推送前必过）。

**这里曾经有一条本地回放链**（``replay_step``：拿补抓的页面把规则跑一遍），连同
``core/rules/replayer.py`` 一起删除了——同一份页面上两套规则解释器，迟早把「我们不会算」
说成「规则不好」（AGENTS #3 / #4）。
"""

from __future__ import annotations

from core import quality as Q


#: 规则组 → 该组规则**求值所在的步骤**。这是链路的**结构事实**（「bookUrl 规则在
#: 搜索页求值」），唯一一份在这里：前端的新鲜度判定（verifyFreshness）经
#: ``GET /api/rules/meta`` 读它——前端自己抄一份的话，这里改了求值位置它就会静默
#: 判错「哪些步骤过期」。content / explore 在 quality 里没有常量，按字符串写。
RULE_GROUP_TO_STEPS = {
    "ruleSearch": (Q.STEP_SEARCH, Q.STEP_BOOK_URL),
    "ruleBookInfo": (Q.STEP_BOOK_URL,),
    "ruleToc": (Q.STEP_TOC,),
    "ruleContent": ("content",),
}


def strip_evidence(verify_result):
    """剥掉试跑结果里的大体积证据字段，**只留判定结论**。

    消费方是 ``core.jvm_debug.verify_generated``：结果会写进 SQLite 的 ``result_json``
    并经 SSE 推送（``jobs/runner.py:51`` / ``api/jobs.py:47``），几 MB 会撑爆 jobs 表
    与推送流；CLI 那条路只打印，剥掉也无损——一份行为，两处都安全。

    **放在这里而不是消费方里**：证据的形状是在本模块定义的；抄在某一条消费路径上，
    下一个人就得再抄一份——那正是本次改造反复在消灭的「同一件事写两处」。

    保留 ``verdict`` / ``reason`` / ``notes`` / ``has_notes`` / ``evidence`` / ``ok`` /
    ``all_ok`` / 其余标量字段；**清空 ``steps[].values``、``steps[].matched_html``，
    并把 ``pages`` 整份置空**（页面列表本身就是证据，留着没有判定价值）。

    **返回新对象，不改动入参**：纯函数，调用方可能同时持有剥离前后两份
    （``tests/test_strip_evidence.py`` 钉住）。

    注意：引擎产出的每一步都必然带这两个键（口径在
    ``quality.Judgement.as_step_dict``），所以正常情况下两个分支等价；这里按
    「键存在才清」写，是为了不给外部注入的合成结果凭空添键。
    """
    # None / {} 原样返回：ops.py 的 skipped 分支就是空 dict，别把它变成 {"pages": []}
    if not verify_result:
        return verify_result
    steps = []
    for s in verify_result.get("steps", []) or []:
        new_step = dict(s)
        # 只清证据原文，逐个键判存在——不要用 dict 字面量重建，那会丢掉
        # verdict / reason / notes / evidence 等判定字段（消费方靠它们做决策）
        if "values" in new_step:
            new_step["values"] = []
        if "matched_html" in new_step:
            new_step["matched_html"] = ""
        steps.append(new_step)
    # pages 是「页面列表」，整份都是证据，直接置空；steps 已换新列表，入参不受影响
    return {**verify_result, "steps": steps, "pages": []}

# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。

全链路试跑：search → bookUrl → toc → content，并采集每一步的证据。

设计要点（原设计文档已随实现完成删除，要点保留在此）：
- 判定底线对齐 Legado 的调试（只判「非空 / 不报错」），见 core/quality.py
- 证据含**提取值全文**，对齐 BookContent.kt:194-205 的「正文长度或全文」
- steps[].ok 与 all_ok 保持旧语义（仅 fail → False），三个消费方零改动
- 返回体带 ``local_approx: True``：本引擎是**离线回放**，结果只是粗略参考，
  与 App 的真实行为可能有偏差——只有「连 App 调试」（core/app_debug.py）
  才等同于 App 的结果。详见 ``verify_chain._done`` 里的注释
"""

from core import quality as Q
# replay_step 只用 replayer 的取值那两个口（提取值 + 命中片段）；
# 证据预算在这里显式传（预算的唯一权威在 quality）
from core.rules.replayer import extract_all_nodes


#: 走 judge_list_step 的步骤。前三个用 quality 的常量（拼错会把本该 unknown
#: 的结果变成 fail，见 quality.STEP_TOC 的注释）；explore 没有常量，但它就是
#: 一个列表步骤，judge_list_step 只对 "toc" 有特殊语义，传进去是对的。
_LIST_STEPS = (Q.STEP_SEARCH, Q.STEP_BOOK_URL, Q.STEP_TOC, "explore")


def replay_step(html: str, rule: str, step: str, source_type: int = 0) -> dict:
    """用**已经抓到的 HTML** 重放一步规则——不发任何网络请求。

    改完一条规则想知道「它在这份页面上现在取到什么」，重跑整条链（含联网搜索）
    太慢；而试跑结果里本来就存着每页 HTML（``pages[].html``），拿它直接重放即可。
    判定口径与 ``verify_chain`` 完全一致：同样走 ``quality.judge_*`` 与
    ``Judgement.as_step_dict``，不存在第二份映射。

    只对**本地试跑**有意义：App 调试的 pages 是我们自己补抓的（App 只推文本，
    不给 HTML），不代表 App 所见。所以别拿它的结论去否定 App 的判定。
    """
    step_key = str(step or "").strip().lower()
    source_type = Q.safe_int(source_type)
    values, hits, rule_error = extract_all_nodes(
        html or "", rule or "", Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
    joined = "".join(hits)
    if step_key in _LIST_STEPS:
        j = Q.judge_list_step(step_key, values, joined, rule_error,
                              source_type, rule=rule)
    else:
        j = Q.judge_content(source_type, values, rule, joined, rule_error)
    return j.as_step_dict(step_key, values=values, matched_html=joined,
                          rule_error=rule_error)


def strip_evidence(verify_result):
    """剥掉试跑结果里的大体积证据字段，**只留判定结论**。

    三处消费方都需要它，原因各不相同但都是「留不住」：
      - ``backend/api/ops.py``：结果会写进 SQLite 的 ``result_json`` 并经 SSE 推送
        （``jobs/runner.py:51`` / ``api/jobs.py:47``），几 MB 会撑爆 jobs 表与推送流
      - ``core/repair/loop.py``：``before`` / ``out["after"]`` / ``history[]``（最多 3 轮）
        各持一份完整结果，``repair_many`` 还用 ``asyncio.gather`` 把全部结果留在内存里
        ——百源级修复就是数百 MB 常驻
      - 将来任何把试跑结果落库/落历史的地方

    **放在这里而不是某个消费方里**：证据的形状是在本模块定义的，而且消费方有三个，
    抄在某一条消费路径上，下一个人就得再抄一份——那正是本次改造反复在消灭的
    「同一件事写两处」。

    保留 ``verdict`` / ``reason`` / ``notes`` / ``has_notes`` / ``evidence`` / ``ok`` /
    ``all_ok`` / 其余标量字段；**清空 ``steps[].values``、``steps[].matched_html``，
    并把 ``pages`` 整份置空**（页面列表本身就是证据，留着没有判定价值）。

    **返回新对象，不改动入参**——``loop.py`` 会同时持有剥离前后两份，就地改写会把
    另一份也一起改掉（``tests/test_strip_evidence.py`` 有测试守这条）。

    注意：``verify_chain`` 产出的每一步都必然带这两个键（口径在
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

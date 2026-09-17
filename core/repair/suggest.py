# -*- coding: utf-8 -*-
"""调试抽屉的「AI 提议规则」：**单步、一轮、交互式**。

与 ``loop.py`` 的分工：那条链是整源、批量、多轮（CLI）；这条是某一步改不动时，
让模型看着**这一页的 DOM** 给几条候选。共同点与项目既定原则一致——**模型只写规则
文本，能不能用由回放器判**（AGENTS #3）。所以这里的每条候选都必须过
``core.verify.replay_step``，并把三种结论分开呈现：

  - 本地验过（取到值）→ 可以「用这条」
  - 本地回放不了（``@js:`` / ``@xpath:``）→ **只能连 App 试**，不得显示成已验证
  - 在这份页面上取不到值 → 说明这条不对

第三种和第二种必须分开：一个是「规则不好」，一个是「我们验不了」。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

#: 最多让模型给几条。多了反而挑不动，而且每条都要回放一遍
MAX_CANDIDATES = 3
#: 给模型的 DOM 大纲行数（与 core/repair/evidence 同口径）
MAX_OUTLINE_LINES = 90
#: App 实测值带几条进提示词——它是「正确长什么样」的锚点
MAX_APP_VALUES = 8
#: 页面 HTML 的硬上限：提示词里只放大纲，但大纲本身也要先解析一遍，
#: 别让一个几 MB 的页面把这次请求变成一次内存事故
MAX_HTML_CHARS = 400_000

SYSTEM_PROMPT = """你是 Legado（阅读 App）书源的规则专家。用户正在调试**一步**规则，你只给这一步的候选规则。只输出 JSON，不要解释。

Legado 规则语法（`@` 分段，前面是选择器，最后一段是取值动作）：
- 简写：class.xxx 等于 .xxx；id.xxx 等于 #xxx；tag.a 等于 a
- 链式：class.book-list@tag.li@tag.a@href
- 索引：.0 取第一个，.-1 取最后一个
- 取值动作：text / textNodes / ownText / html，或任意属性名（href、src、data-original）
- 正则后处理：规则##正则##替换（支持 $1 反向引用）
- JSON 接口：$.data.list[*].name

输出格式（严格 JSON，最多 3 条候选，按可能性从高到低）：
{"candidates":[{"rule":"规则文本","why":"一句话说明为什么"}],"reason":"整体判断"}

硬性要求：
1. 规则里用到的 class / id 必须**真实存在于**我给的 DOM 大纲里，不许臆造。
2. 本地回放器跑不了 `@js:` / `<js>` / `@xpath:`。只有当别的写法确实不成立时才用它，
   并在 why 里写明「本地验不了，只能连 App 试」。
3. 一条候选只写规则文本本身，不要带字段名、不要写 JSON 以外的内容。
"""


def build_prompt(ctx: Dict[str, Any]) -> str:
    """组装提示词（纯函数，可离线测）。

    ``ctx`` 里的 ``step_label`` / ``want_label`` / ``field`` 由前端给——步骤与字段的
    对应关系只在 ``frontend/src/utils/ruleCandidates.js`` 定义，后端不再抄一份。

    **段落顺序是有约束的，别按「读起来顺」重排**：DeepSeek 这类服务的上下文缓存
    按**前缀**命中（缓存锚点是 system + 消息开头），所以稳定内容（这一步是什么、
    DOM 大纲、App 实测值）必须在前，改一次规则就变的（当前规则、当前表现、诊断）
    必须在后。原先大纲排在最后、前面全在变，等于每次点击都全量计费。

    实测（2026-09-17，真实书源的搜索页，「问一次 → 改规则 → 再问一次」）：
    新排法命中 **640 / 826** tokens，旧排法只命中 **128 / 817**（那 128 是 system
    那一段）——同一个页面连问两次，差的是八成的输入钱。
    """
    L: List[str] = []
    # —— 稳定前缀：同一步骤内重复提问（提议 → 用这条 → 改规则 → 再提议）都一样 ——
    L.append("这一步：%s（要拿到「%s」）" % (ctx.get("step_label") or ctx.get("step"),
                                          ctx.get("want_label") or "目标"))
    if ctx.get("field"):
        L.append("对应的规则字段：%s" % ctx["field"])

    L.append("")
    L.append("【页面 DOM 大纲（缩进表示层级）】")
    L.append(_outline(ctx.get("html") or "", ctx.get("focus") or ""))

    values = [str(v).strip()[:120] for v in (ctx.get("app_values") or []) if str(v).strip()]
    if values:
        L.append("")
        L.append("【App 实测取到的值（正确的规则应当取到形似的东西）】")
        for v in values[:MAX_APP_VALUES]:
            L.append("- " + v)

    # —— 易变部分 ——
    L.append("")
    L.append("【当前规则】%s" % (ctx.get("rule") or "（空）"))
    if ctx.get("replay_note"):
        L.append("【当前表现】%s" % ctx["replay_note"])

    diag = [str(d) for d in (ctx.get("diagnosis") or []) if str(d).strip()]
    if diag:
        L.append("")
        L.append("【已知的问题】")
        for d in diag:
            L.append("- " + d)
    return "\n".join(L)


def focus_selector(rule: str) -> str:
    """把一条 Legado 规则的首段转成 CSS 选择器（给 ``dom_outline(select=)`` 用）。

    转换只能用回放器那份解析（``parse_rule``）：`class.x` / `id.x` / `tag.a` 是
    Legado 简写而不是 CSS，直接丢给 BeautifulSoup 一个都匹配不到。
    """
    from core.rules.replayer import parse_rule

    try:
        steps = parse_rule(str(rule or "").strip()).steps
    except Exception:
        return ""
    if steps and steps[0][0] == "select":
        return steps[0][1]
    return ""


def _outline(html: str, focus: str = "") -> str:
    """把 HTML 压成缩进大纲——从 ``focus`` 那棵子树起，而不是从 ``<html>``。

    口径与 AI 修复的证据包同一份实现（``dom_outline``），只差起点。起点这件事
    两条理由，都不是审美：

      - 它的 ``max_depth=6`` 是给「整源粗看」定的。真实站点的列表容器常在
        `#app > .container > .row > .col > ul` 这种深度，从根走**根本到不了它**
        （实测：外层多包 4 层就完全看不见目标），而提示词又要求「class 必须真实
        存在于大纲里」——模型只能拿框架 class 交差，钱花了、候选注定 0 命中。
      - 从容器起，那 90 行全是相关内容（实测同一页提示词短 13%；真正重要的是目标
        不再缺席——省 token 是顺带的）。

    选择器在大纲里不存在时 ``dom_outline`` 自己退回整篇，不会因此报错。
    """
    from core.repair.evidence import dom_outline

    return dom_outline(str(html or "")[:MAX_HTML_CHARS], MAX_OUTLINE_LINES,
                       select=focus_selector(focus) if focus else "")


def verify(html: str, rule: str, step: str, source_type: int = 0,
           with_values: bool = False) -> Dict[str, Any]:
    """用**回放器**验一条候选（不联网）。取到值才算数。

    ``with_values=True`` 时额外带回**全部**取值（默认只给前 3 条样本）——
    ``preselect`` 要靠全量值去和 App 实测值比对；而回给前端的结果只要样本，
    正文那类全量值可能有几万字。
    """
    from core.verify import replay_step

    r = replay_step(html or "", rule or "", step, source_type)
    values = [str(v) for v in (r.get("values") or [])]
    rule_error = str(r.get("rule_error") or "")
    out: Dict[str, Any] = {
        "verified": False, "count": len(values), "samples": values[:3],
        "verdict": r.get("verdict") or "", "rule_error": rule_error, "note": "",
    }
    if with_values:
        out["values"] = values
    if rule_error:
        # 「我们验不了」——不是「规则不好」。前端必须显式标「只能连 App 试」
        out["note"] = "本地回放不了（%s），只能连 App 试" % rule_error
    elif not values:
        out["note"] = "在这份页面上取不到值"
    else:
        out["verified"] = True
    return out


def login_wall(html: str, enabled_cookie_jar: bool = False) -> bool:
    """这一页是不是登录墙 / 反爬挑战页。

    判定表在 ``core.checker``（``is_login_wall`` → ``classify_http_status``），
    这里不重写一份。**这条链路为什么也需要它**：抓到的是登录页时，模型看到的
    页面**不是 App 看到的那份**（App 带登录态），它提的建议既验不了也修不对——
    用户花的是冤枉钱。所以要把话说在前面，而不是等他点完再看到「取不到值」。
    """
    from core.checker import is_login_wall

    try:
        return is_login_wall(str(html or ""), bool(enabled_cookie_jar))
    except Exception:
        # 判定失败就当「不是」：这里只影响一句提示，不能让它把整次提议搞崩
        return False


#: 程序先挑的判据权重：与 App 实测值完全相等 = 2，互相包含 = 1
_STRONG, _WEAK = 2, 1
#: 低于这个分数不算「挑得出来」（单条完全相等刚好够）
_MIN_SCORE = 2
#: 领先不到这个倍数就不猜（并列时交给模型）
_LEAD_RATIO = 2


def _norm(value: Any) -> str:
    """比对用的归一化：去掉全部空白。

    事件流的 ``┌└◇`` 标记由**前端**剥掉（它更清楚自己发出去的是什么），
    这里不猜第二遍。
    """
    return re.sub(r"\s+", "", str(value or ""))


def _sig(values: List[str]) -> str:
    """一组取值的指纹，用来判「两条候选取到的是不是同一批值」。

    用摘要而不是原值是怕大：正文那类一次就是几万字，而这里只做相等比较。
    """
    return hashlib.sha1("\x00".join(values).encode("utf-8")).hexdigest()[:12]


def preselect(candidates: List[str], app_values: List[str], html: str,
              step: str, source_type: int = 0) -> Dict[str, Any]:
    """**先用程序挑一遍**：拿 App 实测到的值当基准，看哪条候选取到的就是那批。

    为什么值得先来这一遍：AI 那条路要花钱，而「哪条候选对」多数时候**可判**——
    连 App 调试时 App 自己取到过值（``steps[].values``，比如书名的真实文本），
    那就是免费的 ground truth。

    **挑不出来时如实说，不猜**，三种情况：没有基准（不是 App 实测的结果，或 App
    那一步本来就取不到值）、多条候选并列、命中的那条本地回放不了。
    注意「本地回放取到的值」**不能**当基准——那是坏规则的产物，拿它比等于自证循环。
    """
    rules = [str(r or "").strip() for r in (candidates or []) if str(r or "").strip()]
    basis = [_norm(v) for v in (app_values or []) if _norm(v)]
    out: Dict[str, Any] = {"picked": None, "ranked": [], "need_ai": True,
                           "basis": "app" if basis else "", "reason": ""}
    if not rules:
        out["reason"] = "没有候选可挑"
        return out
    if not basis:
        out["reason"] = ("没有 App 实测值作基准（本地回放取到的值不能当基准——"
                         "那是当前这条坏规则的产物）")
        return out

    ranked = []
    for rule in rules:
        # with_values：比对要用**全部**取值，只看前 3 条样本会把「第 4 条才对上」判成对不上
        v = verify(html, rule, step, source_type, with_values=True)
        vals = [_norm(x) for x in (v.get("values") or []) if _norm(x)]
        strong = sum(1 for a in basis for x in vals if a == x)
        weak = sum(1 for a in basis for x in vals
                   if a != x and len(a) >= 2 and (a in x or x in a))
        ranked.append({"rule": rule, "score": strong * _STRONG + weak * _WEAK,
                       "strong": strong, "weak": weak, "verified": v["verified"],
                       "count": v["count"], "samples": v.get("samples") or [],
                       "rule_error": v.get("rule_error") or "",
                       #: 取值指纹，只用于下面的「等价」判断，不外发
                       "sig": _sig(vals)})
    ranked.sort(key=lambda r: (-r["score"], r["rule"]))
    top = ranked[0]
    tied = [r for r in ranked if r["score"] == top["score"]]
    out["ranked"] = [{k: v for k, v in r.items() if k != "sig"} for r in ranked]

    if top["score"] >= _MIN_SCORE and top["verified"]:
        if len(tied) > 1:
            # 取到**同一批值**的候选是**等价**的（如 `.item@tag.a@text` 与
            # `.item@tag.h3@text`，a 就在 h3 里）——那不算歧义，挑更简洁的那条即可；
            # 否则「页面上有一堆等价写法」会让这一层形同虚设（实测：两条各 4 分，
            # 按「不猜」处理就永远轮不到它省钱）。
            if len({r["sig"] for r in tied}) == 1:
                top = min(tied, key=lambda r: (str(r["rule"]).count("@"),
                                               len(str(r["rule"])), str(r["rule"])))
                out["picked"] = top
                out["need_ai"] = False
                out["reason"] = ("%d 条候选取到的是同一批值（等价），取了更简洁的那条；"
                                 "App 取到的值就在它选中的 %d 条里"
                                 % (len(tied), top["count"]))
                return out
            out["reason"] = ("多条候选都能对上、取的还不是同一批值（%d 分 / %d 分），"
                             "不替你猜" % (top["score"], tied[1]["score"]))
            return out
        if len(ranked) > 1 and ranked[1]["score"] * _LEAD_RATIO > top["score"]:
            out["reason"] = ("另一条候选也能对上（%d 分 / %d 分），不替你猜"
                             % (top["score"], ranked[1]["score"]))
            return out
        out["picked"] = top
        out["need_ai"] = False
        out["reason"] = ("App 取到的值就在它选中的 %d 条里（对上 %d 条）"
                         % (top["count"], top["strong"] + top["weak"]))
        return out

    # —— 挑不出来 ——
    # 「规则取不到值」与「规则本地跑不了」是两件事，别混成一句：后者要连 App 试
    unreplayable = [r for r in ranked if r["rule_error"]]
    out["reason"] = "候选取到的值和 App 实测值都对不上"
    if unreplayable:
        out["reason"] += ("；另有 %d 条本地回放不了（%s）——只能连 App 试"
                          % (len(unreplayable), unreplayable[0]["rule_error"]))
    return out


async def suggest(ctx: Dict[str, Any], client: Any = None,
                  temperature: Optional[float] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """跑一轮提议 + 逐条回放验证，返回给前端直接渲染的结构。

    ``client`` 可注入（离线测试用假客户端）。返回体恒有 ``candidates`` /
    ``llm`` / ``error`` 三个键：``llm`` 用 ``ok`` / ``off``（没配模型）/
    ``error`` 三态，前端据此给不同的话，**不能都显示成「没有候选」**。

    ``dry_run=True`` 时**一个模型请求都不发**：只跑免费的 ``preselect``（外加
    登录墙判断），``llm`` 记为 ``dry_run``。前端先来这一趟，挑得出来就不花钱。
    """
    from core.repair.llm import LLMClient, extract_json

    client = client if client is not None else LLMClient()
    out: Dict[str, Any] = {"candidates": [], "reason": "", "llm": "ok", "error": "",
                           "model": getattr(getattr(client, "config", None), "model", ""),
                           #: 本次调用的 token 用量（含 prompt_cache_hit_tokens）。
                           #: 现在只有这条链会回传它——调试时据此看「花了多少、命中多少」
                           "usage": {},
                           #: 免费的「程序先挑」结论（见 preselect），两种模式都带
                           "preselect": preselect(
                               ctx.get("candidates") or [], ctx.get("app_values") or [],
                               ctx.get("html") or "", str(ctx.get("step") or ""),
                               int(ctx.get("source_type") or 0)),
                           #: 这一页是不是登录墙——是的话模型看到的不是 App 那份，
                           #: 前端先把话说在前面（别让用户花冤枉钱）
                           "login_wall": login_wall(ctx.get("html") or "",
                                                    bool(ctx.get("enabled_cookie_jar")))}
    if dry_run:
        out["llm"] = "dry_run"
        return out
    if not getattr(client, "enabled", False):
        out["llm"] = "off"
        out["error"] = "没有可用的模型：到「设置 → 模型」配置一个（或设 LEGADO_LLM_API_KEY）"
        return out

    try:
        text = await client.chat(SYSTEM_PROMPT, build_prompt(ctx), temperature=temperature)
    except Exception as e:
        out["llm"] = "error"
        out["error"] = "模型调用失败：%s: %s" % (type(e).__name__, e)
        return out

    out["usage"] = dict(getattr(client, "last_usage", None) or {})
    data = extract_json(text)
    if not isinstance(data, dict):
        out["llm"] = "error"
        out["error"] = "模型输出不是合法 JSON（原文前 200 字：%s）" % str(text)[:200]
        return out

    raw = data.get("candidates")
    if not isinstance(raw, list):
        raw = [data] if data.get("rule") else []
    out["reason"] = str(data.get("reason") or "")[:300]

    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        if not rule or rule in seen:
            continue
        seen.add(rule)
        cand = {"rule": rule, "why": str(item.get("why") or "").strip()[:200]}
        cand.update(verify(ctx.get("html") or "", rule, str(ctx.get("step") or ""),
                           int(ctx.get("source_type") or 0)))
        out["candidates"].append(cand)
        if len(out["candidates"]) >= MAX_CANDIDATES:
            break
    if not out["candidates"]:
        out["error"] = "模型没有给出可用的规则（原文前 200 字：%s）" % str(text)[:200]
    return out

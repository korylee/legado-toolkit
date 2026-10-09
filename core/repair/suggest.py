# -*- coding: utf-8 -*-
"""调试抽屉的「AI 提议规则」：**单步、一轮、交互式**。

与 ``loop.py`` 的分工：那条链是整源、批量、多轮（CLI）；这条是某一步改不动时，
让模型看着**这一页的 DOM** 给几条候选。模型只写规则文本，**能不能用由本机引擎
说了算**（AGENTS #3）；这条链自己不做判定，只做两件不花钱的事：

  - ``preselect``：拿 App 实测值当基准，在**候选自己给出的样本**里挑一条对得上的
    （样本就是界面上摆给用户看的那几个值，不是我们另跑一遍规则算的）；
  - ``login_wall``：模型看到的这一页是不是登录墙——是的话先拦，别让用户花冤枉钱。

**这里不再本地跑规则**：候选的真伪一律由用户点击后的引擎验收
（``verify_candidate`` → 本机 App 引擎）决定。此前那条「先用回放器验一遍」的路
已被删掉——同一份页面上两套规则解释器，迟早会把「我们不会算」说成「规则不好」。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from core import candidates as candidates_mod

#: 最多让模型给几条。多了反而挑不动，而且每条都要用户点一次才验
MAX_CANDIDATES = 3
#: 给模型的 DOM 大纲行数（与 core/repair/evidence 同口径）
MAX_OUTLINE_LINES = 90
#: App 实测值带几条进提示词——它是「正确长什么样」的锚点
MAX_APP_VALUES = 8
#: 页面 HTML 的硬上限：提示词里只放大纲，但大纲本身也要先解析一遍，
#: 别让一个几 MB 的页面把这次请求变成一次内存事故
MAX_HTML_CHARS = 400_000

#: 候选验收只允许触碰书源规则组，不能借这个入口改元数据或请求上下文。
CANDIDATE_RULE_GROUPS = ("ruleSearch", "ruleToc", "ruleContent", "ruleExplore", "ruleBookInfo")


def patch_candidate(source: Dict[str, Any], field: str, rule: str) -> Dict[str, Any]:
    """复制源并只替换一个规则字段，供候选验收使用。

    这是一次性的验证输入，不修改调用方的 ``source``，也不允许候选改写源元数据、请求头、
    登录配置或其他规则组。``@js:`` 等规则在这里原样保留——它们只能由真实引擎验收，
    不能在这个边界被提前当成非法规则。
    """
    if not isinstance(source, dict):
        raise ValueError("source 必须是对象")
    if not isinstance(field, str) or field.count(".") != 1:
        raise ValueError("候选字段必须是 ruleGroup.field")
    group, name = (part.strip() for part in field.split(".", 1))
    if group not in CANDIDATE_RULE_GROUPS:
        raise ValueError("候选字段不是规则组：%s" % group)
    if not name:
        raise ValueError("候选字段名不能为空")
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError("候选规则不能为空")

    patched = dict(source)
    current_group = source.get(group)
    if current_group is not None and not isinstance(current_group, dict):
        raise ValueError("源字段 %s 不是对象" % group)
    patched[group] = dict(current_group or {})
    patched[group][name] = rule.strip()
    return patched


_STEP_OF_TARGET = {
    "search": "search",
    "explore": "explore",
    "info": "bookUrl",
    "toc": "toc",
    "content": "content",
}


def verify_candidate(source: Dict[str, Any], field: str, rule: str,
                     target: str, query: str, timeout: int = 60,
                     runner: Any = None) -> Dict[str, Any]:
    """用本机真实引擎验收一个候选规则，不保存或推送临时源。

    ``runner`` 是测试注入点，签名为 ``runner(source, key, timeout)``，生产默认使用
    ``core.jvm_debug.run_jvm_debug``。只评价目标步骤；整链其他步骤失败不能把这条候选
    的结论偷换掉。真实引擎不可用、目标步骤缺失和明确失败分别返回不同状态。
    """
    if not isinstance(target, str) or target not in _STEP_OF_TARGET:
        raise ValueError("未知的验收目标：%s" % (target or "未提供"))
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("timeout 必须是正整数")

    patched = patch_candidate(source, field, rule)
    from core.debug_keys import build_key
    key = build_key(target, query, keyword=query)
    if runner is None:
        from core.jvm_debug import run_jvm_debug
        runner = lambda src, run_key, run_timeout: run_jvm_debug(  # noqa: E731
            src, key=run_key, timeout=run_timeout)

    try:
        result = runner(patched, key, timeout) or {}
    except Exception as exc:
        return {
            "status": "engine_unavailable",
            "engine": {"status": "unavailable", "channel": "jvm",
                        "reason": "%s: %s" % (type(exc).__name__, exc)},
            "target": target, "step": _STEP_OF_TARGET[target], "key": key,
        }

    if not isinstance(result, dict):
        return {
            "status": "engine_unavailable",
            "engine": {"status": "unavailable", "channel": "jvm",
                        "reason": "引擎返回不是对象"},
            "target": target, "step": _STEP_OF_TARGET[target], "key": key,
        }
    step_name = _STEP_OF_TARGET[target]
    step = next((item for item in (result.get("steps") or [])
                 if isinstance(item, dict) and item.get("name") == step_name), None)
    if step is None:
        reason = str(result.get("error") or result.get("code_text") or
                     "引擎没有返回目标步骤")
        return {
            "status": "engine_unavailable",
            "engine": {"status": "unavailable", "channel": "jvm", "reason": reason},
            "target": target, "step": step_name, "key": key,
        }

    ok = bool(step.get("ok"))
    empty_field = _empty_hit_reason(step)
    verified = ok and not empty_field
    out = {
        "status": "verified" if verified else "rejected",
        "engine": {
            "status": "pass" if verified else "fail", "channel": "jvm",
            "step": step_name, "detail": str(step.get("detail") or ""),
            "count": len(step.get("values") or []),
            "samples": [str(v) for v in (step.get("values") or [])[:3]],
        },
        "target": target, "step": step_name, "key": key,
    }
    if empty_field:
        # **整步通过不等于这个字段取到了东西**：搜索步的 verdict 来自事件流
        # （`◇书籍总数:N` 与页解析完成）。把 `bookList` 换成一个不存在的 class，
        # 站点自己的搜索页照样出结果、步照样 pass——实测（2026-10-08，`去读书网`）
        # 「不存在的 class」拿到过 `verified`。所以补这条**字段级**的零命中判据。
        out["engine"]["reason"] = ("整步跑通了，但这一个字段命中 0 条（%s）——它没取到东西"
                                   % empty_field)
    return out


#: 引擎自己报的「这一字段命中 0 条」形状。**只认已知的零命中标记**：判据窄一点，
#: 宁可漏判（那样只是退回「整步通过」），也不能把引擎说的别的东西当零命中。
_EMPTY_HIT_RES = (
    re.compile(r"总数[:：]\s*0(?!\d)"),
    re.compile(r"解析结果为空"),
    re.compile(r"未找到章节链接"),
)


def _empty_hit_reason(step: Dict[str, Any]) -> str:
    """这一个字段是不是命中 0 条（是则返回引擎原文，否则空串）。

    ``verify_candidate`` 判的是**整步**：搜索步的 verdict 由事件流决定
    （`◇书籍总数:N` + 页解析完成），换掉 `bookList` 之后站点自己的搜索页照样出结果。
    字段级的「命中 0 条」是唯一能一眼看出的反证，所以单独判一次。
    """
    text = "；".join([str(step.get("detail") or "")]
                    + [str(n) for n in (step.get("notes") or [])])
    for rx in _EMPTY_HIT_RES:
        m = rx.search(text)
        if m:
            return m.group(0)
    return ""


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
2. `@js:` / `<js>` / `@xpath:` 是允许的：候选会由本机 App 引擎验收。用它们时在 why 里
   说明依赖什么（全局对象 / 登录态 / 接口），因为取不到材料的那次会判失败。
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


_FOCUS_SHORTHAND = {"class": ".", "id": "#"}

#: `class.a.b` / `tag.div.x` 这类简写里，第一个点之前是**前缀**、之后是名字
_FOCUS_LEGADO_RE = re.compile(r"^(class|id|tag)\.([^.@##:]+)")
#: `tag.a.x` 要退化成 `a`；`class.a.b` 要退化成 `.a`
_FOCUS_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*")


def focus_selector(rule: str) -> str:
    """把一条 Legado 规则的首段转成 CSS 选择器（给 ``dom_outline(select=)`` 用）。

    ``class.x`` / ``id.x`` / ``tag.a`` 是 Legado 简写，直接丢给 BeautifulSoup 一个都
    匹配不到。**只认这三种前缀**，别的一律给空串让大纲退回整篇：这里只是「给模型看
    哪块 DOM」的取景框，不需要一个完整的规则解释器——认错的代价是提示词里少一段
    相关内容，而不是给错规则（选候选与验收都不经过这里）。
    """
    rule = str(rule or "").strip().split("##")[0].split("@")[0].strip()
    m = _FOCUS_LEGADO_RE.match(rule)
    if not m:
        # 已经是 CSS 选择器（`.cls .item` / `#id`）就原样用
        return rule if rule[:1] in (".", "#") else ""
    prefix, rest = m.group(1), m.group(2)
    if prefix == "tag":
        return _FOCUS_NAME_RE.match(rest).group(0) if _FOCUS_NAME_RE.match(rest) else ""
    return _FOCUS_SHORTHAND[prefix] + rest


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
#: 领先不到这个倍数就不猜（并列时交给模型）
_LEAD_RATIO = 2
#: 超过这个长度的「样本」不是可比的值（容器类候选会给一整块列表文本）
_MAX_SAMPLE_CHARS = 120


def _norm(value: Any) -> str:
    """比对用的归一化：去掉全部空白。

    事件流的 ``┌└◇`` 标记由**前端**剥掉（它更清楚自己发出去的是什么），
    这里不猜第二遍。
    """
    return re.sub(r"\s+", "", str(value or ""))


def _sig(values: List[str]) -> str:
    """一组值的指纹，用来判「两条候选取到的是不是同一批值」。

    用摘要而不是原值是怕大：正文那类一次就是几万字，而这里只做相等比较。
    """
    return hashlib.sha1("\x00".join(values).encode("utf-8")).hexdigest()[:12]


def _samples_of(candidate: Any) -> List[str]:
    """候选自己报的样本值（界面上摆给用户看的那几个）。

    候选（算法/点选那两族）与它的样本是**同一份材料**：`core.candidates` 造候选时
    就是按这些值分组的，所以拿样本比对不需要再解释一遍规则文本。模型给的那族没有
    样本，返回空列表——调用方要把它读成「没参与比对」。

    **超长样本丢掉**：容器类候选的样本可能是整块列表文本（实测一次 1000+ 字，
    里面装着全部书名），它和任何基线都「包含」得上，会把分数抬到几十却什么也没说明。
    这一层只认「短样本完全相等」，长文本不是可比的值。
    """
    if isinstance(candidate, dict):
        raw = candidate.get("samples") or []
    else:
        raw = []
    out = []
    for v in raw:
        text = _norm(v)
        if text and len(text) <= _MAX_SAMPLE_CHARS:
            out.append(text)
    return out


def preselect(candidates: List[Any], app_values: List[str], html: str,
              step: str, source_type: int = 0, kind: str = "") -> Dict[str, Any]:
    """**先用程序挑一遍**：拿 App 实测到的值当基准，看哪条候选报的样本就是那批。

    为什么值得先来这一遍：AI 那条路要花钱，而「哪条候选对」多数时候**可判**——
    连 App 调试时 App 自己取到过值（``steps[].values``，比如书名的真实文本），
    那就是免费的 ground truth。

    **比的是候选自己报的样本**，不是我们另跑一遍规则算出来的值：样本就是用户
    在卡片上看到的那些，口径一致；也就没有第二套规则解释器。
    候选**没报样本**时（列表族的候选只给容器）退回页面那一侧：
    `core.candidates.sample_values` 按候选自己的取样范围收「页面上像 App 实测值的那些」。
    两边都比不出来就如实说，**不许读成「取不到值」**（AGENTS #4：不把「我们比不了」
    说成「规则不好」）。

    挑不出来时如实说，不猜：没有基准（不是 App 实测的结果，或 App 那一步本来就取不到
    值）、候选报的样本都对不上、多条候选并列。
    """
    rules = [c for c in (candidates or []) if str((c or {}).get("rule") if isinstance(c, dict)
                                                  else c or "").strip()]
    basis = [x for x in (_norm(v) for v in (app_values or [])) if x]
    out: Dict[str, Any] = {"picked": None, "ranked": [], "need_ai": True,
                           "basis": "app" if basis else "", "reason": ""}
    if not rules:
        out["reason"] = "没有候选可挑"
        return out
    if not basis:
        out["reason"] = ("没有 App 实测值作基准。先连 App 或本机引擎把这一步跑一遍："
                         "当前规则自己取到的值不能当基准（那是它的产物）")
        return out

    #: 页面上像 App 实测值的那几个，作为候选**没报样本**时的比对材料（列表步的候选
    #: 只给容器、不报样本，而 App 在列表步取到的正是条目里的书名）。拿不到就是拿不到，
    #: 调用方按「这一页上比不了」如实说。同样丢掉超长项：容器整块文本会与任何基线
    #: 「包含」得上（实测一次 1000+ 字，把分数抬到 76）。
    on_page = [x for x in (_norm(v) for v in
                           candidates_mod.sample_values(html or "", kind, basis))
               if len(x) <= _MAX_SAMPLE_CHARS]

    ranked = []
    for item in rules:
        rule = str(item.get("rule") if isinstance(item, dict) else item).strip()
        samples = _samples_of(item)
        if not samples:
            # 候选自己没报样本（列表族）：退回「页面上像 App 实测值的那些」
            samples = on_page
        strong = sum(1 for a in basis for x in samples if a == x)
        weak = sum(1 for a in basis for x in samples
                   if a != x and len(a) >= 2 and (a in x or x in a))
        ranked.append({"rule": rule, "score": strong * _STRONG + weak * _WEAK,
                       "strong": strong, "weak": weak,
                       "samples": list(samples[:3]), "count": len(samples),
                       "sig": _sig(samples)})
    ranked.sort(key=lambda r: (-r["score"], r["rule"]))
    out["ranked"] = [{k: v for k, v in r.items() if k != "sig"} for r in ranked]

    scored = [r for r in ranked if r["score"] > 0]
    if not scored:
        if not on_page and not any(r["count"] for r in ranked):
            # 这一页上根本没有像 App 实测值的材料——这是「比不了」，不是「对不上」
            out["reason"] = ("这一页上找不到 App 实测的那些值（材料可能不是同一份），"
                             "没法按样本比：点一次 AI，或直接用引擎验")
            return out
        out["reason"] = "候选报的样本和 App 实测值都对不上"
        return out
    if not any(r["strong"] for r in scored):
        # 只有「包含」没有「完全相等」→ 基线里没有真正的值，撑不起挑一条的结论。
        # App 那一步取到的是事件流水（带 ┌└◇≡ 的结构行、URL、导航文本）时就会这样：
        # 随便一条候选都能撞上几处，报「对上 N 条」等于把人引到错的规则上。
        out["reason"] = ("App 这一步的值里没有和候选样本完全相等的（看着像事件流水或"
                         "统计行，不是取到的值）——不替你猜，点一次 AI 或直接用引擎验")
        return out

    top = scored[0]
    tied = [r for r in scored if r["score"] == top["score"]]
    if len(tied) > 1:
        # 报**同一批样本**的候选是**等价**的（如 `.item@tag.a@text` 与
        # `.item@tag.h3@text`，a 就在 h3 里）——那不算歧义，挑更简洁的那条即可；
        # 否则「页面上有一堆等价写法」会让这一层形同虚设。
        if len({r["sig"] for r in tied}) == 1:
            top = min(tied, key=lambda r: (str(r["rule"]).count("@"),
                                           len(str(r["rule"])), str(r["rule"])))
            out["picked"] = top
            out["need_ai"] = False
            out["reason"] = ("%d 条候选报的是同一批样本（等价），取了更简洁的那条；"
                             "App 取到的值就在它选中的 %d 条里"
                             % (len(tied), top["strong"] + top["weak"]))
            return out
        out["reason"] = ("多条候选都能对上、报的还不是同一批样本（%d 分 / %d 分），"
                         "不替你猜" % (top["score"], tied[1]["score"]))
        return out
    if len(scored) > 1 and scored[1]["score"] * _LEAD_RATIO > top["score"]:
        out["reason"] = ("另一条候选也能对上（%d 分 / %d 分），不替你猜"
                         % (top["score"], scored[1]["score"]))
        return out
    out["picked"] = top
    out["need_ai"] = False
    out["reason"] = ("App 取到的值就在它报的样本里（对上 %d 条）"
                     % (top["strong"] + top["weak"]))
    return out


async def suggest(ctx: Dict[str, Any], client: Any = None,
                  temperature: Optional[float] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """跑一轮提议，返回给前端直接渲染的结构。

    ``client`` 可注入（离线测试用假客户端）。返回体恒有 ``candidates`` /
    ``llm`` / ``error`` 三个键：``llm`` 用 ``ok`` / ``off``（没配模型）/
    ``error`` 三态，前端据此给不同的话，**不能都显示成「没有候选」**。

    ``dry_run=True`` 时**一个模型请求都不发**：只跑免费的 ``preselect``（外加
    登录墙判断），``llm`` 记为 ``dry_run``。前端先来这一趟，挑得出来就不花钱。

    模型给的候选**不带本地判定**（只有 ``rule`` / ``why``）：验收是引擎那一趟的事，
    在界面上由用户点击触发（``/rules/verify-candidate``）。
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
                               int(ctx.get("source_type") or 0),
                               str(ctx.get("kind") or "")),
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
        # 只带规则与理由：**这条链不做本地判定**。候选能不能用由用户点击后的
        # 「验证并应用」（本机引擎）说了算（AGENTS #3）。
        out["candidates"].append({"rule": rule,
                                  "why": str(item.get("why") or "").strip()[:200]})
        if len(out["candidates"]) >= MAX_CANDIDATES:
            break
    if not out["candidates"]:
        out["error"] = "模型没有给出可用的规则（原文前 200 字：%s）" % str(text)[:200]
    return out

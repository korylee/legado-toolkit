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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
#: 每条候选数据源在提示词里带几个键名（真实站点一个对象十几个键，全列会淹掉别的段）
RUNTIME_PROMPT_KEYS = 20
#: 源里已有 webJs 时，提示词里带多长（超了要自报截断）。脚本本身可能很长，
#: 而它只是**线索**不是材料——600 字符足够看清它用了哪个变量/键。
KNOWN_WEBJS_MAX = 600
#: 页面函数名的「像解密/编码」判据：命中才把它当解密函数列给模型。
#: 只认自证是这类用途的词（与 `core.page_layer.PAGE_MARKERS` 收 CryptoJS 同一立场）——
#: 宽判据（如含 `get` / `parse`）会把一堆取数函数也说成解密。
DECRYPT_NAME_RE = re.compile(r"decrypt|encrypt|decode|decodeURI|base64|aes|des|rc4|rsa",
                             re.IGNORECASE)
#: 「图片不在 DOM 里」的两道门槛：图片总数够多、带 src 的占比够低。
#: 实测（2026-10-22 口袋漫画正文页）：139 个 img、只有 1 个带 src、图片地址全在页面脚本里
#: ——这种页面的 DOM 大纲对「取正文」没有可用信息。
OUTLINE_IMAGE_MIN = 10
OUTLINE_SRC_RATIO = 0.1

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
    # 其余段落全走片段（与批量路径同一份实现）：这样单字段路径也能拿到
    # 页面全局候选 / 已有 webJs / 按材料分档的大纲——此前它只有大纲与实测值。
    L.extend(_sections(ctx))
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


def outline_plan(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """这次要不要给 DOM 大纲，给哪一种（纯函数，可离线测）。

    三档，判据全是**已有的材料事实**：
      - ``skip``：材料根本不是 DOM（`material_kind == "webjs_output"`）。有 `webJs` 时
        引擎交回的「整页」是那段脚本的返回值（实测 27150 字节、137 行图片地址、无标签），
        压出来的大纲是垃圾——喂给模型等于让它在一堆 URL 上找选择器。
      - ``stats``：是 DOM，但图片不在 DOM 属性里（`images_with_src / images` 很低）。
        实测该页 139 个 `img` 只有 1 个带 `src`，正文靠页面脚本填——这时 90 行大纲同样
        没有可用信息，但**统计数字本身是判据**（「图不在 DOM」），所以给一行代替。
      - ``full``：其余情况照旧压大纲（文本页正是这么用的）。
    """
    from core.material import can_fill_dom

    if not can_fill_dom(str(ctx.get("material_kind") or "")):
        return {"mode": "skip"}
    stats = ctx.get("page_stats")
    if isinstance(stats, dict):
        images = stats.get("images")
        with_src = stats.get("images_with_src")
        if (isinstance(images, int) and isinstance(with_src, int)
                and not isinstance(images, bool) and not isinstance(with_src, bool)
                and images >= OUTLINE_IMAGE_MIN
                and with_src / images < OUTLINE_SRC_RATIO):
            return {"mode": "stats", "images": images, "with_src": with_src,
                    "links": stats.get("links")}
    return {"mode": "full"}


def _outline_section(ctx: Dict[str, Any]) -> List[str]:
    """大纲那一段（三档共用一份实现：两个 builder 都不许各写一遍）。

    为什么要分档：材料不是 DOM 时压大纲是**喂垃圾**；图片不在 DOM 属性里时 90 行大纲
    没有可用信息，但「图多、带地址的少」这个统计本身就是判据。
    """
    plan = outline_plan(ctx)
    mode = plan["mode"]
    if mode == "skip":
        return ["", "【页面材料说明】这一步的材料是页面脚本（webJs）的输出，不是 HTML——"
                    "没有 DOM 结构可看，选择器类写法在这里不适用。"]
    if mode == "stats":
        links = plan.get("links")
        return ["", "【页面统计（不给 DOM 大纲）】图片 %d 个，其中带 src 的只有 %d 个%s。"
                    "图多而带地址的极少 = 图片地址不在 DOM 属性里，多半由页面脚本填，"
                    "优先看下面的页面全局数据。"
                % (plan["images"], plan["with_src"],
                   "；链接 %d 个" % links if isinstance(links, int) else "")]
    return ["", "【页面 DOM 大纲（缩进表示层级）】",
            _outline(ctx.get("html") or "", ctx.get("focus") or "")]


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

    判定表在 ``core.jvm_health``（``is_login_wall`` → ``classify_http_status``），
    这里不重写一份。**这条链路为什么也需要它**：抓到的是登录页时，模型看到的
    页面**不是 App 看到的那份**（App 带登录态），它提的建议既验不了也修不对——
    用户花的是冤枉钱。所以要把话说在前面，而不是等他点完再看到「取不到值」。
    """
    from core.jvm_health import is_login_wall

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


SYSTEM_PROMPT_MANY = """你是 Legado（阅读 App）书源的规则专家。用户正在调试**一步**规则，一次给出多个字段的候选规则。只输出 JSON，不要解释。

Legado 规则语法（`@` 分段，前面是选择器，最后一段是取值动作）：
- 简写：class.xxx 等于 .xxx；id.xxx 等于 #xxx；tag.a 等于 a
- 链式：class.book-list@tag.li@tag.a@href
- 索引：.0 取第一个，.-1 取最后一个
- 取值动作：text / textNodes / ownText / html，或任意属性名（href、src、data-original）
- 正则后处理：规则##正则##替换（支持 $1 反向引用）
- JSON 接口：$.data.list[*].name

输出格式（严格 JSON；每个字段最多 3 条候选，按可能性从高到低；只给要的字段，一个字段拿不准就给空 candidates 加一句 reason，不要影响别的字段）：
{"fields":[{"field":"字段名","candidates":[{"rule":"规则文本","why":"一句话说明为什么"}],"reason":"该字段的整体判断"}]}

硬性要求：
1. 规则里用到的 class / id 必须**真实存在于**我给的 DOM 大纲里，不许臆造。
2. `@js:` / `<js>` / `@xpath:` 是允许的：候选会由本机 App 引擎验收。用它们时在 why 里
   说明依赖什么（全局对象 / 登录态 / 接口），因为取不到材料的那次会判失败。
   **两个环境别搞混**（实测代价：写错必取空，而 verdict 只会说「没解析完成」）：
     - `webJs`（页面脚本）跑在**页面里**，能读页面的全局变量（如 `params`）；
     - 取值规则（如 `content`）跑在**规则环境**里，只能读 DOM 选择器或 `result`
       ——`result` 就是 webJs 的产出；页面变量在那里一律 `undefined`
       （实测 `@js:typeof params` → `undefined`）。
     所以「内容在页面变量里」这件事要写成**两条规则**：webJs 取出来 → content 用 `result` 包装。
     若这一步现在没有 webJs，就直说需要先写一条，不要给出引用页面变量的 content 候选。
3. 一条候选只写规则文本本身，不要带字段名、不要写 JSON 以外的内容。
"""


def _runtime_line(rt: Dict[str, Any]) -> str:
    """一条候选数据源 → 一行（名字 · 类型 · 长度/键名）。只写形状，不写值。"""
    name = str((rt or {}).get("name") or "")
    type_name = str((rt or {}).get("type") or "").replace("[object ", "").rstrip("]")
    line = "%s（%s" % (name, type_name or "?")
    count = (rt or {}).get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        line += "，%d 条" % count
    keys = (rt or {}).get("keys")
    if isinstance(keys, list) and keys:
        line += "，键：%s" % "、".join(str(k) for k in keys[:RUNTIME_PROMPT_KEYS])
    # 函数签名：解密站要**调用**页面上的解密函数，参数个数决定怎么写
    fns = (rt or {}).get("functions")
    if isinstance(fns, list) and fns:
        sigs = ["%s(%d 参)" % (f.get("name"), int(f.get("arity") or 0))
                for f in fns[:8] if isinstance(f, dict) and f.get("name")]
        if sigs:
            line += "，函数：%s" % "、".join(sigs)
    return line + "）"


def _var_line(name: str, rec: Dict[str, Any]) -> str:
    """`params` 的一个键 → 一行：**它装的是什么**（类型 / 条数 / 首项样例）。

    为什么必须有它（2026-10-23 实测）：同一页 `chapter_images` 是 131 条真地址、
    `chapter_local_images` 是空数组、`images_base64` 是 `false`——只给键名时三个看起来
    一样，模型只能盲选；而选错了**事后也验不出来**（都能「跑出内容」）。
    """
    type_name = str(rec.get("type") or "").replace("[object ", "").rstrip("]")
    line = "  - %s：%s" % (name, type_name or "?")
    n = rec.get("n")
    if isinstance(n, int) and not isinstance(n, bool):
        line += "，%d 条" % n if type_name.lower() in ("array", "object") else "，%d 字符" % n
    first = str(rec.get("first") or "")
    if first:
        line += "，样例：%r" % first[:70]
    return line


# ---------------------------------------------------------------- 提示词段落
#
# 段落顺序与「哪些段会出」的唯一判据都在 :data:`SECTIONS`：**每个段自己判**该不该出
# （事实缺席就返回 None），顺序错了不会报错、只会让每次提问都全量计费（前缀缓存）。
# 加一种页面结构 / 解析方式 = 在 `SECTIONS` 里加一个函数，基础提示词与两个 builder 都不动。


def _sec_fields(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """要的字段（批量形态）。单字段路径的标题行已点名字段，这段缺席。"""
    fields = ctx.get("fields")
    if not isinstance(fields, list) or not fields:
        return None
    L = ["", "【要的字段】"]
    for f in fields:
        if not isinstance(f, dict):
            continue
        L.append("- %s：%s" % (f.get("field") or "", f.get("want_label") or ""))
        if f.get("kind") == "script":
            # 页面脚本这一族的**定义**（叶子成员见 `core.autofill.FILL_SPECS`）：
            # 不写清「跑在哪、结果去哪」，模型给出的往往是取值规则或一段注释
            L.append("  （页面脚本：跑在页面环境里，能读页面变量；它的结果成为这一步的响应体，"
                     "由取值规则通过 `result` 读）")
    # 这一段有列表容器时（`kind == "list"`），其余字段是**条目内规则**——这是
    # `BookList` 的两段式语义（先按容器切条目、再逐条目取名/取链接）。不说明的话
    # 模型一律给从整页起的完整链，而那条链在每个条目里又去找一次容器，取不到东西
    # （实测 2026-10-26：快验给完整链「通过」，落源后引擎报「列表为空」）。
    if any(f.get("kind") == "list" for f in fields if isinstance(f, dict)):
        L.extend(["",
                  "**列表容器之外的字段都是「条目内规则」**：相对容器选中的那个条目元素取"
                  "（如 `tag.a@href`），**不要**再写容器那一段前缀；"
                  "写成从整页起的完整链，引擎会在每个条目里重新找一遍容器，取不到东西。"])
    return L


#: 页面内容在页面变量里时，**产出顺序**。这是「该产出什么」的判据，不是措辞：
#: `webJs` 是内容的来源（页面变量只在那个环境里可读），`content` 只是把它的产出包成
#: 正文。先有 `webJs` 才谈得上 `content`——顺序反了，模型只能给一条读 `result` 的规则，
#: 而那时还没有 `result`（实测：没有 webJs 时同一条 content 把整页 HTML 逐行包成 img，照样 pass）。
SCRIPT_ORDER_NOTE = (
    "**产出顺序**：这一步的内容在页面变量里，先写 `webJs` 把内容取出来"
    "（`result = params.<键>.join('\\n')`），再写 `content` 读 `result` 并包装。"
    "没有 `webJs` 时不要给引用页面变量的 `content` 候选——那个环境里它们一律是 `undefined`。")


def _sec_script_step(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """这一步有两个字段族（页面脚本 + 取值规则）时，点明先后关系与解密场景。

    **为什么必须是这一段而不是在 system prompt 里加句子**：顺序取决于「这一步有哪些
    字段」，只有 ctx 知道；而且这条判据只在 `webJs` 与 `content` 同组时成立。
    """
    fields = ctx.get("fields")
    kinds = {f.get("kind") for f in (fields or []) if isinstance(f, dict)}
    if "script" not in kinds or len(kinds) < 2:
        return None
    L = ["", "【产出顺序】", SCRIPT_ORDER_NOTE]
    runtimes = ctx.get("runtimes")
    if isinstance(runtimes, list) and runtimes:
        L.append("若内容是被加密的（页面里有长 base64 负载 / CryptoJS / 解密调用），"
                 "**页面自己已经把解密写在脚本里了**：读你列出来的变量（如解密后的数组），"
                 "不要在 webJs 里重写解密算法——重写必错，而错法看不出来（解出来是乱码）。")
    return L


def _sec_js_globals(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """页面全局候选（渲染后枚举，只有形状）。变量名不可猜——列事实，判断给模型。"""
    runtimes = ctx.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        return None
    L = ["", "【页面里的全局数据（渲染后枚举，只有形状没有值）】",
         "这些是页面脚本自己挂的变量。判断哪个键装的是要取的内容。",
         "**作用域是本段的要害**：这些变量**只在页面脚本（webJs）里可读**——"
         "取值规则（content）跑在另一个环境，那里它们一律是 `undefined`"
         "（实测：`@js:typeof params` 在 content 里返回 `undefined`）。",
         "所以要取这类内容，正确形态是两条规则分工：",
         "  1) webJs：`result = params.<键>.join('\\n')`（把变量里的内容取出来）",
         "  2) content：`@js:result.split('\\n')...`（只读 `result`，那是上一步的产出）",
         "**地址列表要包成 `<img>`**：若取到的是图片地址逐行列表，content 必须把它们包起来"
         "（`result.split('\\n').filter(...).map(x => '<img src=\"'+x+'\">').join('')`）——"
         "只写 `@js:result` 的话正文是一串裸地址文本，阅读器里显示的是文字不是图"
         "（实测两者 verdict 都是 pass，差别只在这层包装）。",
         "若本步现在还没有 webJs，就直说「需要先写一条 webJs 把内容取出来」，"
         "不要给一条在 content 里引用页面变量、必然取不到的候选。"]
    for rt in runtimes:
        L.append("- %s" % _runtime_line(rt))
    # `params` 的**键值形状**：光有键名时所有键看起来一样，模型只能盲选
    page_vars = ctx.get("page_vars")
    if isinstance(page_vars, dict) and page_vars:
        L.append("")
        L.append("其中页面变量里各键装的是什么（样例只是形状，不是完整值）：")
        for name, rec in list(page_vars.items())[:RUNTIME_PROMPT_KEYS]:
            if isinstance(rec, dict):
                L.append(_var_line(str(name), rec))
        L.append("**按样例挑键**：空数组 / `false` / 空串的那些键在这一页没有内容；"
                 "要的是「有内容且像地址或正文」的那一个。")
    # 解密站的第三种情况：结果没挂在变量上，但**解密函数在页面上**。
    # 只有真的采到函数签名时才出这段——没有函数时谈「调用它」是误导。
    decrypt_fns = [f for rt in runtimes if isinstance(rt, dict)
                   for f in (rt.get("functions") or [])
                   if isinstance(f, dict) and DECRYPT_NAME_RE.search(str(f.get("name") or ""))]
    if decrypt_fns:
        L.append("")
        L.append("【页面里有解密/编码函数（要调用，不是重写）】")
        for f in decrypt_fns[:8]:
            L.append("- %s（%d 个参数）" % (f.get("name"), int(f.get("arity") or 0)))
        L.append("这些函数**页面自己已经加载好了**：webJs 里直接调用它们，"
                 "把参数从页面变量或 DOM 里取出来传进去。"
                 "**不要自己重写解密算法**——写错了不会报错，只会解出乱码，"
                 "而那时你分不清是算法错还是密钥错。")
        L.append("若看不出该传什么参数，就直说「看不出解密函数的入参」——"
                 "不要猜一个，猜错会解出乱码且同样看起来「跑通了」。")
    dropped = int(ctx.get("runtime_dropped") or 0)
    if dropped > 0:
        L.append("（另有 %d 个更小的候选未列出）" % dropped)
    return L


def _sec_known_webjs(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """源里已有的 webJs：比枚举全局更准的「数据在哪」线索（变量名是源作者选的）。"""
    known = str(ctx.get("known_webjs") or "").strip()
    if not known:
        return None
    L = ["", "【源里已有的页面脚本（webJs，不是本步的材料）】", known[:KNOWN_WEBJS_MAX]]
    if len(known) > KNOWN_WEBJS_MAX:
        L.append("（脚本已截断：只给了前 %d 个字符）" % KNOWN_WEBJS_MAX)
    L.append("说明：这段脚本是源作者写的，执行结果会成为本步的响应体。"
             "若它已经取到了要的内容，优先沿用它里面的变量名/键名；"
             "若它引用的键在本页并不存在，直接指出来。")
    return L


def _sec_known_container(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """本段**已定的列表容器**：其余字段是相对它取的条目内规则。

    给出去的理由不是「约束模型」而是**补事实**：模型看不到源里刚生成的那一格
    （`_propose_group` 的 `source` 是调用时的快照），它只能从大纲猜「从哪起」。
    猜对时给出 `tag.a@href`，猜偏时给出 `#chapterList_ul_1 li a@href`——后者在条目
    元素里重找一个祖先，引擎直接取空（实测 2026-10-26 目录页）。
    """
    known = ctx.get("known_container")
    if not isinstance(known, dict) or not known:
        return None
    L = ["", "【本段已定的列表容器】"]
    for path, rule in known.items():
        text = str(rule or "").strip()
        if text:
            L.append("- `%s` = `%s`" % (path, text[:120]))
    if len(L) <= 2:
        return None
    L.append("列表容器就是「一个条目一条」的那条规则。**其余字段都相对条目元素取**："
             "直接写条目里面的东西（如 `tag.a@href`、`.title@text`），**不要**再写"
             "容器那一段或任何祖先定位——那些在条目里找不到，引擎会取到空。")
    return L


def _clean_values(raw: Any) -> List[str]:
    """取实测值：**先判原始值再转字符串**。

    原实现写的是 `str(v).strip()` 作条件，于是 `None` 会变成字面量 `"None"` 混进提示词
    ——模型看到的「实测值」里有个字符串 `None`，那是噪音，还可能被它当成模版抄进规则。
    """
    out: List[str] = []
    for v in raw if isinstance(raw, list) else []:
        if v is None:
            continue
        text = str(v).strip()
        if text:
            out.append(text[:120])
    return out


def _value_rows(ctx: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """实测值，两种形态归一：批量的逐字段（fields[].app_values）、单字段的平铺。"""
    if isinstance(ctx.get("fields"), list) and ctx["fields"]:
        rows = []
        for f in ctx["fields"]:
            if not isinstance(f, dict):
                continue
            vs = _clean_values(f.get("app_values"))
            if vs:
                rows.append((str(f.get("field") or ""), vs))
        return rows
    vs = _clean_values(ctx.get("app_values"))
    return [("", vs)] if vs else []


def _sec_app_values(ctx: Dict[str, Any]) -> Optional[List[str]]:
    rows = _value_rows(ctx)
    if not rows:
        return None
    L = ["", "【App 实测取到的值（正确的规则应当取到形似的东西）】"]
    for name, vs in rows:
        if name:
            L.append("%s：" % name)
        for v in vs[:MAX_APP_VALUES]:
            L.append("- " + v)
    return L


def _sec_diagnosis(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """易变段：当前规则 / 当前表现 / 已知问题。**恒在最后**（前缀缓存约束）。"""
    if not (ctx.get("rule") or ctx.get("replay_note") or ctx.get("diagnosis")):
        return None
    L = ["", "【当前规则】%s" % (ctx.get("rule") or "（空）")]
    if ctx.get("replay_note"):
        L.append("【当前表现】%s" % ctx["replay_note"])
    diag = [str(d) for d in (ctx.get("diagnosis") or []) if str(d).strip()]
    if diag:
        L.append("")
        L.append("【已知的问题】")
        for d in diag:
            L.append("- " + d)
    return L


def _sec_header(ctx: Dict[str, Any]) -> Optional[List[str]]:
    """请求头（源级）——**只在有「请求被拒」的事实**时出。

    触发条件（两个都来自已有判据，不新增探测）：
      - 本段材料是拦截页（`core.quality.interstitial_marker`）；
      - 或**别的段**被拦（`ctx["step_health"]`）——`header` 是源级字段，反爬常只拦某一段。

    **不拿图片可达性当判据**（实测 2026-10-23）：口袋漫画 131 条图片 URL，六种请求头
    组合（裸/UA/Referer/Origin…）全部 403，而用户在浏览器里也打不开——站点自己下发的
    签名已过期（`x-expires` 早于当天）。因此「图片打不开」不能推出「要加 header」，
    见 TODO `image-probe-is-not-a-verdict`。
    """
    from core.quality import interstitial_marker

    html = str(ctx.get("html") or "")
    step_name = str(ctx.get("step") or "")
    hit = interstitial_marker(html)
    health = ctx.get("step_health")
    blocked = ([name for name, code in health.items()
                if code in ("interstitial", "browser_error")]
               if isinstance(health, dict) else [])
    # 本段在体检里也被标成被拦时，以体检为准：两份事实都指向「材料不是站点」。
    # 只说「这一步正常」会把「别按这份材料写规则」这个关键提醒吞掉。
    if not hit and str(step_name) in blocked:
        hit = "（体检）"
    if not hit and not blocked:
        return None
    L = [""]
    if hit:
        L.append("【这段材料是反爬拦截页（命中「%s」）——不是站点本身】" % hit)
        L.append("先别写给这一步的取值规则：手里这份不是站点，按它写出来的规则会在 App 里取空。")
    else:
        L.append("【这一步正常，但**别的步骤**被拦了】")
    if blocked:
        L.append("被拦的步骤：%s。" % "、".join(blocked))
    L.extend(["",
              "`header` 是**源级**请求头，写 JSON："
              '`{"Referer": "https://站点首页/", "User-Agent": "..."}`；'
              "要动态算（带 Cookie / 签名）就写 `@js:` 或 `<js>`，表达式返回 JSON 字符串。"])
    return L


#: 段落顺序 = **前缀缓存约束的落点**：稳定段（字段/大纲/全局数据/已有脚本/实测值）在前，
#: 易变段（`_sec_diagnosis`：改一次规则就变）恒在最后。重排前先读 `build_prompt` 的 docstring
#: ——顺序错了不会报错，只会让每次提问都全量计费。
#:
#: **每个段自己判「该不该出」**（返回 None = 缺席），所以这里只是一张有序表：不另设
#: 「这次给哪几段」的清单。曾经有过一份（`prompt_plan`），它把每段的判据抄了第二遍
#: ——判据写两处必然漂，而且它除被自己调用外没人用。
SECTIONS: Tuple[Callable[[Dict[str, Any]], Optional[List[str]]], ...] = (
    _sec_fields,
    # 顺序段紧跟字段段：「先写哪个」依赖「这一步有哪些字段」，读者（模型）按顺序读
    _sec_script_step,
    _sec_header,
    _outline_section,
    _sec_js_globals,
    _sec_known_webjs,
    _sec_known_container,
    _sec_app_values,
    _sec_diagnosis,
)


def _sections(ctx: Dict[str, Any]) -> List[str]:
    """按 `SECTIONS` 顺序把非缺席的段拼起来（每段自带段前空行）。"""
    out: List[str] = []
    for section in SECTIONS:
        out.extend(section(ctx) or [])
    return out


def build_batch_prompt(ctx: Dict[str, Any]) -> str:
    """批量提议的提示词（纯函数，可离线测）。

    段落全走 :data:`SECTIONS`：与单字段路径同一份实现，两个 builder 只差标题行与
    system prompt 的输出格式。加一种页面结构 = 在 `SECTIONS` 里加一个函数，这里不动。

    段落顺序沿用 :func:`build_prompt` 的**前缀缓存约束**：稳定内容（这一步是什么、
    DOM 大纲）在前，字段与实测值在后——同一步里「提议 → 改规则 → 再提议」
    大纲一段是共用的，批量把这份共用从 N 次调用压成 1 次。
    """
    L: List[str] = []
    L.append("这一步：%s（一次给出下列字段的候选规则）"
             % (ctx.get("step_label") or ctx.get("step") or ""))
    L.extend(_sections(ctx))
    return "\n".join(L)


async def suggest_many(ctx: Dict[str, Any], client: Any = None,
                       temperature: Optional[float] = None) -> Dict[str, Any]:
    """一轮提议多个字段（同一步、同一取样类型的一族）：**一次调用共享 DOM 大纲**。

    逐字段问的代价不只是 N 倍 token：同一份 90 行大纲重复喂 N 遍，延迟也是 N 次
    往返（8 个字段 = 半分钟起）。返回体恒有 ``fields`` / ``llm`` / ``error``；
    ``fields`` 是 ``{field: [candidates]}``，**单字段失败不塌整批**——某个字段
    没给候选就在返回体里缺席，调用方读成「该字段没有候选」。只提议不判定
    （AGENTS #3）：每条候选落地前由调用方的 rule_try 自查。

    ``client`` 可注入（离线测试用假客户端）。

    返回体里带 ``prompt`` / ``response`` 原文（**排障要看的原件**）：调用方据此落
    ``llm.jsonl`` 账本。它们只描述刚刚这一次调用，不缓存、不跨调用复用。
    """
    from core.repair.llm import LLMClient, extract_json

    client = client if client is not None else LLMClient()
    out: Dict[str, Any] = {"fields": {}, "llm": "ok", "error": "",
                           "model": getattr(getattr(client, "config", None), "model", ""),
                           "usage": {}, "prompt": "", "response": ""}
    if not getattr(client, "enabled", False):
        out["llm"] = "off"
        out["error"] = "没有可用的模型：到「设置 → 模型」配置一个（或设 LEGADO_LLM_API_KEY）"
        return out
    prompt = build_batch_prompt(ctx)
    out["prompt"] = prompt
    try:
        text = await client.chat(SYSTEM_PROMPT_MANY, prompt, temperature=temperature)
    except Exception as e:
        out["llm"] = "error"
        out["error"] = "模型调用失败：%s: %s" % (type(e).__name__, e)
        return out
    out["response"] = str(text or "")
    out["usage"] = dict(getattr(client, "last_usage", None) or {})
    data = extract_json(text)
    if not isinstance(data, dict):
        out["llm"] = "error"
        out["error"] = "模型输出不是合法 JSON（原文前 200 字：%s）" % str(text)[:200]
        return out
    for item in data.get("fields") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if not field:
            continue
        seen = set()
        cands: List[Dict[str, str]] = []
        for c in item.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            rule = str(c.get("rule") or "").strip()
            if not rule or rule in seen:
                continue
            seen.add(rule)
            cands.append({"rule": rule, "why": str(c.get("why") or "").strip()[:200]})
            if len(cands) >= MAX_CANDIDATES:
                break
        out["fields"][field] = cands
    return out

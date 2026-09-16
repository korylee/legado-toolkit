# -*- coding: utf-8 -*-
"""
高并发联网校验核心。

使用 asyncio + aiohttp 异步并发探测每个书源的：
1. 域名连通性（GET 根路径，检查状态码与响应体特征）
2. 搜索可用性（若存在 searchUrl，构造一次搜索请求）
3. 反爬 / 登录 / 失效特征识别

性能设计：
- 单连接复用（keep-alive），信号量控制并发上限
- 短超时（默认 8s），快速失败，避免卡死
- 结果按源写入 NDJSON 缓存，可断点续跑
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from core.models import (
    Health, BookSourceRecord, ANTI_BOT_MARKERS, LOGIN_MARKERS,
    NOVEL_TEST_KEYWORDS, MANGA_TEST_KEYWORDS, TEST_TITLES, TOC_COMPLETE_THRESHOLD,
)
from core.urls import abs_url as _abs_url
from core.rules.replayer import (extract_all as apply_css_rule, extract_all_nodes,
                          parse_rule)
from core.loader import _normalize_url, fingerprint
# 判定口径的唯一来源（与「全链路试跑」共用，避免同源两判）。
# 依赖方向：checker → quality，quality 不依赖 replayer，这是刻意的。
from core import quality as Q
# 限速口径的唯一实现在 quality：checker（异步）与 fetch（同步）共用同一份。
# 早先它定义在本模块，而 fetch 也要用时就只能反向依赖 checker——lessons §十记过
# 这个坑（core 层不该反向依赖）。这里只是引用，不是第二份定义
from core.quality import rate_interval_ms
# 探测深度的合法取值只在 settings_store 定义一份（那边同时供设置接口的收敛用）。
# 依赖方向：checker → settings_store，反过去会把 aiohttp 拖进配置模块
from core.settings_store import DEFAULTS as _SETTINGS_DEFAULTS
from core.settings_store import (DEPTH_CONTENT, DEPTH_HOME, DEPTH_SEARCH,
                                DEPTH_TOC, PROBE_DEPTHS)
# 常见 User-Agent（规避简单 UA 拦截）的**唯一实现在 constants**，这里只引用——
# 本模块原来自己存了一份，已经和 constants 漂成两条（`Chrome/124.0.0.0` vs
# `Chrome/124.0`），而构建 / fetch / reclassify 读的都是 constants 那份。
# 与上面 rate_interval_ms 同一个道理：core 层不许有第二份定义
from core.constants import DEFAULT_UA

# 缓存版本 8：判定口径新增「验过搜索」这一维（见 is_cache_item_valid 的 search_probed）。
# 现在库里的条目没有 search_probed 字段，item.get("search_probed") 为假——对开着
# 搜索探测的用户来说，所有历史 OK 缓存都会被判为"没验过搜索"而重验。这正是想要的，
# 但必须**显式**发生：只靠"字段缺失"这个巧合的话，将来若给旧条目补回该字段就会
# 静默失效，而且很难看出是版本号的锅。
#
# 缓存版本 7：反爬特征词表删掉裸 "cloudflare"（它匹配的是 Cloudflare 的邮箱保护
# 脚本，站点当 CDN 用就会被注入，与反爬无关）。判定口径变了——原来被判 auth 的源
# 现在会判 ok，旧缓存里的 health 是旧逻辑的产物，必须整体作废。
# 不作废的后果不是"结果旧一点"，而是**修了等于没修**：那条源的 auth 会在 7 天
# TTL 内一直命中缓存，看起来像修复失效。（v6 是同一理由：正文/目录判定收拢到
# core.quality，底线改为「非空即通过」。）
# 缓存版本 9：4xx 不再算「可达」。`_classify` 原来末尾直接 `return Health.OK`，
# 注释却写「其他 2xx/3xx 视为可达」——404/400/410/451 全被判成「可用」
# （实测库里 91 条是 ok+4xx）。口径变了：原来判 ok 的现在判 dead，
# 旧缓存的 health 是旧逻辑的产物，必须整体作废——理由同 v7，
# **不作废就是「修了等于没修」**（结论会在 TTL 内一直命中旧缓存）。
# 缓存版本 10：探测深度从「1/2/3 + 独立的搜索开关」合并成「1/2/3/4 一根轴」，
# 缓存里的 `probe_depth` 是**实际执行到的深度**，所以整列的含义随编号一起平移了
# （旧 1 档 = 域名+搜索 = 新 2 档，旧 2/3 = 新 3/4）。
#
# 严格说旧编号是**单调小于**新编号的，沿用旧行不会放出错误的结论（只会更保守地
# 重验）。但「深度 1 + 搜索开」这档旧行在新口径下必然判「深度不够」，等于整体重跑
# 一遍——把这件事标成版本变化，比让它以「我什么都没改，怎么全量又跑了」的样子
# 出现要好。
CACHE_VERSION = 10

#: 缓存有效期（天）：可用源留久一点，其余状态一律短 TTL——「待验证」「需代理复检」
#: 长期停在旧结论上，比多校验几次更糟。
#:
#: **默认值只从 settings_store 取**（AGENTS.md 硬性约定 #8），Web 端可在设置里
#: 覆盖并透传到 AsyncChecker；CLI 的 organize/report 与 cache_parity 不读设置，
#: 走这里的默认值。
DEFAULT_TTL_OK = _SETTINGS_DEFAULTS["check"]["cache_ttl_ok"]
DEFAULT_TTL_OTHER = _SETTINGS_DEFAULTS["check"]["cache_ttl_other"]


def classify_transport_error(error: str) -> str:
    """将传输层错误映射为保守状态，避免断网把源误判为永久失效。"""
    if error in ("reset", "tls"):
        return Health.GFW
    if error in ("timeout", "proxy"):
        return Health.TIMEOUT
    # DNS 失败既可能是域名注销，也可能是本机/网络断开时的统一解析失败。
    # 在批量校验中无法仅凭一次请求区分，按待复检处理，避免误杀并阻止缓存污染。
    if error == "dns":
        return Health.TIMEOUT
    return Health.ERROR


def classify_http_status(status: Optional[int], text: str,
                         enabled_cookie_jar: bool = False) -> str:
    """HTTP 状态码 + 响应体 → 健康态。**全仓库唯一的判定表**。

    三处需要它：`AsyncChecker._classify`（域名探测）、`_probe_search`（搜索探测）、
    `reclassify.diagnose_source`（失效归因）。原来各写一份，实测已在 5 种输入上分叉：

        503（无响应体）         域名探测=dead    归因=需验证
        404 / 500 / 406         域名探测=dead    归因=继续判
        200 + 登录页 + cookie   域名探测=auth    归因=继续判

    **判定与后果要分开**：本函数只回答「这个响应算什么」，各调用点自己决定怎么用。
    最典型的是搜索入口 404——那**不能**推出「源死了」（可能只是搜索规则过期），
    所以 `_probe_search` 只取它的 AUTH，不套用 4xx → DEAD。
    """
    if status is None:
        return Health.DEAD
    low = (text or "").lower()
    # 401 / 403 / 429 一律 AUTH：它们本身就是「要登录 / 被拒」，不必再看响应体
    if status in (401, 403, 429):
        return Health.AUTH
    # 503 要看响应体：带反爬特征才算「需验证」，否则是服务端挂了
    if status == 503 and any(m in low for m in ANTI_BOT_MARKERS):
        return Health.AUTH
    if status >= 500:
        return Health.DEAD
    if status == 200:
        if any(m in low for m in ANTI_BOT_MARKERS):
            return Health.AUTH
        # 登录墙要看源有没有声明 cookie jar——「请登录」在正常页面的导航栏里太常见
        if any(m in low for m in LOGIN_MARKERS) and enabled_cookie_jar:
            return Health.AUTH
        return Health.OK
    # 2xx / 3xx 都是可达（204、301、302 是大量正常源的形态）
    if 200 <= status < 400:
        return Health.OK
    # 其余 4xx：服务端明确说「这个入口拿不到东西」——不是可达。
    # **原来这里直接 return OK**，而注释写「其他 2xx/3xx 视为可达」，于是
    # 404/400/410/451 全被判成「可用」（实测库里 91 条 ok+4xx）。
    return Health.DEAD


def should_cache_result(health: str) -> bool:
    """瞬时网络错误不写缓存，避免一次断网污染后续校验。"""
    return health not in (Health.TIMEOUT, Health.ERROR)


def _err_desc(err: str) -> str:
    """失败原因 → 中文描述（用于诊断信息）。"""
    return {
        "dns": "DNS解析失败(可能域名不存在或被污染)",
        "timeout": "连接超时",
        "reset": "连接被重置(典型被墙特征)",
        "tls": "TLS握手失败(可能SNI阻断)",
        "proxy": "代理连接失败",
        "other": "网络异常",
    }.get(err, "网络异常")


#: Legado 的 URL 选项分隔符——**原文照抄** `AnalyzeUrl.kt:776` 的 `paramPattern`：
#:
#:     Regex("\s*,\s*(?=\{)")
#:
#: 逗号 + 可选空白 + 紧随其后的 `{`。**切第一个匹配**，不是最后一个：切最后一个
#: 会让 URL 里残留一段 `,{...}`，请求就变形了。
_URL_OPTION_RE = re.compile(r"\s*,\s*(?=\{)")

#: 关键词占位符（Legado 的 searchUrl 模板里可能是这几种写法之一）
_KEYWORD_PLACEHOLDERS = ("{{key}}", "{{searchKey}}", "{{keyword}}", "$searchKey")


def split_url_options(rule: str) -> Tuple[str, Dict[str, Any]]:
    """把 ``url,{json}`` 拆成 ``(url, 选项 dict)``。

    **JSON 解不出来时照样把 URL 切下来**、选项当空——这是**对齐 App**，不是随手：
    Legado 先按 `paramPattern` 切出 `urlNoOption` 拿去发请求，**之后**才解析选项；
    解析失败只是不应用选项，URL 已经被切了（`AnalyzeUrl.kt:219-231`）。
    """
    text = rule or ""
    m = _URL_OPTION_RE.search(text)
    if not m:
        return text, {}
    url = text[:m.start()]
    try:
        opt = json.loads(text[m.end():])
    except Exception:
        return url, {}
    return url, opt if isinstance(opt, dict) else {}


def parse_search_request(url_template: str, keyword: str
                         ) -> Tuple[str, str, Dict[str, str], str]:
    """解析 searchUrl 模板，返回 ``(请求URL, 方法, 附加Header, body)``。

    **两条语法都要认**：

    1. **Legado 的 URL 选项**——``url,{"method":"POST","body":"kw={{key}}"}``
       （`AnalyzeUrl.kt` 的 `UrlOption`）。实测库里 **1551 条源**在用
       （method 1167 / body 1141 / charset 627 / headers 53），**它才是官方语法**。
    2. **本项目早期的 ``@POST`` / ``@headers=`` / ``@Cookie=`` 后缀**——只作兼容，
       库里仅 1 条在用。**别再往这条上扩展**，新写法一律用选项 JSON。

    > 修之前第 1 条完全没人解析：整段 JSON 被留在 URL 里当 GET 发出去，
    > **关键词根本没到服务端**。实测带该语法的源搜索命中率 **1.0%（17/1674）**，
    > 不带的是 **20.5%（446/2175）**——差 20 倍。

    ``charset`` 选项**暂不处理**：响应解码走 `_decode_body` 的
    utf-8 → gbk → gb2312 回退，已覆盖常见情况；真要按声明解码需要把它一路铺到
    `_request`，收益与改动面不成比例。**留着它不影响正确性**（回退本来就会命中）。
    """
    body, options = split_url_options(url_template)
    url_part = body
    method = "GET"
    headers: Dict[str, str] = {}

    # ① Legado 的 URL 选项
    m = str(options.get("method", "") or "").strip().upper()
    if m in ("GET", "POST"):
        method = m
    hdr = options.get("headers")
    if isinstance(hdr, dict):
        headers.update({str(k): str(v) for k, v in hdr.items()})
    body_tpl = str(options.get("body", "") or "")

    # ② 兼容早期的 @ 后缀（库里仅 1 条；别在这条上扩展）
    for token in sorted(["@POST", "@post", "@GET", "@get", "@headers=", "@Cookie="],
                        key=len, reverse=True):
        idx = url_part.rfind(token)
        if idx > 0:
            if token in ("@POST", "@post"):
                method = "POST"
                url_part = url_part[:idx]
            elif token in ("@GET", "@get"):
                method = "GET"
                url_part = url_part[:idx]
            elif token == "@headers=":
                rest = url_part[idx + len(token):]
                url_part = url_part[:idx]
                if rest.startswith("{"):
                    try:
                        headers.update(json.loads(rest))
                    except Exception:
                        pass
            elif token == "@Cookie=":
                rest = url_part[idx + len(token):]
                url_part = url_part[:idx]
                headers["Cookie"] = rest
            break

    # 关键词占位符替换（中文关键词需 URL 编码）。**URL 与 body 都要替**
    encoded_keyword = quote(keyword, safe="")
    for ph in _KEYWORD_PLACEHOLDERS:
        if ph in url_part:
            url_part = url_part.replace(ph, encoded_keyword)
        if ph in body_tpl:
            body_tpl = body_tpl.replace(ph, encoded_keyword)
    # 分页占位符替换为 1
    url_part = url_part.replace("{{page}}", "1")
    body_tpl = body_tpl.replace("{{page}}", "1")
    return url_part, method, headers, body_tpl


def build_domain_url(url: str) -> str:
    """从 bookSourceUrl 构造用于连通性探测的根 URL。

    **必须先剥掉 ``#`` 之后的内容**。书源分享圈习惯把署名/溯源标记挂在 URL 后面
    （``https://m.qidian.com##时间排序发现规则``、``http://www.yuedsk.com###昵称``、
    ``https://blquge99.cc/#pb1101``），本项目实测 40.9% 的源带这种后缀。

    正则 ``[^/]+`` 在 ``#`` 前**没有** ``/`` 时会把它一起吞进"域名"，
    请求就带着 fragment 发出去——现在能跑只是因为 HTTP 客户端会把 ``#`` 之后丢掉，
    属于依赖巧合。有 ``/`` 的形式（``https://a.com/##签名``）反而正常，
    所以这个坑只在部分数据上显形，容易漏。

    **只在这里剥**：``_normalize_url`` 保留 fragment 是对的——署名不同即两个源，
    与 Legado 的 ``getSourceKey()`` 一致。两者用途不同，别顺手一起改。
    """
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("#", 1)[0].strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    # 去掉路径，保留根
    m = re.match(r"(https?://[^/]+)", url)
    if m:
        return m.group(1)
    return url


# ------------------------------------------------------------ 星级与缓存公共逻辑

def _strip_rule_prefix(rule: str) -> str:
    """剥离 Legado 规则类型前缀（@css:），保留纯 CSS 选择器给 apply_css_rule。"""
    rule = rule.strip()
    if rule.startswith("@css:"):
        return rule[len("@css:"):]
    return rule


def _static_toc_ok(raw: Optional[Dict[str, Any]]) -> bool:
    """静态判定：ruleToc.chapterList 非空（规则存在，不保证能解析出章节）。"""
    if not raw:
        return False
    toc = raw.get("ruleToc") or {}
    if not isinstance(toc, dict):
        return False
    return bool(str(toc.get("chapterList", "") or ""))


def _static_content_ok(raw: Optional[Dict[str, Any]]) -> bool:
    """静态判定：ruleContent.content 非空（规则存在，不保证能解析出正文）。"""
    if not raw:
        return False
    content = raw.get("ruleContent") or {}
    if not isinstance(content, dict):
        return False
    return bool(str(content.get("content", "") or ""))


def static_rule_complete(raw: Optional[Dict[str, Any]]) -> bool:
    """静态判定：目录 + 正文规则均齐全（quality_tags 里的「规则完整」标签依据）。"""
    return _static_toc_ok(raw) and _static_content_ok(raw)


def is_cache_item_valid(
    record: BookSourceRecord,
    item: Dict[str, Any],
    now: Optional[datetime] = None,
    min_depth: int = DEPTH_HOME,
    ttl_ok: int = DEFAULT_TTL_OK,
    ttl_other: int = DEFAULT_TTL_OTHER,
) -> bool:
    """判断缓存是否仍可用于该书源。

    复用必须满足：缓存版本正确、规则指纹一致、校验时间有效且不在未来；
    若本次要跑验证，缓存还必须是**在同等或更深探测下**产出的。

    探测能力现在只有一根轴（``min_depth``）：深度到「搜索」档就意味着这次要验搜索，
    所以「探没探搜索」不再单独传参——合并前它是第二根轴 ``min_search``，两根轴能
    配出「要求搜索但不要求深度」这种没有意义的组合。

    ``min_depth`` 只有校验链路（``AsyncChecker.run``）需要传本次的配置。其余
    调用方——CLI 的 organize/report、cache_parity 的两库比对——只是拿缓存算标签和
    报告，重跑不了探测，保持默认（``DEPTH_HOME``）即「不因探测能力作废」。
    """
    if item.get("v") != CACHE_VERSION:
        return False
    if item.get("fingerprint") != fingerprint(record.raw):
        return False
    checked_at = item.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        return False
    try:
        checked_time = datetime.strptime(checked_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    current_time = now or datetime.now()
    if checked_time > current_time:
        return False
    health = str(item.get("health", ""))
    ttl_days = ttl_ok if health == Health.OK else ttl_other
    if current_time - checked_time > timedelta(days=ttl_days):
        return False
    # 深度不够必须重验。缓存里的 probe_depth 是**实际执行到的深度**
    # （_probe_toc/_probe_content 内部写的），不是配置深度。若只比版本/指纹/时间，
    # 用户把深度从 1 调到 3 后浅缓存仍然命中，深度验证一条都不会跑，而界面上显示的
    # 是「校验完成」——正是最该避免的「看起来跑了其实没跑」。
    #
    # 只对 OK 的源要求深度：非 OK 的源按 fail-fast 根本走不到深度验证
    # （check_one 里要求 health == OK 才继续），强制重验只是白打请求。
    if health == Health.OK:
        if int(item.get("probe_depth", DEPTH_HOME) or DEPTH_HOME) < min_depth:
            return False
    # 「探没探搜索」原本是第二根轴，现在由深度推出：深度到了「搜索」档就是要验搜索。
    # 缓存行里 `search_probed` 记的是**实际跑没跑**（不是参数开没开），判据方向只能
    # 是「本次要求更高才作废」——缓存比本次"强"（验过搜索、本次只到主页档）时照常复用。
    #
    # 对本来就走不到搜索的源（health 非 OK、无搜索规则）不设此要求：check_one 的
    # 条件正是 `health == OK and self.wants_search and record.has_search`。对它们
    # 也要求的话，这些源**永远命中不了缓存**，每次校验都白打一遍请求。
    if (min_depth >= DEPTH_SEARCH and health == Health.OK and record.has_search
            and not item.get("search_probed")):
        return False
    return True


def evaluate_stars(
    health: str,
    has_search: bool,
    search_response_ms: int,
    search_hit: str,
    toc_complete: Optional[bool],
    content_ok: Optional[bool],
    raw: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str]:
    """星级评分 + **这一级是实测还是推定**。

    返回 ``(星级, 来源)``，来源取值：

    - ``"measured"``：这个星级依赖的每一级都有真实请求的结论支撑
    - ``"static"``  ：中间有某一级是「没验到，按静态规则回退通过」的
    - ``""``        ：0★（不可达），没有可标注的东西

    **为什么要这一维**：3★ 有**两种完全不同的来源**，界面上长得一模一样——

        (a) 搜索实测命中              → 看就是「验过了」
        (b) 没验过，只是静态规则齐全   → 其实是「看规则推的」

    用户看到「可用 3★」分不出这两种。海豚书屋（404 却报 3★）本质就撞在这一格上。
    4★/5★ 同样可能是推的：`toc_complete` / `content_ok` 为 None（验证跑不了，
    例如规则含 JS）时会回退静态规则，**那一格就没有实测支撑**。

    判定规则只有一条：**把「靠静态规则回退通过」的那几级记下来，出现任意一级就是
    ``static``**。所以「命中 + 规则齐全、但目录正文一次都没验」的源会拿到
    ``5★ static``——5★ 的含义是「正文可用」，而它里面没有一格正文是实测的。
    如实呈现；**要不要连星级本身也收紧是另一个问题，本函数只负责说清楚**。

    阶梯本身（每一级依赖上一级，方案D 分档宽松）：

      1★ 可达：health ∈ (ok / auth / no_search)（域名通或能访问，仅需登录/不可搜）
      2★ 搜索连通：有搜索规则且搜索请求有响应（search_response_ms > 0）
      3★ 弱证据档：搜索真实命中测试作品 或 静态规则完整（目录+正文规则齐全）
      4★ 目录完整：命中源用实测 toc_complete（None=无法验证→回退静态目录规则非空）；
                    未命中源静态目录规则非空
      5★ 正文可用：命中源用实测 content_ok（None=无法验证→回退静态正文规则非空）；
                    未命中源静态正文规则非空

    宽严边界（方案D）：
      - 未命中测试作品的源最高 3★（弱证据档封顶）：测试集仅覆盖大众作品，
        未命中≠源差，规则齐全即给 3★，但 4★/5★ 保留给有命中实测证据的源
      - 深度验证「无法验证」（None）的维度回退静态规则判定——不误杀规则齐全的源
      - 实测明确不达标（False）仍按不满足扣分——不误放真坏的源
    """
    if health not in (Health.OK, Health.AUTH, Health.NO_SEARCH):
        return 0, ""
    stars = 1  # 可达（域名请求本身是实测）
    if not (has_search and search_response_ms > 0):
        return stars, "measured"
    stars = 2  # 搜索连通（实测）
    # 命中和静态规则都不满足 → 2★。静态判据被问过但**没给分**，不算「推的」
    if not (search_hit or static_rule_complete(raw)):
        return stars, "measured"
    stars = 3  # 命中 或 静态规则完整（弱证据档）
    if not search_hit:
        # 这一级的依据**就是**静态规则 → 推定
        return stars, "static"
    used_static = False
    # 目录完整：实测优先，None（无法验证）回退静态规则；False 仍不达标
    toc_ok = toc_complete
    if toc_ok is None:
        toc_ok = _static_toc_ok(raw)
        # **只有静态回退真的放行了才算「推的」**：回退了但没通过时，这一级并没有
        # 靠静态规则拿到东西（星级由下面的 return 决定，依据是实测的命中）
        if toc_ok:
            used_static = True
    if not toc_ok:
        return stars, _basis(used_static)
    stars = 4  # 目录完整
    # 正文可用：实测优先，None（无法验证）回退静态规则；False 仍不达标
    content_ok_ = content_ok
    if content_ok_ is None:
        content_ok_ = _static_content_ok(raw)
        if content_ok_:
            used_static = True
    if not content_ok_:
        return stars, _basis(used_static)
    return 5, _basis(used_static)  # 正文可用


def _basis(used_static: bool) -> str:
    return "static" if used_static else "measured"


def calc_stars(
    health: str,
    has_search: bool,
    search_response_ms: int,
    search_hit: str,
    toc_complete: Optional[bool],
    content_ok: Optional[bool],
    raw: Optional[Dict[str, Any]] = None,
) -> int:
    """星级评分（0-5★）。阶梯、宽严边界、以及「实测 / 推定」的口径见
    :func:`evaluate_stars`（本函数只是它的取值出口，**不要在这里另写一套阶梯**）。
    """
    return evaluate_stars(health, has_search, search_response_ms, search_hit,
                          toc_complete, content_ok, raw)[0]


def restore_from_cache(rec: BookSourceRecord, item: Dict[str, Any]) -> None:
    """把缓存项恢复到记录（checker.run / main.cmd_organize / main.cmd_report 共用）。

    - 星级用缓存的**原始量**（health/has_search/search_response_ms/search_hit/深度字段）
      经 calc_stars 重算，不信任缓存里存的旧 quality_stars——星级规则升级后旧缓存自动按新规则重评
    - 标签：剔除旧缓存遗留的「命中《》」标签，保留静态「原创」标记，
      并按静态规则补齐「规则完整」（若缓存里没有）
    """
    rec.health = item.get("health", Health.SKIPPED)
    rec.status_code = int(item.get("status_code", 0) or 0)
    rec.response_time_ms = int(item.get("response_time_ms", 0) or 0)
    rec.error = item.get("error", "")
    rec.checked_at = item.get("checked_at", "")
    rec.search_hit = item.get("search_hit", "")
    rec.search_response_ms = int(item.get("search_response_ms", 0) or 0)
    # 深度验证字段（旧缓存没有这些字段 → 按最低档处理）
    rec.probe_depth = int(item.get("probe_depth", DEPTH_HOME) or DEPTH_HOME)
    rec.chapter_count = int(item.get("chapter_count", 0) or 0)
    rec.toc_complete = item.get("toc_complete")  # None / True / False
    rec.toc_fail_reason = item.get("toc_fail_reason", "")
    rec.content_ok = item.get("content_ok")      # None / True / False
    rec.content_fail_reason = item.get("content_fail_reason", "")
    rec.content_response_ms = int(item.get("content_response_ms", 0) or 0)
    # 星级重算（规则升级后旧缓存不失效；方案D：实测优先、None 回退静态，无需配置深度）
    rec.quality_stars, rec.star_basis = evaluate_stars(
        health=rec.health,
        has_search=rec.has_search,
        search_response_ms=rec.search_response_ms,
        search_hit=rec.search_hit,
        toc_complete=rec.toc_complete,
        content_ok=rec.content_ok,
        raw=rec.raw,
    )
    # 标签清洗与补齐：剔除「命中《》」和旧缓存遗留的「原创」，按静态规则补齐「规则完整」
    cached_tags = [t for t in (item.get("quality_tags", []) or [])
                   if not t.startswith("命中") and t != "原创"]
    merged = list(dict.fromkeys(cached_tags))
    if "规则完整" not in merged and static_rule_complete(rec.raw):
        merged.append("规则完整")
    rec.quality_tags = merged


def _decode_body(body: bytes) -> str:
    """尝试多种编码解码响应体。"""
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


class AsyncChecker:
    """异步书源校验器。"""

    def __init__(
        self,
        concurrency: int = 50,
        timeout: float = 8.0,
        keyword: str = "我",
        verify_ssl: bool = True,
        cache_dir: Optional[str] = None,
        testset: Optional[Dict[str, List[str]]] = None,
        max_keywords: int = 2,
        proxy: Optional[str] = None,
        probe_depth: int = DEPTH_HOME,
        test_titles: Optional[Dict[str, Dict[str, Any]]] = None,
        use_store: Optional[bool] = None,
        store_path: Optional[str] = None,
        cache_ttl_ok: int = DEFAULT_TTL_OK,
        cache_ttl_other: int = DEFAULT_TTL_OTHER,
    ):
        self.concurrency = concurrency
        self.timeout = timeout
        self.keyword = keyword
        self.verify_ssl = verify_ssl
        self.cache_dir = cache_dir
        self.proxy = proxy  # 可选代理（socks5:///http://），用于翻墙源复检
        # 测试集：{"novel": [...], "manga": [...]}，缺省使用内置
        self.testset = testset or {}
        self.novel_keywords = self.testset.get("novel") or NOVEL_TEST_KEYWORDS
        self.manga_keywords = self.testset.get("manga") or MANGA_TEST_KEYWORDS
        self.max_keywords = max_keywords  # 每个源最多尝试测试的关键词数
        # 探测深度：一根轴四档，一档对一级星级（口径的唯一定义在 settings_store）：
        #   DEPTH_HOME 1 主页  仅域名探测
        #   DEPTH_SEARCH 2 搜索  搜索探测（含命中判定）
        #   DEPTH_TOC 3 目录  详情页 + 目录页，比对章节数
        #   DEPTH_CONTENT 4 正文  章节页抓一章全文
        self.probe_depth = probe_depth if probe_depth in PROBE_DEPTHS else DEPTH_HOME
        # 缓存有效期（天）。取值由调用方决定（Web 端从全局设置来），
        # 这里不再自己读设置——checker 不该隐式依赖用户配置
        self.cache_ttl_ok = cache_ttl_ok
        self.cache_ttl_other = cache_ttl_other
        # 目录完整度参考表：{作品名: {"type": "novel|manga", "chapters": N}}，缺省内置 TEST_TITLES
        self.test_titles = test_titles or TEST_TITLES
        self._sem: Optional[asyncio.Semaphore] = None
        self.refresh_cache = False
        # 命中判定降级的次数与原因（见 _confirm_hit）。**必须留痕**：那条路径
        # 把「规则回放不了」当成「命中了」，静默的话与 lessons §二 记的那次
        # bs4 缺失事故是同一个形状——当时就是这样让命中判定悄悄退化成
        # 「响应体里出现关键词就算命中」的。这里只收集，由 run() 收尾汇总
        self.hit_downgrades: List[str] = []
        #: 写缓存失败的条数。**必须能报出来**：写不进去的表现是「校验跑了但状态
        #: 不变」，而这个表现和「源本来就没变」在界面上无法区分
        self.save_failures = 0
        #: 本次有多少条直接复用了缓存（没发请求）。run() 里填，供任务结果展示——
        #: 不然用户点完校验只看到「完成」，却不知道一条请求都没发
        self.cached_count = 0
        # 每个源上一次发请求的时刻（monotonic 毫秒），用于遵守该书源自己声明的
        # concurrentRate。见 _throttle
        self._rate_last: Dict[str, float] = {}

        if use_store is None:
            use_store = not os.getenv("LEGADO_LEGACY_CACHE")
        self.use_store = bool(use_store)
        self.store_path = store_path
        self._store_conn = None

    @property
    def wants_search(self) -> bool:
        """本次要不要验搜索。**判据只有一条**：深度到「搜索」档及以上。

        合并前这是一个独立的 `probe_search` 开关，与深度是两根轴，能配出
        「关搜索 + 选目录档」这种永远进不去目录的非法组合（目录/正文的门要求
        `search_hit`）。现在由深度推出，判据只有这一处。
        """
        return self.probe_depth >= DEPTH_SEARCH

    def _store(self):
        if self._store_conn is None:
            from core.store import Store
            self._store_conn = Store(self.store_path)
        return self._store_conn

    def close(self) -> None:
        if self._store_conn is not None:
            try:
                self._store_conn.close()
            except Exception:
                pass
            self._store_conn = None

    def keywords_for(self, record: BookSourceRecord) -> List[str]:
        """按书源类型选择测试关键词（漫画源用漫画名，其余用小说名）。"""
        if record.source_type == 2:
            return self.manga_keywords
        return self.novel_keywords

    # ------------------------------------------------------------ 缓存
    def _cache_path(self, url_key: str) -> str:
        return os.path.join(self.cache_dir, f"check_{url_key}.ndjson") if self.cache_dir else ""

    def load_cache(self) -> Dict[str, Dict[str, Any]]:
        """读取历史校验结果缓存 ``{规范化url: result}``。

        **键一律规范化，两个后端都归一**。store 后端存的 ``checks.source_url``
        本来就是规范化的（``Store.save_checks`` 里做的），而 ndjson 后端存的是
        抓取时的原文 URL。两边键不一致的后果不是「少命中一点」，而是 store 那条
        **永远命不中**——列表里的 URL 常带尾斜杠、规范化后不带（这个差异本项目
        实测是 20.7%）。查漏了缓存就一直不复用，而界面上看不出来。

        这正是 lessons §五 记的那类「跨表/跨库关联的 URL 两侧必须用同一套规范化」，
        只是方向反过来：那次是写侧没归一，这次是读侧。
        """
        if self.use_store:
            try:
                return self._store().checks_map()
            except Exception:
                return {}
        if not self.cache_dir or not os.path.isdir(self.cache_dir):
            return {}
        cache: Dict[str, Dict[str, Any]] = {}
        for fn in os.listdir(self.cache_dir):
            if not fn.startswith("check_") or not fn.endswith(".ndjson"):
                continue
            p = os.path.join(self.cache_dir, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            cache[_normalize_url(item.get("url", ""))] = item
                        except Exception:
                            continue
            except Exception:
                continue
        return cache

    def save_cache_append(self, record: BookSourceRecord) -> None:
        """把一条校验结果写进缓存后端：store（SQLite 管理库）优先，否则 ndjson 目录。

        **两个后端都要能单独工作**。以前的写法是「先 ``os.makedirs(self.cache_dir)``，
        再判 use_store」——cache_dir 为 None 时第一步就抛 TypeError，而 Web 那条
        链路（``backend/api/ops.py``）正好只传 ``use_store=True``、不传 cache_dir。
        配上 ``run()`` 里那道 ``if self.cache_dir:`` 的门，结果是**校验算完从不落库**：
        界面上请求成功、状态不变。见 run() 里保存那一段。
        """
        if not should_cache_result(record.health):
            return
        url_key = re.sub(r"[^\w\-.]", "_", record.url or f"idx{record.index}")[:80]
        item = {
            "v": CACHE_VERSION,
            "url": record.url,
            "fingerprint": fingerprint(record.raw),
            "name": record.name,
            "health": record.health,
            "status_code": record.status_code,
            "response_time_ms": record.response_time_ms,
            "error": record.error,
            "checked_at": record.checked_at,
            "search_hit": record.search_hit,
            "search_response_ms": record.search_response_ms,
            # 本次是否**真的跑过**搜索探测——注意不是"参数开着"。取或的理由：
            # search_response_ms 在 _probe_search 里赋值，没跑过时为 0；search_hit
            # 只在命中时非空。而「跑过但没命中」正是最需要与"没跑"区分开的情况，
            # 它会让 search_response_ms > 0。
            #
            # 唯一的假 False 路径：parse_search_request / _request 每次都在抛异常，
            # 关键词耗尽后返回 None，此时两值都是 0。这个方向是**对的**（下次保守
            # 重探、多打一次请求），别把它"修正"成严格相等
            "search_probed": bool(record.search_response_ms or record.search_hit),
            "quality_stars": record.quality_stars,
            "star_basis": record.star_basis,
            "quality_tags": record.quality_tags,
            # 深度验证字段
            "probe_depth": record.probe_depth,
            "chapter_count": record.chapter_count,
            "toc_complete": record.toc_complete,
            "toc_fail_reason": record.toc_fail_reason,
            "content_ok": record.content_ok,
            "content_fail_reason": record.content_fail_reason,
            "content_response_ms": record.content_response_ms,
        }
        if self.use_store:
            try:
                self._store().save_checks([item])
            except Exception as e:
                # **不能静默**：写不进去 = 校验跑了但状态不变，界面上完全看不出来。
                # 以前这里是 `except Exception: pass`，正好把这次的故障盖住了
                self.save_failures += 1
                if self.save_failures == 1:
                    print("警告: 校验结果写管理库失败（后续同类错误不再逐条打印）: "
                          "%s: %s" % (type(e).__name__, e))
            return
        if not self.cache_dir:
            return      # 两个后端都没配：跳过就好，别去 makedirs(None)
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(self._cache_path(url_key), "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ 单源探测
    async def _throttle(self, record: BookSourceRecord) -> None:
        """按源**自己声明**的 concurrentRate 等够间隔再发下一个请求。

        为什么需要：源声明 ``concurrentRate`` 就是在说「这么打我会封你」，而
        check 一次要连发好几个请求（域名 → 搜索 ×N → 详情 → 章节）。不遵守的话
        轻则触发反爬被判失效（而失效会被缓存 7 天），重则让对方封掉整个 IP。

        源内的请求本来就是顺序发出的（``check_one`` 里逐个 await），所以不需要锁，
        只要记住这个源上次发请求的时刻。等的时候仍然占着一个并发槽——声明了限速的
        源本来就不该被并发地打，这是期望行为而不是缺陷。
        """
        interval = rate_interval_ms((record.raw or {}).get("concurrentRate"))
        if interval <= 0:
            return
        now = time.monotonic() * 1000.0
        last = self._rate_last.get(record.url)
        if last is not None:
            wait_ms = interval - (now - last)
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000.0)
        self._rate_last[record.url] = time.monotonic() * 1000.0

    async def _request(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        allow_redirects: bool = True,
        body: str = "",
    ) -> tuple[Optional[int], bytes, float, str]:
        """发请求，返回 (状态码, 响应体, 耗时ms, 失败原因)。

        失败原因分为：dns / timeout / reset / tls / proxy / other
        用于区分「真死」与「被墙」。

        ``record`` **必填且不给默认值**：它用来读该书源的 concurrentRate 限速。
        留默认值的话，将来新增的调用点漏传就会静默不受限速约束——而「静默地打了
        不该打的频次」正是会招来封禁的那种错。
        """
        await self._throttle(record)
        t0 = time.perf_counter()
        h = {"User-Agent": DEFAULT_UA}
        if headers:
            h.update(headers)
        err = ""
        try:
            async with session.request(
                method, url, headers=h, timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=allow_redirects, ssl=None if self.verify_ssl else False,
                proxy=self.proxy,
                # 书源可以声明 body（`url,{"method":"POST","body":"kw={{key}}"}`）。
                # **关键词在这里，不在 URL 里**——不传 body 的话服务端收不到搜索词
                data=body if body else None,
            ) as resp:
                body = await resp.read()
                cost = (time.perf_counter() - t0) * 1000
                return resp.status, body, cost, ""
        except asyncio.TimeoutError:
            err = "timeout"
        except aiohttp.ClientConnectorDNSError:  # 域名解析失败：域名不存在或被 DNS 污染
            err = "dns"
        except aiohttp.ClientConnectorSSLError as e:  # TLS 握手失败：SNI 阻断/证书问题
            err = "tls"
        except aiohttp.ClientConnectionResetError:  # TCP 连接被重置：被墙典型特征
            err = "reset"
        except aiohttp.ServerDisconnectedError:  # 服务器主动断开
            err = "reset"
        except aiohttp.ClientProxyConnectionError as e:  # 代理连接失败
            err = "proxy"
        except aiohttp.ClientError as e:
            err = "other"
        except Exception as e:
            err = "other"
            # 这一支是**兜底**：常见的连接类错误在上面按 aiohttp 的具体类型分完了
            # （10054 → `aiohttp.ClientConnectionResetError` → "reset"；
            #  10061 → `aiohttp.ClientConnectorError` → `ClientError` → "other"；
            #  超时 → `asyncio.TimeoutError` → "timeout"），走到这里的是没归类的。
            #
            # 这里原本还有两支按 `winerror` 分 10054 / 10060 / 10061 的 elif，
            # **两支都不可达**：`TimeoutError` 本身就是 `OSError` 的子类，所以
            # 这一支的 `isinstance(e, OSError)` 已经把 OSError 全吃掉了
            # （实测：伪造一个 `winerror=10054` 的 OSError，命中的是这一支）。
            # 已删——删它们不改变任何分类结果
            if isinstance(e, (TimeoutError, OSError)) or "timed out" in str(e).lower():
                err = "timeout"
        return None, b"", (time.perf_counter() - t0) * 1000, err

    def _classify(self, status: Optional[int], body: bytes, rec: BookSourceRecord) -> str:
        """根据 HTTP 状态与响应体判定健康状态。

        判定表只有一份，见模块级的 :func:`classify_http_status`——**不要在这里
        另写一遍**（那正是原来三处分叉的成因）。
        """
        return classify_http_status(status, _decode_body(body), rec.enabled_cookie_jar)


    async def check_one(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
    ) -> BookSourceRecord:
        """校验单个书源 + 优质度检测。"""
        assert self._sem is not None
        async with self._sem:
            if not record.url:
                record.health = Health.ERROR
                record.error = "无 bookSourceUrl"
                record.quality_stars = 0
                record.star_basis = ""
                return record

            # 1) 域名连通性探测
            domain_url = build_domain_url(record.url)
            status, body, cost, err = await self._request(session, record, domain_url)
            record.status_code = status or 0
            record.response_time_ms = int(cost)
            record.checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
            health = self._classify(status, body, record)
            if health == Health.DEAD:
                # 细分失败原因：被墙特征（连接重置/TLS阻断）→ 需翻墙
                # 传输层失败统一走保守分类；只有明确 HTTP 失败才保留 DEAD。
                if err in ("reset", "tls"):
                    health = Health.GFW
                    record.error = f"疑似被墙（{_err_desc(err)}）"
                elif err:
                    record.error = _err_desc(err)
                    health = classify_transport_error(err)
                elif status:
                    # 传输是通的，是服务端回了 4xx 才判死。**不能沿用下面那句
                    # 「连接失败/超时/DNS错误」**——结论对了、理由错了同样没用，
                    # 而且会把排查引向网络层（lessons §二 的同一类问题）。
                    record.error = "HTTP %s（入口不存在或被拒）" % status
                else:
                    record.error = "连接失败/超时/DNS错误"

            # 2) 若域名可达且有搜索规则，用测试集探测搜索并判定命中
            s_body: Optional[bytes] = None
            if health == Health.OK and self.wants_search and record.has_search:
                record.health = health
                s_body = await self._probe_search(session, record, domain_url)
                health = record.health

            # 3) 深度验证：命中测试作品后才链式验证目录（目录档）与正文（正文档）
            #    fail-fast：未命中/搜索不通 → 零额外请求；目录验证失败 → 不再验证正文
            #    注意：此处条件必须用配置深度 self.probe_depth（而非 record.probe_depth，
            #    后者由 _probe_* 内部记录"实际执行到的深度"，目录阶段已置 DEPTH_TOC）
            if (record.health == Health.OK and record.search_hit
                    and self.probe_depth >= DEPTH_TOC and s_body):
                toc_body = await self._probe_toc(session, record, domain_url, s_body)
                if self.probe_depth >= DEPTH_CONTENT and toc_body:
                    await self._probe_content(session, record, domain_url, toc_body)

            record.health = health
            # 4) 计算星级 + 证据来源（阶梯与「实测/推定」口径都见 evaluate_stars）
            record.quality_stars, record.star_basis = evaluate_stars(
                health=record.health,
                has_search=record.has_search,
                search_response_ms=record.search_response_ms,
                search_hit=record.search_hit,
                toc_complete=record.toc_complete,
                content_ok=record.content_ok,
                raw=record.raw,
            )
            return record

    def _confirm_hit(self, s_body: bytes, record: BookSourceRecord) -> bool:
        """强化命中判定：响应体含关键词后，用 ruleSearch.bookList 真实解析一次，
        结果列表 >=1 条非空才算真命中（防关键词出现在导航/搜索框回显等假阳性）。

        无法用 CSS 规则验证时不拦截、直接算命中（保守不误杀）：
          - 无 bookList 规则（搜不到列表规则，仅按响应体判定）
          - 规则含 <js / js: / @xpath 前缀（非 CSS 选择器，apply_css_rule 不支持）
          - 解析抛异常（页面结构异常，不因工具限制误判）

        **前两种是明知的取舍，第三种是异常**——异常那条必须留痕：它会把
        「规则回放不了」当成「命中了」，正是 lessons §二 那次 bs4 缺失事故的形状
        （ModuleNotFoundError 被吞 → 命中判定退化成「含关键词就算命中」，且无任何
        痕迹）。降级仍返回 True（保守不误杀），但会记进 ``hit_downgrades``。
        """
        rule = str(((record.raw or {}).get("ruleSearch") or {}).get("bookList", "") or "").strip()
        if not rule:
            return True
        # 规则离线跑不了（JS / 模板 / XPath / 多规则合并…）→ 保守算命中。
        # **判据只能来自 replayer**：原来这里手写子串（`<js` / 开头的 `js:` /
        # `@xpath`），漏掉最常见的**中间形态** `selector@js:code`——那条会掉到
        # 下面被 apply_css_rule 跑出空（实测 `@js:` 恒返回 []），于是判「未命中」，
        # 而判未命中会让整个深度验证跳过（上面 check_one 要求 search_hit）。
        # 同一张判据表散在三处必然漂移，见本文件另外两处同源改动。
        #
        # **这里刻意不留痕**：明知的能力边界，每次记会把真正要看的异常
        # （依赖缺失、解析器坏掉）淹没——见 tests/test_checker_judge.py 的
        # test_js_rule_is_a_known_tradeoff_not_a_downgrade。
        if parse_rule(rule).unsupported:
            return True
        try:
            html = _decode_body(s_body)
            items = apply_css_rule(html, _strip_rule_prefix(rule))
            items = [i for i in items if str(i or "").strip()]
            return len(items) >= 1
        except Exception as e:
            self.hit_downgrades.append(
                "%s: %s: %s" % (record.url, type(e).__name__, e))
            return True

    async def _probe_search(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
        domain_url: str,
    ) -> Optional[bytes]:
        """用测试集关键词逐个尝试搜索，命中即停；同时记录搜索响应时间。

        返回命中的搜索响应体（bytes，供目录档复用解析详情 URL），未命中/失败返回 None。
        """
        # 到这一步就算「走到了搜索档」——记在 record 上的是**实际执行到的深度**，
        # 缓存据此判断下次要不要重验（见 is_cache_item_valid）
        record.probe_depth = DEPTH_SEARCH
        keywords = self.keywords_for(record)
        for kw in keywords[:self.max_keywords]:
            try:
                search_url, method, headers, req_body = parse_search_request(
                    record.search_url_template, kw
                )
                if search_url.startswith("/"):
                    search_url = domain_url + search_url
                elif not search_url.startswith(("http://", "https://")):
                    search_url = domain_url + "/" + search_url
                s_status, s_body, s_cost, s_err = await self._request(
                    session, record, search_url, method=method, headers=headers,
                    body=req_body,
                )
                record.search_response_ms = int(s_cost)
                if s_status is None:
                    record.health = classify_transport_error(s_err)
                    record.error = f"疑似被墙（{_err_desc(s_err)}）" if s_err in ("reset", "tls") else _err_desc(s_err)
                    return None
                # 判定表只有一份（`classify_http_status`）。**这里只取它的 AUTH**：
                # 搜索入口 404 不能推出「源死了」（可能只是搜索规则过期），
                # 所以不套用那张表的 4xx → DEAD。≥500 仍然判死——那是服务端挂了。
                text = _decode_body(s_body)
                s_health = classify_http_status(s_status, text,
                                                record.enabled_cookie_jar)
                if s_health == Health.AUTH:
                    record.health = Health.AUTH
                    return None
                if s_status >= 500:
                    record.health = Health.DEAD
                    return None
                if s_status == 200:
                    # 命中判定：响应体含该测试词，且 bookList 规则真实解析出结果
                    # 仅记录 search_hit 内部字段（用于星级评分与报告），不再打「命中《》」分组标签
                    if kw in text and self._confirm_hit(s_body, record):
                        record.search_hit = kw
                        return s_body
                    # 假阳性：响应体含关键词但列表解析为空 → 继续尝试下一个测试词
            except Exception:
                continue
        return None

    async def _probe_toc(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
        domain_url: str,
        s_body: bytes,
    ) -> Optional[bytes]:
        """深度 2：验证目录完整度。

        命中测试作品后：解析 ruleSearch.bookUrl 取详情页 URL → 请求详情页 →
        解析 ruleToc.chapterList 数章节 → 与参考表比对（小说 ≥80% / 漫画 ≥60%）。
        返回详情页响应体（bytes，供深度3 复用解析章节 URL），失败返回 None。

        三分类失败归因（判定口径已收拢到 core.quality，与「全链路试跑」一致）：
          - 规则缺失 / 含 JS 规则 → toc_complete=None（无法验证，不给分）
          - 详情页请求失败（网络/超时/4xx/5xx） → toc_complete=None（网络失败不判不完整）
          - 解析为空（规则跑不出东西） → toc_complete=False（源的规则确实失效了，
            配置错误与我们的能力边界在这里已经分开）
        解析出章节数后再与参考表比对（小说 ≥80% / 漫画 ≥60%），比不过才判 False。
        """
        record.probe_depth = DEPTH_TOC
        raw = record.raw or {}
        search = raw.get("ruleSearch") or {}
        toc = raw.get("ruleToc") or {}
        book_url_rule = str(search.get("bookUrl", "") or "").strip()
        chapter_list_rule = str(toc.get("chapterList", "") or "").strip()
        if not (book_url_rule and chapter_list_rule):
            record.toc_complete = None
            record.toc_fail_reason = "bookUrl/chapterList 规则缺失"
            return None
        # 判据来自 replayer（原来只查 `"<js"`，漏掉中间形态 `selector@js:` 与
        # 模板/xpath/多规则合并——那些会掉到下面被 apply_css_rule 跑出空，
        # 于是报「解析为空（搜索结果页结构变化？）」，把「工具测不了」说成「源坏了」）
        reason = (parse_rule(book_url_rule).unsupported
                  or parse_rule(chapter_list_rule).unsupported)
        if reason:
            record.toc_complete = None
            record.toc_fail_reason = "bookUrl/chapterList 规则无法离线回放：%s" % reason
            return None
        try:
            html = _decode_body(s_body)
            urls = apply_css_rule(html, _strip_rule_prefix(book_url_rule))
            urls = [str(u).strip() for u in urls if str(u or "").strip()]
            if not urls:
                record.toc_complete = None
                record.toc_fail_reason = "bookUrl 解析为空（搜索结果页结构变化？）"
                return None
            # 取第一条详情 URL（相对链接补全为绝对地址）
            detail_url = _abs_url(domain_url, urls[0])
            d_status, d_body, d_cost, d_err = await self._request(session, record, detail_url)
            if d_status is None or d_status >= 400:
                record.toc_complete = None
                record.toc_fail_reason = f"详情页请求失败(status={d_status})"
                return None
            d_html = _decode_body(d_body)
            # 基础判定交 core.quality（非空即通过）；比例比对留在下面由 checker 叠加，
            # 因为那依赖 TEST_TITLES 参考数据，是 checker 独有的信息
            chapters, hits, rule_error = extract_all_nodes(
                d_html, _strip_rule_prefix(chapter_list_rule),
                Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
            chapters = [str(c).strip() for c in chapters if str(c or "").strip()]
            record.chapter_count = len(chapters)
            # rule= 是必须的：空规则是**源的配置错误**（fail），不是我们的能力边界
            # （unknown）。不传的话这层信息就丢了，正是刚修掉的那个回归。
            toc_verdict = Q.judge_list_step("toc", chapters, "".join(hits), rule_error,
                                            Q.safe_int(record.source_type),
                                            rule=chapter_list_rule)
            if toc_verdict.verdict != Q.VERDICT_PASS:
                record.toc_complete = toc_verdict.checker_state   # False 或 None
                record.toc_fail_reason = (toc_verdict.reason
                                          or "；".join(toc_verdict.notes))
                return None
            ref = self.test_titles.get(record.search_hit)
            if not ref:
                record.toc_complete = None
                record.toc_fail_reason = f"参考表无「{record.search_hit}」数据"
                return None
            threshold = TOC_COMPLETE_THRESHOLD.get(ref["type"], 0.8)
            need = int(ref["chapters"] * threshold)
            record.toc_complete = record.chapter_count >= need
            if not record.toc_complete:
                record.toc_fail_reason = (
                    f"章节数 {record.chapter_count} < 参考 {ref['chapters']}×{threshold}={need}"
                )
            return d_body
        except Exception as e:
            record.toc_complete = None
            record.toc_fail_reason = f"验证异常：{type(e).__name__}"
            return None

    async def _probe_content(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
        domain_url: str,
        toc_body: bytes,
    ) -> None:
        """深度 3：抽样一章验证正文可用性（速度 + 内容）。

        解析 ruleToc.chapterUrl 取章节 URL 列表 → 取**中位章节**（防首章特判/防盗链） →
        请求正文并计时 → ruleContent.content 提取后交 core.quality 判定
        （底线是「非空 / 不报错」，与「全链路试跑」同口径）。
        """
        record.probe_depth = DEPTH_CONTENT
        raw = record.raw or {}
        toc = raw.get("ruleToc") or {}
        content = raw.get("ruleContent") or {}
        chapter_url_rule = str(toc.get("chapterUrl", "") or "").strip()
        content_rule = str(content.get("content", "") or "").strip()
        if not chapter_url_rule:
            record.content_ok = None
            record.content_fail_reason = "chapterUrl 规则缺失"
            return
        # 判据来自 replayer，同 _probe_toc：只查 `"<js"` 会漏掉中间形态 `@js:`
        reason = parse_rule(chapter_url_rule).unsupported
        if reason:
            record.content_ok = None
            record.content_fail_reason = "chapterUrl 规则无法离线回放：%s" % reason
            return
        try:
            html = _decode_body(toc_body)
            urls = apply_css_rule(html, _strip_rule_prefix(chapter_url_rule))
            urls = [str(u).strip() for u in urls if str(u or "").strip()]
            if not urls:
                record.content_ok = None
                record.content_fail_reason = "chapterUrl 解析为空（目录页分页加载？）"
                return
            # 中位章节（>1 时取中间），单章源取唯一章节
            pick = urls[len(urls) // 2] if len(urls) > 1 else urls[0]
            chap_url = _abs_url(domain_url, pick)
            c_status, c_body, c_cost, c_err = await self._request(session, record, chap_url)
            record.content_response_ms = int(c_cost)
            if c_status is None or c_status >= 400:
                record.content_ok = None
                record.content_fail_reason = f"正文请求失败(status={c_status})"
                return
            # 判定收拢到 core.quality：与「全链路试跑」共用同一套口径，
            # 避免同一个源在两个入口得到相反结论
            c_html = _decode_body(c_body)
            if content_rule:
                # 截断上限必须显式传：extract_all_nodes 故意没有默认值，
                # 好让「证据预算」只在 core.quality 里定义一处
                parts, hits, rule_error = extract_all_nodes(
                    c_html, _strip_rule_prefix(content_rule),
                    Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
                verdict = Q.judge_content(Q.safe_int(record.source_type), parts,
                                          content_rule, "".join(hits), rule_error)
            else:
                # 空规则：quality 会按类型分派（文本源 fail / 音图源 pass）
                verdict = Q.judge_content(Q.safe_int(record.source_type), [], "")
            record.content_ok = verdict.checker_state      # True / False / None
            record.content_fail_reason = (
                "" if verdict.verdict == Q.VERDICT_PASS
                else (verdict.reason or "；".join(verdict.notes))
            )
        except Exception as e:
            record.content_ok = None
            record.content_fail_reason = f"验证异常：{type(e).__name__}"

    # ------------------------------------------------------------ 批量校验
    async def run(self, records: List[BookSourceRecord],
                  on_progress=None) -> List[BookSourceRecord]:
        """并发校验所有书源，返回带结果的对象列表。

        若配置了 cache_dir，会读取历史校验结果缓存：
        - 仅规则指纹一致且未过期的缓存才复用，不再发请求
        - 新增、规则变化或缓存过期的源执行校验并追加写入缓存

        ``on_progress(done, total)`` 每批报一次进度。**Web 那条链路必须传**：
        不传的话长任务在界面上永远是 0——`jobs.progress` 没人写，而全量 3800 条
        要跑十几分钟，用户看到的就是「点了没反应」。CLI 不用它（打印够了）。
        """
        # 读取历史缓存，标记已校验的源
        cache = {} if self.refresh_cache else self.load_cache()
        pending: List[BookSourceRecord] = []
        for r in records:
            # 键规范化后再查：缓存两侧的口径见 load_cache 的注释
            item = cache.get(_normalize_url(r.url))
            # 「本次要不要验搜索」由深度推出（wants_search），不再单独传——
            # 深度到「搜索」档就是要验，两根轴能配出没有意义的组合
            if item and is_cache_item_valid(r, item, min_depth=self.probe_depth,
                                            ttl_ok=self.cache_ttl_ok,
                                            ttl_other=self.cache_ttl_other):
                restore_from_cache(r, item)
            else:
                pending.append(r)
        # 复用了几条要说出来：不然「点校验 → 完成」和「一条请求都没发」长得一样
        self.cached_count = len(records) - len(pending)
        total = len(records)
        # 先报一次「起步」：缓存命中的那批是**立刻就算完成的**，进度要从它们算起——
        # 否则界面上会出现「849/849」而任务总数写着 3849，两个数字对不上
        if on_progress:
            on_progress(self.cached_count, total)

        self._sem = asyncio.Semaphore(self.concurrency)
        # 缓存目录就绪
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
        connector = aiohttp.TCPConnector(
            limit=self.concurrency,
            limit_per_host=5,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout,
            headers={"User-Agent": DEFAULT_UA},
        ) as session:
            if pending:
                print(f"待校验 {len(pending)} 个（缓存命中 {len(records) - len(pending)} 个）...")
            # 分批提交，避免一次性创建过多任务内存压力
            batch = 500
            results: List[BookSourceRecord] = []
            for i in range(0, len(pending), batch):
                chunk = pending[i:i + batch]
                chunk_results = await asyncio.gather(*[self.check_one(session, r) for r in chunk])
                results.extend(chunk_results)
                done = i + len(chunk)
                print(f"  进度: {done}/{len(pending)}")
                if on_progress:
                    on_progress(self.cached_count + done, total)
        # **保存条件是「有任一后端」，不是「有 cache_dir」。** 以前只判 cache_dir，
        # 而 Web 那条链路传的是 use_store=True、cache_dir=None，于是校验结果一条都
        # 不写——界面上请求成功、状态不变。这条门和 save_cache_append 里那句
        # makedirs 是同一个故障的两半
        if self.use_store or self.cache_dir:
            for r in results:
                self.save_cache_append(r)
        self.close()
        if self.save_failures:
            print("警告: %d 条校验结果没能写进缓存（状态不会更新）"
                  % self.save_failures)
        if self.hit_downgrades:
            # 降级 = 「规则回放不了」被当成了「命中」，星级会偏高。零星几次是页面
            # 结构异常；成片出现说明是工具本身坏了（依赖缺失之类），这时必须看见。
            # 只印前几条示例：目的是让人判断「零星还是成片」，不是列清单
            print("警告: %d 个源的命中判定降级（规则无法回放，已按「命中」处理）"
                  % len(self.hit_downgrades))
            for line in self.hit_downgrades[:5]:
                print("  " + line)
            if len(self.hit_downgrades) > 5:
                print("  ...（其余 %d 条省略）" % (len(self.hit_downgrades) - 5))
        return records  # 已合并缓存结果


def run_check(
    records: List[BookSourceRecord],
    concurrency: int = 50,
    timeout: float = 8.0,
    keyword: str = "我",
    verify_ssl: bool = True,
    cache_dir: Optional[str] = None,
    show_progress: bool = True,
    testset: Optional[Dict[str, List[str]]] = None,
    max_keywords: int = 2,
    proxy: Optional[str] = None,
    probe_depth: int = DEPTH_HOME,
    refresh_cache: bool = False,
) -> List[BookSourceRecord]:
    """同步入口：运行校验（Windows 上 asyncio.run 即可）。

    probe_depth 一档对一级星级（口径定义在 core/settings_store.py）：
        1 主页   仅域名探测                                   → 1★
        2 搜索   搜索探测 + 命中判定                           → 2★ / 3★
        3 目录   详情页 + 目录页比对章节数                      → 4★
        4 正文   章节页抓一章全文                              → 5★
    """
    checker = AsyncChecker(
        concurrency=concurrency,
        timeout=timeout,
        keyword=keyword,
        verify_ssl=verify_ssl,
        cache_dir=cache_dir,
        testset=testset,
        max_keywords=max_keywords,
        proxy=proxy,
        probe_depth=probe_depth,
    )
    checker.refresh_cache = refresh_cache
    return asyncio.run(checker.run(records))

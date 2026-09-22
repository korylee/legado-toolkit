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
    Health, Engine, BookSourceRecord, ANTI_BOT_MARKERS, LOGIN_MARKERS,
    anti_bot_marker_of, login_marker_of,
    NOVEL_TEST_KEYWORDS, MANGA_TEST_KEYWORDS, TEST_TITLES, TOC_COMPLETE_THRESHOLD,
)
from core.toc_page import resolve_toc_page
from core.urls import abs_url as _abs_url, rule_url as _rule_url, split_url_options
# DNS 失败的归因（域名注销 vs 本地解析被污染）：**外部视角的唯一实现**，
# 判定口径与「为什么不能只凭本机一次解析失败判死」都写在那模块的开头
from core import dns_check
from core.rules.replayer import (extract_all as apply_css_rule, extract_all_nodes,
                          extract_field_in_nodes,
                          parse_rule)
from core.loader import _normalize_url, fingerprint
# 判定口径的唯一来源（与「全链路试跑」共用，避免同源两判）。
# 依赖方向：checker → quality，quality 不依赖 replayer，这是刻意的。
from core import quality as Q
# 限速口径的唯一实现在 quality：checker（异步）与 fetch（同步）共用同一份。
# 早先它定义在本模块，而 fetch 也要用时就只能反向依赖 checker——lessons §十记过
# 这个坑（core 层不该反向依赖）。这里只是引用，不是第二份定义
from core.quality import rate_interval_ms
# 探测深度那套常量（`DEPTH_*` / `PROBE_DEPTHS`）随本地执行体退场（十-4）：
# 今天只有本机引擎在验，档位的枚举与区间都在 `core/jvm_health` / 设置里，
# 这里不再引用——**别顺手补回来**，那会让两处档位定义重新漂开。
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
# 缓存版本 11：登录词表删掉裸 `login` / `sign in`（它们匹配的是页面里的登录**入口**，
# 实测 `m.cread.com` 的首页零反爬词、唯一命中的是 `<a href="/user/login.aspx">`，
# 797 条带 cookieJar 的源因此被判「需验证」），并给「200 + 登录词」判出来的 auth
# 一个短 TTL。口径变了 → 旧缓存里的 auth 是旧逻辑的产物，必须整体作废。
# 缓存版本 10：探测深度从「1/2/3 + 独立的搜索开关」合并成「1/2/3/4 一根轴」，
# 缓存里的 `probe_depth` 是**实际执行到的深度**，所以整列的含义随编号一起平移了
# （旧 1 档 = 域名+搜索 = 新 2 档，旧 2/3 = 新 3/4）。
#
# 严格说旧编号是**单调小于**新编号的，沿用旧行不会放出错误的结论（只会更保守地
# 重验）。但「深度 1 + 搜索开」这档旧行在新口径下必然判「深度不够」，等于整体重跑
# 一遍——把这件事标成版本变化，比让它以「我什么都没改，怎么全量又跑了」的样子
# 出现要好。
# 12：本地回放的能力边界补了两类写法（`text.` / `children.` 简写、方括号索引式
# `[-1]` / `[0]` / `[1,3]` / `[!0]`）。它们此前被判成「解析为空」＝**源失效**，
# 现在一律 unknown（无法离线回放）；同批还修了取值类末段语义（对齐 getResultLast）
# 与 tocUrl 进探针。判定变了，旧缓存里的 toc/content 结论作废。
# 13：健康档位收成六档——timeout / error / no_search / skipped 并入 pending
# （「待验证」），判据是下一步动作相同；AUTH / GFW 的标签改名「需登录 / 需翻墙」。
# 结论词表变了：旧缓存里的 health 是旧词表的产物，必须整体作废（checks 表的
# 历史值由 Store.migrate_health_tiers_once 一次性映射——纯子集合并、观测不变；
# 缓存这边直接重探，重跑是已知代价）。
# 14：目录判定改在 **tocUrl 指向的目录页**上做（此前一律在详情页数章节）——
# 「目录在独立页上」的源（实测 1617 条）从「解析为空＝失效」翻成真实章节数，
# 结论方向变了，旧缓存的 toc/content 必须作废。
# 16：**撤掉「证书问题」这一档**（校验收成 App 引擎后它产不出来：App 要么直接
# 通过——不校验证书信任链，要么报 TLS 阻断 → 需翻墙）。存量 cert 行由
# `Store.migrate_cert_tier_once` 就地映射成 pending（动作相同：重跑一次定案），
# 缓存这边整体作废——**词表变了**（少了一个取值）。
# 15：**多了一根轴：结论是谁判的**（`engine`，见 core/models.Engine）。本机引擎
# （App 真源码）的结论写进同一张 checks 表，而它与本地回放的判据强度不同（本地跑不了
# `@js:`、没有登录态、超时 8s vs App 60s）——缓存条目不记出处的话，换个引擎重跑会
# **静默复用**另一台引擎的结论（AGENTS #5b）。
CACHE_VERSION = 16



def classify_transport_error(error: str) -> str:
    """将传输层错误映射为保守状态，避免断网把源误判为永久失效。

    **DNS 单独说**：这里给的 ``PENDING``（待复检）是**保守兜底**，因为只凭本机一次
    解析失败无法区分「域名注销」和「本机解析被污染/断网」。真正的归因在
    ``check_one`` 里做——它拿 ``core/dns_check`` 的外部视角去交叉验证，验得出来
    才会升级成 ``DEAD``（两个公共 DNS 都说域名不存在）或 ``GFW``（公共 DNS 能解析
    到）。**别在这里改成 DEAD**：那等于用一次本机解析失败判死。
    """
    if error in ("reset", "tls"):
        return Health.GFW
    # timeout / proxy / dns / 其他传输层错误：都是「这次没测出结论」，
    # 下一步动作相同——重跑。原因留在 record.error。
    return Health.PENDING


def dns_verdict_text(verdict: str, note: str) -> Tuple[str, str]:
    """DNS 交叉验证的结论 → ``(健康态, 给用户看的一句话)``。

    **判词只有这一份**：本机引擎那条链（`core/jvm_health`）与 DNS 归因
    结论的映射（``core.jvm_health``）都用它。判定口径在 :mod:`core.dns_check`
    （两个独立来源都同意才判死）；``note`` 说的是"我们观察到了什么"。
    """
    if verdict == dns_check.POLLUTED:
        return (Health.GFW,
                "本机解析失败，但%s，本地 DNS 疑似被污染，可开代理复检" % note)
    if verdict == dns_check.GONE:
        return Health.DEAD, "域名已注销（%s），建议删除" % note
    return Health.PENDING, "DNS 解析失败待复检（%s）" % note


def classify_http_status(status: Optional[int], text: str,
                         enabled_cookie_jar: bool = False) -> str:
    """HTTP 状态码 + 响应体 → 健康态。**全仓库唯一的判定表**。

    需要它的是域名探测（`core/jvm_health` 的归因读到它）与
    `reclassify.diagnose_source`（失效归因）。原来各写一份，实测已在 5 种输入上分叉：

        503（无响应体）         域名探测=dead    归因=需登录
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
    # 503 要看响应体：带反爬特征才算「需登录」，否则是服务端挂了
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


def is_login_wall(text: str, enabled_cookie_jar: bool = False) -> bool:
    """这一页是不是登录墙 / 反爬挑战页。

    判定**完全交给 ``classify_http_status``**（全仓唯一的判定表），这里只回答
    「算不算 AUTH」——状态码固定传 200，因为这是给「已经拿到 HTML 的人」用的：
    403 那类在抓取时就抛了，走不到这里；真正骗人的恰恰是 **200 + 登录页**。

    ``enabled_cookie_jar`` 必须传源自己的：那一档「200 + 登录词」以它为前提
    （「请登录」在正常页面的导航栏里太常见，不声明 cookie 的源不算登录墙）。
    """
    return classify_http_status(200, text, enabled_cookie_jar) == Health.AUTH


def is_inconclusive(health: str) -> bool:
    """这个结论是「我们没测出来」吗（待验证档：超时、异常、从未校验…）。

    **名字说的是这件事本身，不是它的成因**：早先叫 `is_transient`（瞬时网络
    错误），但档位收成六档后这一档的含义就是「没有结论」——它既包含瞬时抖动，
    也包含「压根没跑过」。留着旧名会让人以为它在判「瞬时性」，从而照它加条件。

    它只用来决定"能不能复用"，不再用来决定"要不要写"。
    再早它叫 `should_cache_result`，同时管着写库那道门——结果是超时/异常的源
    **一条都不落库**，而列表是按"有没有 checks 行"算「未校验」的，于是这些源永久
    显示成「未校验」：明明刚跑过，界面上却像没跑（实测 3861 条里有 1222 条是
    这个状态，占 31.7%），而且每次全量都会把它们的请求重打一遍。

    现在拆开：**照写**（界面能显示「❓待验证」、能筛出来单独重测）+ **不复用**
    （一次断网/抖动不会变成源的结论）。原来要防的那件事（污染）靠 `is_cache_item_valid`
    的那道早退照样堵着。
    """
    return health == Health.PENDING


def err_desc(err: str, detail: str = "") -> str:
    """失败原因 → 中文描述（用于诊断信息）。

    ``detail`` 是底层异常类名（``_request`` 的第五个返回值），**只附在「网络异常」
    这一档上**：dns/timeout/reset/tls/cert 的中文描述本身已经指向具体成因，而
    「网络异常」什么也没说——实测那一档底下混着连接被拒、对端断开、协议错误等好几种
    （20 条抽样里 4 种），排查时那句"网络异常"等于没有。
    """
    desc = {
        "dns": "DNS解析失败(待交叉验证)",
        "timeout": "连接超时",
        "reset": "连接被重置(典型被墙特征)",
        "tls": "TLS握手失败(可能SNI阻断)",
        "proxy": "代理连接失败",
        "cert": "证书不被信任(自签/过期/域名不匹配)",
        "other": "网络异常",
    }.get(err, "网络异常")
    if err == "other" and detail:
        return "%s（%s）" % (desc, detail)
    return desc


#: ``split_url_options`` 与 ``_URL_OPTION_RE`` 已抽到 core/urls.py（它是对 URL
#: 语法的解析，不是校验逻辑；补抓 core/app_debug.py 也要用，不该为此导入本模块）。
#: 上面从 core.urls 的导入把它 re-export，test_checker_judge 的导入不受影响。

#: 关键词占位符（Legado 的 searchUrl 模板里可能是这几种写法之一）
_KEYWORD_PLACEHOLDERS = ("{{key}}", "{{searchKey}}", "{{keyword}}", "$searchKey")


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

      1★ 可达：health ∈ (ok / auth)（域名通或能访问，仅需登录）
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
    if health not in (Health.OK, Health.AUTH):
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


def _decode_body(body: bytes) -> str:
    """尝试多种编码解码响应体。"""
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


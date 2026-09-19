# -*- coding: utf-8 -*-
"""
书源数据模型。

将 Legado 书源 JSON 中的字段做类型化封装，
提供书源类型、健康状态等枚举定义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- 类型枚举

#: 书源类型 -> 中文类型名
#: 对齐 Legado BookSourceType.kt:8-11 —— 0 文本 / 1 音频 / 2 图片 / 3 只提供下载服务的网站。
#: 注意：Legado 的 @IntDef 里没有 4，任何 4 都是脏值，不得在此定义中文名。
BOOK_SOURCE_TYPE_NAMES: Dict[int, str] = {
    0: "📖小说",
    1: "🎧听书",
    2: "🎨漫画",
    3: "📥下载",
}

#: 校验健康状态。**六档，按「下一步动作」划分**（2026-09 档位重设计）：
#: 两档该不该合并，唯一判据是动作是否相同——删 / 修 / 重测 / 翻墙 / 连 App 试。
class Health:
    OK = "ok"                # ✅ 可用——直接用
    DEAD = "dead"            # ❌ 已失效（域名不可达）——删
    GFW = "gfw"              # 🌐 需翻墙（DNS污染/连接重置/TLS阻断）——挂代理复测
    AUTH = "auth"            # 🔒 需登录（403/验证码/登录墙）——连 App 试
    #: 🔐 证书问题（自签/过期/域名不匹配）。**单独一档**的理由：它是这批源里唯一
    #: 「我们自己能处理」的一类——站点本身是通的，关掉证书校验（设置里的
    #: verify_ssl）或用 http 就能用。混进「待验证」时用户只看到"没结论"，
    #: 既不知道该翻墙、该删源，还是该关校验。实测 20 条异常抽样里有 1 条是它
    CERT = "cert"
    #: ❓ 待验证——**我们没结论**。吸收旧档 timeout / error / no_search / skipped
    #: 和「从未校验」：它们的下一步动作完全相同（跑/重跑一次校验），分档只是在
    #: 罗列失败原因，用户分不出来也不该让他分。失败**原因**不丢——落在 checks
    #: 的 error 与 steps 里，列表 tooltip 仍可见。档名必须是「待验证」这类
    #: 非断言：「异常」是肯定断言，会把一次请求都没发过的新源凭空标成坏的。
    PENDING = "pending"


#: 健康状态中文名。**这是唯一一份**（`core/organizer.py` 拿它写书源分组名，
#: CLI 拿它打印分布，前端 `utils/health.js` 是显示层副本——那份的注释里写了
#: 为什么允许存在、以哪份为准）
HEALTH_NAMES: Dict[str, str] = {
    Health.OK: "✅可用",
    Health.AUTH: "🔒需登录",
    Health.GFW: "🌐需翻墙",
    Health.CERT: "🔐证书问题",
    Health.PENDING: "❓待验证",
    Health.DEAD: "❌已失效",
}

# ---------------------------------------------------------------- 失效/异常特征词

#: 分组标签中表示"失效"的信号词
DEAD_TAG_PATTERNS: List[str] = [
    "失效", "网站失效", "搜索失效", "搜索目录失效", "发现失效",
    "js失效", "校验超时", "搜索链接规则为空",
]

#: 响应体中出现的"反爬/验证"特征
ANTI_BOT_MARKERS: List[str] = [
    "验证码", "人机验证", "安全验证", "滑动验证",
    "cf-challenge", "__cf_chl", "captcha", "verify you are human",
    "访问验证", "继续访问", "安全检测",
]

#: **不要往上面的表里加裸 ``cloudflare``。** 它匹配的是 Cloudflare 的邮箱保护脚本
#: （``/cdn-cgi/scripts/.../cloudflare-static/email-decode.min.js``）——站点只要拿
#: Cloudflare 当 CDN 就会被注入，与反爬无关。实测：m.manhuahao.com 的首页与搜索
#: 响应都带这个脚本，于是 _probe_search 走早退分支判 AUTH 并直接 return，连 bookList
#: 命中判定都不做，health 由 ok 变 auth、星级由 5★ 压到 3★。去掉后同一条源命中
#: 《海贼王》、目录 1197 章。真正的挑战标记 ``cf-challenge`` / ``__cf_chl``
#: 以及 403/503 状态码已经够用。
#: （回归护栏见 tests/test_checker_judge.py::TestAntiBotMarkers）

#: 响应体中出现的「需要登录」特征。**与上面的反爬词表同一条纪律：不要放裸的英文词。**
#:
#: 原来这里有裸的 `login` / `sign in`，它们匹配的是页面里的登录**入口**而不是登录墙：
#: 实测 `m.cread.com` 的 33KB 首页零反爬词，唯一命中的是
#: `<a href="/user/login.aspx">` —— 配上有 cookieJar 的源，**797 条**被判「需验证」，
#: 而它们在 App 里是好的。这与 v7 删掉裸 `cloudflare` 是同一类错。
#:
#: 英文要留就留**只在登录墙上出现的短语**（下面的三个），别再用单词。
LOGIN_MARKERS: List[str] = [
    "请登录", "需要登录", "登录后", "未登录",
    "please log in", "please login", "login required", "sign in to continue",
]


def anti_bot_marker_of(text: str) -> str:
    """命中的反爬特征词（没有则空串）。给「为什么判需登录/异常」留痕用。"""
    low = str(text or "").lower()
    for m in ANTI_BOT_MARKERS:
        if m in low:
            return m
    return ""


def login_marker_of(text: str) -> str:
    """命中的登录特征词（没有则空串）。同上，给留痕用。"""
    low = str(text or "").lower()
    for m in LOGIN_MARKERS:
        if m in low:
            return m
    return ""

#: 内置优质度检测测试集：按书源类型选择使用的作品名
#: 小说源/听书源使用 NOVEL_TEST_KEYWORDS，漫画源使用 MANGA_TEST_KEYWORDS
NOVEL_TEST_KEYWORDS: List[str] = [
    "斗破苍穹", "凡人修仙传", "赘婿", "诡秘之主", "庆余年",
]
MANGA_TEST_KEYWORDS: List[str] = [
    "海贼王", "火影忍者", "斗罗大陆", "进击的巨人", "龙珠",
]

#: 目录完整度参考表：测试作品在官方平台的正版章节数（数据截至 2026-08，用于深度验证比例比对）
#: 判定规则：解析出的章节数 >= 参考数 * 阈值（小说 0.8 / 漫画 0.6）即视为目录完整。
#: 阈值容忍：连载作品持续增长、不同平台分卷差异、目录分页加载等情况。
#: 连载中作品的参考数会随更新过时，由阈值 + 报告标注数据日期缓解。
TEST_TITLES: Dict[str, Dict[str, Any]] = {
    # 小说（起点/QQ阅读，完结或连载稳定数据）
    "斗破苍穹":   {"type": "novel", "chapters": 1681},  # 起点目录，2011 完结，共 1681 章
    "凡人修仙传": {"type": "novel", "chapters": 2446},  # 起点，2013 完本，两千四百四十六章
    "赘婿":       {"type": "novel", "chapters": 1358},  # 起点连载中（2026 数据，含番外）
    "诡秘之主":   {"type": "novel", "chapters": 1432},  # 起点，2020 完结（约，共 8 卷）
    "庆余年":     {"type": "novel", "chapters": 826},   # 起点，2009 完结，全书 826 章
    # 漫画（正版权数；海贼王连载中）
    "海贼王":     {"type": "manga", "chapters": 1190},  # 连载中，2026-08 更新至第 1190 话
    "火影忍者":   {"type": "manga", "chapters": 700},   # 完结，全 700 话
    "斗罗大陆":   {"type": "manga", "chapters": 750},   # 2025-09 完结（穆逢春版约 750 话）
    "进击的巨人": {"type": "manga", "chapters": 139},   # 完结，全 139 话
    "龙珠":       {"type": "manga", "chapters": 519},   # 完结，全 519 章
}

#: 目录完整度比例阈值：按作品类型区分（小说目录完整要求更高）
TOC_COMPLETE_THRESHOLD: Dict[str, float] = {"novel": 0.8, "manga": 0.6}


@dataclass
class BookSourceRecord:
    """单个书源的诊断视图（只读，不修改原始 JSON）。"""
    raw: Dict[str, Any] = field(default_factory=dict)
    index: int = -1                    # 在原文件中的序号
    name: str = ""
    group: str = ""
    source_type: int = 0
    url: str = ""
    enabled: bool = True
    comment: str = ""
    enabled_cookie_jar: bool = False   # 供 checker 判「登录墙」用（有读者）
    has_search: bool = False           # 是否有**可探测**的搜索（searchUrl 或 ruleSearch.url）
    search_url_template: str = ""      # 提取的搜索 URL 模板
    # ---- 静态诊断 ----
    dead_tagged: bool = False          # 分组/备注标注失效（reporter 在读）
    #
    # 这里删掉了一批「算了没人读」的字段（2026-09-16 逐个 grep 确认零读者）：
    #   login_url / has_book_list / has_explore / has_header_js / has_login_js /
    #   concurrent_rate / auth_tagged，以及**没有声明过、动态挂上去的** raw_book_list。
    # 它们大多能从 `raw` 现算，留着只会让人以为有人在用。
    #
    # 两处**有意的例外**，不在这里、也不该顺手补回来：
    #   - `dead_tagged` 留着：`core/reporter.py` 在读它（同名的 `auth_tagged` 没人读，删了）
    #   - `has_search` 留着：探测门与星级都在读
    # 另外 `AUTH_TAG_PATTERNS` 也随 `auth_tagged` 一起删了——它的唯一读者就是那个字段。
    # ---- 动态校验结果 ----
    health: str = Health.PENDING
    status_code: int = 0
    response_time_ms: int = 0
    error: str = ""
    checked_at: str = ""
    # ---- 优质度检测结果 ----
    search_hit: str = ""             # 命中的测试作品名（空=未命中/未测）
    search_response_ms: int = 0      # 搜索请求响应耗时
    quality_stars: int = 0           # 星级 0-5
    #: 这个星级是**实测**来的还是**按静态规则推的**（见 checker.evaluate_stars）：
    #: "measured" / "static" / ""（0★ 不可达，无可标注）。**必须与 quality_stars 一起
    #: 看**——3★ 有「搜索实测命中」和「只是规则齐全」两种来源，光看星级分不出来。
    star_basis: str = ""
    quality_tags: List[str] = field(default_factory=list)  # 如 ["规则完整"]（命中不在此打标签，见 search_hit）
    # ---- 深度验证结果（probe_depth >= 2 时填充；None=未验证/无法验证）----
    probe_depth: int = 1             # 实际执行的验证深度（1=浅探测 / 2=+目录 / 3=+正文）
    chapter_count: int = 0           # 目录解析出的章节数
    toc_complete: Optional[bool] = None  # 目录完整度（True=达标 / False=不达标 / None=无法验证）
    toc_fail_reason: str = ""        # 目录验证失败归因（网络失败/规则解析失败/参考表缺项）
    content_ok: Optional[bool] = None    # 正文可用性（True/False/None=无法验证）
    content_fail_reason: str = ""    # 正文验证失败归因
    content_response_ms: int = 0     # 抽样章节正文响应耗时

    @property
    def type_name(self) -> str:
        # 兜底为空串：4 之类的脏值在 Legado 里不存在类型名，不再编造「未知」
        return BOOK_SOURCE_TYPE_NAMES.get(self.source_type, "")

    @property
    def health_name(self) -> str:
        return HEALTH_NAMES.get(self.health, self.health)

    @property
    def host(self) -> str:
        """提取域名主机名。"""
        m = re.search(r"//([^/]+)", self.url)
        if m:
            return m.group(1).rstrip("/")
        return self.url.rstrip("/")


def build_record(raw: Dict[str, Any], index: int) -> BookSourceRecord:
    """从原始 JSON dict 构建诊断视图。"""
    rec = BookSourceRecord(raw=raw, index=index)
    rec.name = str(raw.get("bookSourceName", "") or "")
    rec.group = str(raw.get("bookSourceGroup", "") or "")
    rec.source_type = int(raw.get("bookSourceType", 0) or 0)
    rec.url = str(raw.get("bookSourceUrl", "") or "")
    rec.enabled = bool(raw.get("enabled", True))
    rec.comment = str(raw.get("bookSourceComment", "") or "")
    rec.enabled_cookie_jar = bool(raw.get("enabledCookieJar", False))

    # 搜索规则提取
    rule_search = raw.get("ruleSearch", {}) or {}
    if not isinstance(rule_search, dict):
        rule_search = {}
    raw_search_url = str(raw.get("searchUrl", "") or "")
    rule_url = str(rule_search.get("url", "") or "")
    tpl = raw_search_url or rule_url
    # **先剥 `@` 后缀、再判 `has_search`**——顺序很关键（实测 252 条踩过）：
    # `@js:` 形态的 searchUrl **整段都落在 `@` 之后**，剥完是空串。先判的话会留下
    # 「有搜索规则、模板却是空」的矛盾态，checker 拿这个空模板去拼 `domain + "/"`，
    # **把站点首页当搜索页打**：白费一次请求，而且结论无从解释。
    # 剥了再判，「我们回放不了的搜索」自然就落成「没有搜索」。
    #
    # 后缀本身不必在这里解读：`parse_search_request` 才是**唯一**懂
    # `@POST` / `@headers=` / `@Cookie=` 的地方，让它去认，别再存第二份。
    if "@" in tpl:
        tpl = tpl.rpartition("@")[0]
    rec.search_url_template = tpl
    rec.has_search = bool(tpl)
    # 这里原本还有一条「无 url 但有 bookList → has_search = False」。剥 `@` 挪到
    # 前面之后它成了空操作（模板为空时上面已经给了 False），删掉。
    #
    # 这里原本还算了 header / loginCheckJs / loginUi / exploreUrl 的四个静态标记
    # （has_header_js / has_login_js / has_explore，以及动态挂上去的 raw_book_list），
    # **都已删除**——逐个 grep 确认零读者。

    # 静态失效标注（`dead_tagged` 是这里唯一有读者的产物，见 reporter.py）
    combined_tag = f"{rec.group} {rec.comment} {rec.name}"
    rec.dead_tagged = any(p in combined_tag for p in DEAD_TAG_PATTERNS)

    return rec
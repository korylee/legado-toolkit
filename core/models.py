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

#: 校验健康状态
class Health:
    OK = "ok"                # ✅ 可用
    DEAD = "dead"            # ❌ 失效（域名不可达）
    GFW = "gfw"              # 🔒 需翻墙（DNS污染/连接重置/TLS阻断）
    AUTH = "auth"            # 🔒 需登录/验证（403/验证码/登录页）
    NO_SEARCH = "no_search"  # 🔍 不可搜索（无搜索规则）
    TIMEOUT = "timeout"      # ⏱ 超时
    SKIPPED = "skipped"      # ⏭ 跳过（enabled=false）
    ERROR = "error"          # ⚠️ 校验异常


#: 健康状态中文名
HEALTH_NAMES: Dict[str, str] = {
    Health.OK: "✅可用",
    Health.DEAD: "❌失效",
    Health.GFW: "🔒需翻墙",
    Health.AUTH: "🔒需验证",
    Health.NO_SEARCH: "🔍不可搜",
    Health.TIMEOUT: "⏱超时",
    Health.SKIPPED: "⏭跳过",
    Health.ERROR: "⚠️异常",
}

# ---------------------------------------------------------------- 失效/异常特征词

#: 分组标签中表示"失效"的信号词
DEAD_TAG_PATTERNS: List[str] = [
    "失效", "网站失效", "搜索失效", "搜索目录失效", "发现失效",
    "js失效", "校验超时", "搜索链接规则为空",
]

#: 需要登录/验证的信号词（出现在 group / comment 中）
AUTH_TAG_PATTERNS: List[str] = [
    "需登录", "需要登录", "登录", "人机验证", "验证码", "cf盾",
    "CF盾", "cf验证", "cf盾", "验证", "需梯子", "被墙", "墙",
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

#: 响应体中出现的"需要登录"特征
LOGIN_MARKERS: List[str] = [
    "请登录", "需要登录", "登录后", "未登录", "login", "sign in",
]

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

#: 原创/自写/整理标记信号词（出现在名称、分组或备注中）
ORIGINAL_TAG_PATTERNS: List[str] = [
    "自写", "自建", "原创", "修复", "整理", "自制",
    "自用", "精排", "重做",
]


@dataclass
class SearchTarget:
    """从书源规则中提取的搜索目标信息。"""
    url_template: str = ""       # searchUrl 或 ruleSearch.url
    method: str = "GET"          # GET/POST
    has_book_list_rule: bool = False  # 是否有 bookList 抽取规则
    keyword_placeholder: str = "{{key}}"  # 关键词占位符
    raw_search_url: str = ""     # 原始 searchUrl 字段
    raw_book_list: str = ""      # 原始 ruleSearch.bookList


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
    login_url: str = ""
    enabled_cookie_jar: bool = False
    has_search: bool = False           # 是否有搜索能力（searchUrl 或 ruleSearch.url）
    has_book_list: bool = False        # ruleSearch.bookList 非空
    has_explore: bool = False          # exploreUrl 非空
    is_js_search: bool = False         # 搜索规则是 JS 实现（无法纯 HTTP 验证）
    has_header_js: bool = False        # header 含 js
    has_login_js: bool = False         # loginUrl 含 js
    concurrent_rate: str = ""          # 并发限制
    # ---- 静态诊断 ----
    dead_tagged: bool = False          # 分组/备注标注失效
    auth_tagged: bool = False          # 标注需登录/验证
    search_url_template: str = ""      # 提取的搜索 URL 模板
    search_method: str = "GET"
    # ---- 动态校验结果 ----
    health: str = Health.SKIPPED
    status_code: int = 0
    response_time_ms: int = 0
    error: str = ""
    checked_at: str = ""
    # ---- 优质度检测结果 ----
    search_hit: str = ""             # 命中的测试作品名（空=未命中/未测）
    search_response_ms: int = 0      # 搜索请求响应耗时
    quality_stars: int = 0           # 星级 0-5
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
    rec.login_url = str(raw.get("loginUrl", "") or "")
    rec.enabled_cookie_jar = bool(raw.get("enabledCookieJar", False))
    rec.concurrent_rate = str(raw.get("concurrentRate", "") or "")

    # 搜索规则提取
    rule_search = raw.get("ruleSearch", {}) or {}
    if not isinstance(rule_search, dict):
        rule_search = {}
    rec.raw_book_list = str(rule_search.get("bookList", "") or "")
    raw_search_url = str(raw.get("searchUrl", "") or "")
    rule_url = str(rule_search.get("url", "") or "")
    rec.search_url_template = raw_search_url or rule_url
    rec.has_search = bool(rec.search_url_template)
    rec.has_book_list = bool(rec.raw_book_list)
    rec.is_js_search = "<js" in rec.raw_book_list or "<js" in rec.search_url_template
    # 若 bookList 为纯 JS 且无 url，则无法纯 HTTP 验证搜索
    if not rec.search_url_template and rec.raw_book_list:
        rec.has_search = False  # 只有 JS bookList 而无 URL，视为不可纯HTTP搜索

    # 探索规则
    explore = str(raw.get("exploreUrl", "") or "")
    rec.has_explore = bool(explore)

    # header / login js 标记
    header = str(raw.get("header", "") or "")
    rec.has_header_js = "<js" in header or "@js" in header
    login_js = str(raw.get("loginCheckJs", "") or "") + str(raw.get("loginUi", "") or "")
    rec.has_login_js = "<js" in login_js or "@js" in login_js or "<js" in rec.login_url

    # 静态失效/验证标注
    combined_tag = f"{rec.group} {rec.comment} {rec.name}"
    rec.dead_tagged = any(p in combined_tag for p in DEAD_TAG_PATTERNS)
    rec.auth_tagged = any(p in combined_tag for p in AUTH_TAG_PATTERNS)

    # 搜索方法判定（searchUrl 可能带 @POST 后缀）
    if "@" in rec.search_url_template:
        parts = rec.search_url_template.rsplit("@", 1)
        if len(parts) == 2:
            tail = parts[1].upper()
            if tail.startswith("POST") or tail == "POST" or "POST" in tail:
                rec.search_method = "POST"
            rec.search_url_template = parts[0]

    return rec
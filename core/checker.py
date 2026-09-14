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
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from core.models import (
    Health, BookSourceRecord, build_record, ANTI_BOT_MARKERS, LOGIN_MARKERS,
    NOVEL_TEST_KEYWORDS, MANGA_TEST_KEYWORDS, TEST_TITLES, TOC_COMPLETE_THRESHOLD,
)
from core.urls import abs_url as _abs_url
from core.rules.replayer import (extract_all as apply_css_rule, extract_all_ex,
                          rule_kind, parse_list, parse_field, rule_supported)
from core.loader import fingerprint

# 常见 User-Agent（规避简单 UA 拦截）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 常见爬虫/安全拦截状态码
BLOCKED_STATUS = {403, 429, 401, 503, 406}

# 缓存版本 5：加入规则指纹和状态有效期，规则变化或结果过期均需重新校验。
CACHE_VERSION = 5
CACHE_TTL_DAYS = {
    Health.OK: 14,
    Health.AUTH: 7,
    Health.GFW: 7,
}


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


def parse_search_request(url_template: str, keyword: str) -> tuple[str, str, Dict[str, str]]:
    """
    解析 searchUrl 模板，返回 (请求URL, 方法, 附加Header)。

    Legado 的 searchUrl 支持：
        "https://host/search?q={{key}}"
        "https://host/search?q={{key}}@POST"
        "https://host/search?q={{key}}@headers=XXX"
        "https://host/search?q={{key}}@Cookie=xxx"
    关键词占位符可能是 {{key}} / {{searchKey}} / {{keyword}}
    """
    method = "GET"
    headers: Dict[str, str] = {}
    url_part = url_template

    # 按 @ 拆方法/头（注意 URL 本身可含 @，尽量从后向前取最后一个 @POST）
    for token in sorted(["@POST", "@post", "@GET", "@get", "@headers=", "@Cookie="], key=len, reverse=True):
        idx = url_part.rfind(token)
        if idx > 0:
            if token in ("@POST", "@post"):
                method = "POST"
                url_part = url_part[:idx]
            elif token in ("@GET", "@get"):
                method = "GET"
                url_part = url_part[:idx]
            elif token == "@headers=":
                # headers=后接 JSON 或名称
                rest = url_part[idx + len(token):]
                url_part = url_part[:idx]
                if rest.startswith("{"):
                    try:
                        headers = json.loads(rest)
                    except Exception:
                        pass
            elif token == "@Cookie=":
                rest = url_part[idx + len(token):]
                url_part = url_part[:idx]
                headers["Cookie"] = rest
            break

    # 关键词占位符替换（中文关键词需 URL 编码）
    encoded_keyword = quote(keyword, safe="")
    for ph in ("{{key}}", "{{searchKey}}", "{{keyword}}", "$searchKey"):
        if ph in url_part:
            url_part = url_part.replace(ph, encoded_keyword)
            break
    # 分页占位符替换为 1
    url_part = url_part.replace("{{page}}", "1")
    return url_part, method, headers


def build_domain_url(url: str) -> str:
    """从 bookSourceUrl 构造用于连通性探测的根 URL。"""
    url = (url or "").strip()
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
) -> bool:
    """判断缓存是否仍可用于该书源。

    复用必须满足：缓存版本正确、规则指纹一致、校验时间有效且不在未来。
    可用源保留 14 天，其余健康状态均保留 7 天，避免待验证/需代理复检长期停留在旧结论。
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
    ttl_days = CACHE_TTL_DAYS.get(str(item.get("health", "")), 7)
    return current_time - checked_time <= timedelta(days=ttl_days)


def calc_stars(
    health: str,
    has_search: bool,
    search_response_ms: int,
    search_hit: str,
    toc_complete: Optional[bool],
    content_ok: Optional[bool],
    raw: Optional[Dict[str, Any]] = None,
) -> int:
    """
    星级评分（0-5★）阶梯规则——每一级依赖上一级（方案D 分档宽松）：

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
        return 0
    stars = 1  # 可达
    if not (has_search and search_response_ms > 0):
        return stars
    stars = 2  # 搜索连通
    if not (search_hit or static_rule_complete(raw)):
        return stars
    stars = 3  # 命中 或 静态规则完整（弱证据档）
    if not search_hit:
        return stars  # 未命中源最高 3★
    # 目录完整：实测优先，None（无法验证）回退静态规则；False 仍不达标
    toc_ok = toc_complete if toc_complete is not None else _static_toc_ok(raw)
    if not toc_ok:
        return stars
    stars = 4  # 目录完整
    # 正文可用：实测优先，None（无法验证）回退静态规则；False 仍不达标
    content_ok_ = content_ok if content_ok is not None else _static_content_ok(raw)
    if not content_ok_:
        return stars
    return 5  # 正文可用


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
    # 深度验证字段（v3 旧缓存无这些字段 → 按浅探测默认值处理）
    rec.probe_depth = int(item.get("probe_depth", 1) or 1)
    rec.chapter_count = int(item.get("chapter_count", 0) or 0)
    rec.toc_complete = item.get("toc_complete")  # None / True / False
    rec.toc_fail_reason = item.get("toc_fail_reason", "")
    rec.content_ok = item.get("content_ok")      # None / True / False
    rec.content_fail_reason = item.get("content_fail_reason", "")
    rec.content_response_ms = int(item.get("content_response_ms", 0) or 0)
    # 星级重算（规则升级后旧缓存不失效；方案D：实测优先、None 回退静态，无需配置深度）
    rec.quality_stars = calc_stars(
        health=rec.health,
        has_search=rec.has_search,
        search_response_ms=rec.search_response_ms,
        search_hit=rec.search_hit,
        toc_complete=rec.toc_complete,
        content_ok=rec.content_ok,
        raw=rec.raw,
    )
    # 标签清洗与补齐：剔除「命中《》」、保留静态「原创」、按静态规则补齐「规则完整」
    cached_tags = [t for t in (item.get("quality_tags", []) or []) if not t.startswith("命中")]
    merged = list(dict.fromkeys(cached_tags + [t for t in rec.quality_tags if t == "原创"]))
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
        probe_search: bool = True,
        keyword: str = "我",
        verify_ssl: bool = True,
        cache_dir: Optional[str] = None,
        testset: Optional[Dict[str, List[str]]] = None,
        max_keywords: int = 2,
        proxy: Optional[str] = None,
        probe_depth: int = 1,
        test_titles: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.concurrency = concurrency
        self.timeout = timeout
        self.probe_search = probe_search
        self.keyword = keyword
        self.verify_ssl = verify_ssl
        self.cache_dir = cache_dir
        self.proxy = proxy  # 可选代理（socks5:///http://），用于翻墙源复检
        # 测试集：{"novel": [...], "manga": [...]}，缺省使用内置
        self.testset = testset or {}
        self.novel_keywords = self.testset.get("novel") or NOVEL_TEST_KEYWORDS
        self.manga_keywords = self.testset.get("manga") or MANGA_TEST_KEYWORDS
        self.max_keywords = max_keywords  # 每个源最多尝试测试的关键词数
        # 探测深度：1=浅探测（现状速度，静态规则判星级）
        #           2=命中后验证目录完整度（参考表比例比对）
        #           3=再抽样一章验证正文可用性（速度+内容）
        self.probe_depth = probe_depth if probe_depth in (1, 2, 3) else 1
        # 目录完整度参考表：{作品名: {"type": "novel|manga", "chapters": N}}，缺省内置 TEST_TITLES
        self.test_titles = test_titles or TEST_TITLES
        self._sem: Optional[asyncio.Semaphore] = None
        self.refresh_cache = False

    def keywords_for(self, record: BookSourceRecord) -> List[str]:
        """按书源类型选择测试关键词（漫画源用漫画名，其余用小说名）。"""
        if record.source_type == 2:
            return self.manga_keywords
        return self.novel_keywords

    # ------------------------------------------------------------ 缓存
    def _cache_path(self, url_key: str) -> str:
        return os.path.join(self.cache_dir, f"check_{url_key}.ndjson") if self.cache_dir else ""

    def load_cache(self) -> Dict[str, Dict[str, Any]]:
        """读取历史校验结果缓存 {url: result}。"""
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
                            cache[item.get("url", "")] = item
                        except Exception:
                            continue
            except Exception:
                continue
        return cache

    def save_cache_append(self, record: BookSourceRecord) -> None:
        """追加一条校验结果到缓存。"""
        if not self.cache_dir or not should_cache_result(record.health):
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        url_key = re.sub(r"[^\w\-.]", "_", record.url or f"idx{record.index}")[:80]
        path = self._cache_path(url_key)
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
            "quality_stars": record.quality_stars,
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
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ 单源探测
    async def _request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        allow_redirects: bool = True,
    ) -> tuple[Optional[int], bytes, float, str]:
        """发请求，返回 (状态码, 响应体, 耗时ms, 失败原因)。

        失败原因分为：dns / timeout / reset / tls / proxy / other
        用于区分「真死」与「被墙」。
        """
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
            # 超时可能是 aiohttp 的 ServerTimeoutError（非 asyncio.TimeoutError）
            if isinstance(e, (TimeoutError, OSError)) or "timed out" in str(e).lower():
                err = "timeout"
            elif isinstance(e, OSError) and getattr(e, "winerror", None) == 10054:
                err = "reset"
            elif isinstance(e, OSError) and getattr(e, "winerror", None) in (10060, 10061):
                err = "timeout"
            elif isinstance(e, OSError) and getattr(e, "winerror", None) == 10061:
                err = "reset"
        return None, b"", (time.perf_counter() - t0) * 1000, err

    def _classify(self, status: Optional[int], body: bytes, rec: BookSourceRecord) -> str:
        """根据 HTTP 状态与响应体判定健康状态。"""
        if status is None:
            return Health.DEAD
        text = _decode_body(body)
        # 状态码拦截
        if status in (403, 401):
            if any(m in text.lower() for m in ANTI_BOT_MARKERS):
                return Health.AUTH
            return Health.AUTH
        if status in (429,):
            return Health.AUTH
        if status == 503 and any(m in text.lower() for m in ANTI_BOT_MARKERS):
            return Health.AUTH
        if status >= 500:
            return Health.DEAD
        # 200 时看响应体特征
        if status == 200:
            low = text.lower()
            if any(m in low for m in ANTI_BOT_MARKERS):
                return Health.AUTH
            if any(m in low for m in LOGIN_MARKERS) and rec.enabled_cookie_jar:
                return Health.AUTH
            return Health.OK
        return Health.OK  # 其他 2xx/3xx 视为可达

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
                return record

            # 1) 域名连通性探测
            domain_url = build_domain_url(record.url)
            status, body, cost, err = await self._request(session, domain_url)
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
                else:
                    record.error = "连接失败/超时/DNS错误" if not err else _err_desc(err)
                    if err:
                        health = classify_transport_error(err)

            # 2) 若域名可达且有搜索规则，用测试集探测搜索并判定命中
            s_body: Optional[bytes] = None
            if health == Health.OK and self.probe_search and record.has_search:
                record.health = health
                s_body = await self._probe_search(session, record, domain_url)
                health = record.health

            # 3) 深度验证：命中测试作品后才链式验证目录（深度2）与正文（深度3）
            #    fail-fast：未命中/搜索不通 → 零额外请求；目录验证失败 → 不再验证正文
            #    注意：此处条件必须用配置深度 self.probe_depth（而非 record.probe_depth，
            #    后者由 _probe_toc/_probe_content 内部记录"实际执行到的深度"，目录阶段已置 2）
            if record.health == Health.OK and record.search_hit and self.probe_depth >= 2 and s_body:
                toc_body = await self._probe_toc(session, record, domain_url, s_body)
                if self.probe_depth >= 3 and toc_body:
                    await self._probe_content(session, record, domain_url, toc_body)

            record.health = health
            # 4) 计算星级（阶梯规则公共函数，方案D：实测优先、None 回退静态）
            record.quality_stars = calc_stars(
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
        """
        rule = str(((record.raw or {}).get("ruleSearch") or {}).get("bookList", "") or "").strip()
        if not rule:
            return True
        if "<js" in rule or rule.startswith("js:") or "@xpath" in rule:
            return True
        try:
            html = _decode_body(s_body)
            items = apply_css_rule(html, _strip_rule_prefix(rule))
            items = [i for i in items if str(i or "").strip()]
            return len(items) >= 1
        except Exception:
            return True

    async def _probe_search(
        self,
        session: aiohttp.ClientSession,
        record: BookSourceRecord,
        domain_url: str,
    ) -> Optional[bytes]:
        """用测试集关键词逐个尝试搜索，命中即停；同时记录搜索响应时间。

        返回命中的搜索响应体（bytes，供深度2 复用解析详情 URL），未命中/失败返回 None。
        """
        keywords = self.keywords_for(record)
        for kw in keywords[:self.max_keywords]:
            try:
                search_url, method, headers = parse_search_request(
                    record.search_url_template, kw
                )
                if search_url.startswith("/"):
                    search_url = domain_url + search_url
                elif not search_url.startswith(("http://", "https://")):
                    search_url = domain_url + "/" + search_url
                s_status, s_body, s_cost, s_err = await self._request(
                    session, search_url, method=method, headers=headers
                )
                record.search_response_ms = int(s_cost)
                if s_status is None:
                    record.health = classify_transport_error(s_err)
                    record.error = f"疑似被墙（{_err_desc(s_err)}）" if s_err in ("reset", "tls") else _err_desc(s_err)
                    return None
                if s_status in (403, 401, 429):
                    record.health = Health.AUTH
                    return None
                if s_status >= 500:
                    record.health = Health.DEAD
                    return None
                if s_status == 200:
                    text = _decode_body(s_body)
                    if any(m in text.lower() for m in ANTI_BOT_MARKERS):
                        record.health = Health.AUTH
                        return None
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

        三分类失败归因（保守策略：无法验证不给分、不误判）：
          - 规则缺失 / 含 JS 规则 → toc_complete=None（无法验证，不给分）
          - 详情页请求失败（网络/超时/4xx/5xx） → toc_complete=None（网络失败不判不完整）
          - 解析为空（CSS 规则跑不出东西） → toc_complete=None（规则解析失败，无法验证）
        仅当真的解析出章节数并与参考表比对后，才给出 True/False 判定。
        """
        record.probe_depth = 2
        raw = record.raw or {}
        search = raw.get("ruleSearch") or {}
        toc = raw.get("ruleToc") or {}
        book_url_rule = str(search.get("bookUrl", "") or "").strip()
        chapter_list_rule = str(toc.get("chapterList", "") or "").strip()
        if not (book_url_rule and chapter_list_rule):
            record.toc_complete = None
            record.toc_fail_reason = "bookUrl/chapterList 规则缺失"
            return None
        if "<js" in book_url_rule or "<js" in chapter_list_rule:
            record.toc_complete = None
            record.toc_fail_reason = "bookUrl/chapterList 含 JS 规则，无法用 CSS 验证"
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
            d_status, d_body, d_cost, d_err = await self._request(session, detail_url)
            if d_status is None or d_status >= 400:
                record.toc_complete = None
                record.toc_fail_reason = f"详情页请求失败(status={d_status})"
                return None
            d_html = _decode_body(d_body)
            chapters = apply_css_rule(d_html, _strip_rule_prefix(chapter_list_rule))
            chapters = [str(c).strip() for c in chapters if str(c or "").strip()]
            record.chapter_count = len(chapters)
            if not chapters:
                record.toc_complete = None
                record.toc_fail_reason = "chapterList 解析为空（规则跑不了或目录分页加载）"
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
        请求正文并计时 → ruleContent.content 提取文本（>100 字符）判定；
        文本不达标或 content 规则缺失时，回退 ruleContent.image（图片 URL >= 3 张）。
        """
        record.probe_depth = 3
        raw = record.raw or {}
        toc = raw.get("ruleToc") or {}
        content = raw.get("ruleContent") or {}
        chapter_url_rule = str(toc.get("chapterUrl", "") or "").strip()
        content_rule = str(content.get("content", "") or "").strip()
        image_rule = str(content.get("image", "") or "").strip()
        if not chapter_url_rule:
            record.content_ok = None
            record.content_fail_reason = "chapterUrl 规则缺失"
            return
        if "<js" in chapter_url_rule:
            record.content_ok = None
            record.content_fail_reason = "chapterUrl 含 JS 规则，无法用 CSS 验证"
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
            c_status, c_body, c_cost, c_err = await self._request(session, chap_url)
            record.content_response_ms = int(c_cost)
            if c_status is None or c_status >= 400:
                record.content_ok = None
                record.content_fail_reason = f"正文请求失败(status={c_status})"
                return
            ok = False
            if content_rule and "<js" not in content_rule:
                c_html = _decode_body(c_body)
                parts = apply_css_rule(c_html, _strip_rule_prefix(content_rule))
                text = "".join(str(p or "") for p in parts).strip()
                ok = len(text) > 100  # 正文长度阈值：防空壳页/验证码页/错误页
            if not ok and image_rule and "<js" not in image_rule:
                c_html = _decode_body(c_body)
                imgs = apply_css_rule(c_html, _strip_rule_prefix(image_rule))
                imgs = [str(u).strip() for u in imgs if str(u or "").strip()]
                ok = len(imgs) >= 3  # 图片正文判定：漫画图源至少 3 张图
            record.content_ok = ok
            if not ok:
                record.content_fail_reason = (
                    "正文提取为空或长度不足（疑似反爬/需登录/图片源）" if content_rule else "无正文规则且图片不足"
                )
        except Exception as e:
            record.content_ok = None
            record.content_fail_reason = f"验证异常：{type(e).__name__}"

    # ------------------------------------------------------------ 批量校验
    async def run(self, records: List[BookSourceRecord]) -> List[BookSourceRecord]:
        """并发校验所有书源，返回带结果的对象列表。

        若配置了 cache_dir，会读取历史校验结果缓存：
        - 仅规则指纹一致且未过期的缓存才复用，不再发请求
        - 新增、规则变化或缓存过期的源执行校验并追加写入缓存
        """
        # 读取历史缓存，标记已校验的源
        cache = self.load_cache() if self.cache_dir and not self.refresh_cache else {}
        pending: List[BookSourceRecord] = []
        for r in records:
            item = cache.get(r.url)
            if item and is_cache_item_valid(r, item):
                restore_from_cache(r, item)
            else:
                pending.append(r)

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
        if self.cache_dir:
            for r in results:
                self.save_cache_append(r)
        return records  # 已合并缓存结果


def run_check(
    records: List[BookSourceRecord],
    concurrency: int = 50,
    timeout: float = 8.0,
    probe_search: bool = True,
    keyword: str = "我",
    verify_ssl: bool = True,
    cache_dir: Optional[str] = None,
    show_progress: bool = True,
    testset: Optional[Dict[str, List[str]]] = None,
    max_keywords: int = 2,
    proxy: Optional[str] = None,
    probe_depth: int = 1,
    refresh_cache: bool = False,
) -> List[BookSourceRecord]:
    """同步入口：运行校验（Windows 上 asyncio.run 即可）。

    probe_depth: 1=浅探测（静态规则判星级，现状速度）
                 2=命中后验证目录完整度（参考表比例比对）
                 3=再抽样一章验证正文可用性（速度+内容）
    """
    checker = AsyncChecker(
        concurrency=concurrency,
        timeout=timeout,
        probe_search=probe_search,
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

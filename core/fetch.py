# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

import collections
import hashlib
import threading
import time
from typing import Any, Dict, Optional, Tuple

from core.constants import *
from core.quality import rate_interval_ms

# ------------------------------------------------------------------ 限速
# 源可以声明 concurrentRate（就是「这么打我你会被封」）。四条抓取链路里 checker 走
# 异步 aiohttp，其余三条——全链路试跑、连 App 调试补抓页面、快速新增源——都走本模块。
#
# 限速必须两边都管：repair_many 是**自动批量**的（Semaphore(4) + gather，默认 3 轮 ×
# 每轮最多 3 次抓取 = 单源最多 9 次），只给 checker 加限速等于「批量校验守规矩、
# 自动修复连打九下」，而修复的目标恰恰是失效源——里面混着「可修」的站，连打容易
# 把能修的打成真封。
#
# 模块级状态 + 锁：fetch 是同步函数，repair 里会被多个线程同时调用。
_rate_last: Dict[str, float] = {}
_rate_lock = threading.Lock()


def _throttle(key: str, interval_ms: int) -> None:
    """等距上次同 key 请求满 ``interval_ms`` 毫秒再返回。"""
    if not key or interval_ms <= 0:
        return
    with _rate_lock:
        now = time.monotonic() * 1000.0
        last = _rate_last.get(key)
        wait_ms = 0.0 if last is None else max(0.0, interval_ms - (now - last))
        # 先占位再睡：别的线程读到的是「已排到的时刻」，不会有两个请求同时被放出去。
        # 锁也不能握进 sleep —— 那样一个慢源的等待会把所有源一起卡住
        _rate_last[key] = now + wait_ms
    if wait_ms > 0:
        time.sleep(wait_ms / 1000.0)


def _rate_key(source) -> Tuple[str, int]:
    """书源 dict → ``(限速键, 间隔毫秒)``。不传 source / 没声明限速 → ``("", 0)``。

    键用书的**导入原文 URL**：App 的 ``ConcurrentRateLimiter`` 也是按
    ``source.getKey()`` 记的（``ConcurrentRateLimiter.kt:64``）。别用规范化后的
    URL 当键——``strip().rstrip("/").lower()`` 会把两个不同的源合成一个。
    """
    if not isinstance(source, dict):
        return "", 0
    interval = rate_interval_ms(source.get("concurrentRate"))
    if interval <= 0:
        return "", 0
    return str(source.get("bookSourceUrl", "") or "").strip(), interval

def parse_source_header(raw: str) -> tuple:
    """解析 Legado 书源的 ``header`` 字段，返回 ``(请求头, 不可用原因)``。

    Legado 支持两种写法：
      - JSON：``{"User-Agent":"...","Referer":"..."}``
      - 换行分隔：``User-Agent: xxx\\nReferer: yyy``

    含 JS（``<js`` / ``@js:``）时返回空头 + 原因。Legado 在 App 里有 Rhino 引擎
    可以执行（BaseSource.kt:102-124），我们离线做不到，所以只能标注为附注，
    **不因此判源失败**。

    本函数对脏值必须绝对宽容：``header`` 字段来自外部 JSON，任何输入都不得抛异常
    （该字段会被整批书源共用，抛异常会中断整批任务）。
    """
    # 脏值防御：``header`` 来自外部 JSON，可能是 None / 数字 / 列表 / 对象等非字符串。
    # 统一在此拦下，绝不让 .strip() 抛出 AttributeError / TypeError
    # （该字段被整批书源共用，抛异常会中断整批任务）。
    if raw is None:
        return {}, ""
    if not isinstance(raw, str):
        return {}, "header 不是字符串，已忽略"

    # BOM 必须先去：否则 `{"a":"b"}` 的 startswith("{") 为 False，会落到换行
    # 分隔分支被解析成 key='{"a"' / value='"b"}' —— 一个垃圾头真的发给服务器，
    # 比丢掉更糟（会被表现成「源坏了」）。
    # 注意先 strip 再剥 BOM：书源 JSON 里 BOM 前常带空格（如 ``  \ufeff{"a":"b"}``），
    # 此时 BOM 不在首位，lstrip("\ufeff") 会剥不掉，故障静默重现（why 仍为 ""）。
    # BOM 在源码里写成 "\ufeff" 转义而非裸字符：零宽不可见，编辑器清理或复制粘贴
    # 一次就可能变成 lstrip("") 而静默失效。
    text = raw.strip().lstrip("\ufeff").strip()
    if not text:
        return {}, ""
    if "<js" in text or "@js:" in text:
        return {}, "header 含 JS 规则，需要 Legado 引擎，离线无法应用"

    # JSON 写法：看起来像 JSON（无论是否对象）就交给 json.loads 判，
    # 解析失败要给原因，不能静默丢。
    # 注意 `null` / `true` / `false` 是合法 JSON 字面量，但首字符既不特殊也不是
    # 数字——漏掉它们会被当换行写法处理，语义错位（例如 `null` 既非对象也无原因，
    # 变成静默空头）。别删这个子条件。
    if (text[:1] in ("{", "[", '"', "-") or text[:1].isdigit()
            or text in ("null", "true", "false")):
        try:
            obj = json.loads(text)
        except Exception:
            return {}, "header 的 JSON 解析失败"
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items() if v is not None}, ""
        return {}, "header 的 JSON 不是对象"

    # 换行分隔写法
    headers = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            headers[key] = value
    return headers, ""


# ------------------------------------------------------------------ 页面缓存
# 「改一次选择器试一次」的反馈环原来是**重新联网**：实测单页 p50 805ms、一条链
# （搜索/详情/正文）2.4 秒上下，而**解析本身是毫秒级**。缓存只加在本模块唯一的
# 同步抓取出口上，所以 analyzer / App 调试的补抓 / verify_chain / AI 修复的证据 /
# 快速新增源这几条链路**自动**受益，不必逐个改。
#
# **与「结论缓存」是两件事**，名字与量级都必须分得开，否则会让人以为调了它就能
# 少校验：
#   本模块   **页面**：进程内、5 分钟、不落盘，与源是否已保存无关
#   checker  **结论**：落盘、14 / 7 / 1 天，按 fingerprint + probe_depth 比有效性
PAGE_CACHE_TTL = 300                 # 秒
PAGE_CACHE_MAX_PAGES = 200           # 按页数计的 LRU 上限
PAGE_CACHE_MAX_BYTES = 1024 * 1024   # 单页上限；**超出的不缓存**（理由见 _cache_put）

#: 每次抓取用哪种缓存策略（``fetch_ex(cache=...)``）
CACHE_AUTO = "auto"        # 命中就用，缺失/过期就抓（默认）
CACHE_ONLY = "only"        # **完全不发请求**，缺失就抛 CacheMiss
CACHE_REFRESH = "refresh"  # 忽略已有缓存，重抓并回填
CACHE_MODES = (CACHE_AUTO, CACHE_ONLY, CACHE_REFRESH)

#: ``fetch_ex`` 的返回。``cached`` 为真表示这份来自缓存，``fetched_at`` 是它
#: **实际被抓到**的时刻（命中缓存时不是现在）——页面证据要据此标出「你看到的
#: 不是我刚抓的」，这是默认开着缓存时唯一的线索。
Fetched = collections.namedtuple("Fetched", "html cached fetched_at")


class CacheMiss(Exception):
    """只读缓存（``CACHE_ONLY``）下这一页不在缓存里。

    **不是抓取失败**：「我们没去抓」和「抓不到」必须分开呈现，否则用户会把它读成
    站点坏了。调用方要给它自己的话术（见 ``core/verify.py`` / ``core/app_debug.py``）。
    """


_page_cache: "collections.OrderedDict" = collections.OrderedDict()
#: fetch 是同步函数，修复循环里会被多个线程同时调用（同文件头的「限速」一节）
_page_cache_lock = threading.RLock()


def _now_str() -> str:
    """缓存记录用的时间戳。格式与 ``core.store.now()`` 一致（都是给人看的），
    但**不 import store**——fetch 是抓取层，不该为一行时间格式把 SQLite 拖进来。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _charset_key(charset: Any) -> str:
    """charset 进缓存键的形态。它和 header 同源（来自书源 JSON），可能是脏值：
    非字符串一律当「没传」——下面的解码分支本来也只认字符串，两处口径必须一致，
    否则会出现「键相同、行为不同」。"""
    return charset.strip().lower() if isinstance(charset, str) else ""


def _cache_key(url: str, headers: dict, charset: Any, proxy: str) -> str:
    """缓存键 = **请求身份**（URL + 请求头 + charset + 代理）。

    只按 URL 做键会把两种身份的内容串在一起（同一类坑见 lessons §五）：带登录态
    与不带登录态的源、换过 UA 的源，抓回来的不是同一份页面。代理也进键——换了
    出口 IP，同一个 URL 可能给出另一个地区的页面。

    用哈希而不是把身份本身当键：URL 与请求头都可能很长，而键会常驻 200 份。
    ``url`` 传的是**编码后**的那份（见下面的 quote）：`?q=我` 与 `?q=%E6%88%91`
    是同一个请求，不该各占一格。
    """
    ident = "\x00".join([
        url,
        "\x01".join("%s=%s" % (str(k).lower(), v) for k, v in sorted(headers.items())),
        _charset_key(charset),
        str(proxy or "").strip(),
    ])
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()


def _cache_get(key: str):
    with _page_cache_lock:
        item = _page_cache.get(key)
        if item is None:
            return None
        html, stamp, at = item
        if time.time() - at > PAGE_CACHE_TTL:
            del _page_cache[key]      # 过期即删：TTL 一到就必须回源，留着只占内存
            return None
        _page_cache.move_to_end(key)
        return html, stamp


def _cache_put(key: str, html: str, stamp: str) -> None:
    # **超上限的页面不缓存**，而不是截断后缓存：命中缓存必须与重新抓取**等价**。
    # 截断会让解析看到半页 HTML——同一个源两次试跑给出不同判定，而界面上没有任何
    # 东西说明为什么。宁可这一页每次都重抓（代价只是它一直显示「本次新抓」）。
    if len(html) > PAGE_CACHE_MAX_BYTES:
        return
    with _page_cache_lock:
        _page_cache[key] = (html, stamp, time.time())
        _page_cache.move_to_end(key)
        while len(_page_cache) > PAGE_CACHE_MAX_PAGES:
            _page_cache.popitem(last=False)      # LRU：最久没用过的先走


def page_cache_clear() -> None:
    """清空页面缓存。测试要它——模块级状态会跨用例串味（同 ``_rate_last``）。"""
    with _page_cache_lock:
        _page_cache.clear()


def page_cache_size() -> int:
    with _page_cache_lock:
        return len(_page_cache)


def fetch(url: str, timeout: int = 15,
          headers: dict = None, charset: str = "", proxy: str = "",
          source: Optional[Dict[str, Any]] = None) -> str:
    """抓取页面 HTML——``fetch_ex`` 的薄包装，只把 HTML 本身交出去。

    **缓存默认生效**（``CACHE_AUTO``）：同一份请求 5 分钟内不会再联网。需要知道
    「这份是刚抓的还是缓存里的」时用 ``fetch_ex``。
    """
    return fetch_ex(url, timeout, headers, charset, proxy, source).html


def fetch_ex(url: str, timeout: int = 15,
             headers: dict = None, charset: str = "", proxy: str = "",
             source: Optional[Dict[str, Any]] = None,
             cache: str = CACHE_AUTO) -> Fetched:
    """抓取页面，返回 ``Fetched(html, cached, fetched_at)``。

    与 Legado 的 ``AnalyzeUrl`` 对齐的部分：
      - ``headers``：书源自身的 header；缺 User-Agent 时补默认 UA
        （对齐 BaseSource.kt 缺 UA 补 UA 的行为）
      - ``charset``：优先用它解码，失败按常见编码回退
      - ``proxy``：形如 ``http://host:port``；留空走直连。
        **只支持 http 代理**——urllib 的 ProxyHandler 不认 ``socks5://``
        （会抛 ``unknown url type: socks5``）。项目别处（cli/main.py --proxy 帮助、
        WORKFLOW.md、checker.py 注释）宣传的 socks5 同样不成立，是既有的文档失实，
        不属本模块要修的范围，但这里**不要**再写 socks5 以免加深误导。
      - ``source``：完整的书源 dict。传了才会遵守它自己声明的 ``concurrentRate``
        限速（见文件头的「限速」一节）。**调用方应当传**——不传不会报错，只是
        那条链路不受限速约束，而这是静默的。
      - ``cache``：见上面的 ``CACHE_*``。**只缓存抓成功的**——失败页缓存下来
        会让「站点恢复了」看不见（urllib 对 4xx/5xx 直接抛，失败根本走不到写入
        那一步）。也不落盘：重启即失效，调试场景够用。
    """
    if cache not in CACHE_MODES:
        # 不认识的策略要显式报错，别静默退回默认：用户以为在「只读不联网」，
        # 实际每次都在联网，而界面上看不出任何区别
        raise ValueError("未知的缓存策略：%r（只能是 %s）"
                         % (cache, " / ".join(CACHE_MODES)))
    h = {"User-Agent": DEFAULT_UA,
         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        # 值为空（含纯空白）的键不覆盖默认值。用 if v 挡不住 " "，而 urllib 发送前
        # 会 strip，结果服务端收到空 UA —— 与换行写法（已 strip）行为不一致。
        # None 单独挡：str(None) 是 "None"，会被当成真值把默认 UA 覆盖成字面量 "None"
        h.update({str(k): str(v) for k, v in headers.items()
                  if v is not None and str(v).strip()})

    # URL 里可能是**未编码的非 ASCII**（实测：连 App 调试时它给的搜索 URL 就是
    # `...?q=我` 这种原样形态），而 urllib 发送前会按 ascii 编码 → 抛
    # `'ascii' codec can't encode character`。必须在这里补一次编码。
    #
    # safe 保留全部 URL 结构字符**以及 `%`**：这样已经编码好的 `%E6%88%91`
    # 不会被二次编码成 `%25E6...`，对纯 ASCII 的 URL 完全幂等。
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")

    key = _cache_key(url, h, charset, proxy)
    # 缓存查询放在**限速之前**：命中缓存根本没发请求，不该占掉一个限速名额
    # （否则「快了」的收益会被源自己声明的间隔吃掉大半）
    if cache != CACHE_REFRESH:
        hit = _cache_get(key)
        if hit is not None:
            return Fetched(hit[0], True, hit[1])
    if cache == CACHE_ONLY:
        # **绝不偷偷去抓**：置位「只重解析不重抓」的用户以为看到的是缓存里的东西，
        # 一旦这里退回联网，他看到的就是刚抓的——而这正是他要排除的
        raise CacheMiss("本次只读缓存（不联网），这一页没有缓存：%s" % url)

    # 遵守源自己声明的限速。紧挨着真正的网络调用放，别提到函数开头——
    # 那样参数校验失败也会白白占掉一个限速名额
    _throttle(*_rate_key(source))

    req = urllib.request.Request(url, headers=h)

    # 代理：checker 一直支持 proxy，verify 之前不支持——这会让需要代理的源
    # 在试跑里表现为「连接失败」，被用户误判成源坏了
    opener = None
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)

    open_fn = opener.open if opener is not None else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        raw = resp.read()

    # charset 优先，其次按常见编码回退
    order = []
    # charset 与 header 同源（都取自书源 JSON），同样可能是脏值：
    # 直接 .strip() 会让 charset=123 抛 AttributeError。试跑接线后会直接从
    # source.get("charset") 传进来，所以这里必须挡
    if isinstance(charset, str) and charset.strip():
        order.append(charset.strip().lower())
    order += ["utf-8", "gbk", "gb2312", "big5"]
    html = ""
    for enc in order:
        try:
            html = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        html = raw.decode("utf-8", errors="replace")

    stamp = _now_str()
    # 回填缓存（CACHE_REFRESH 也走这里——它正是「重抓并把新的写回去」）。
    # 走到这一行说明请求已经成功：4xx/5xx 由 urllib 抛异常，不会到这里
    _cache_put(key, html, stamp)
    return Fetched(html, False, stamp)
def extract_keyword(url: str) -> str:
    """从搜索 URL 中解出真实关键词（优先 q/keyboard 等常见参数）。"""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("q", "keyboard", "key", "search", "keyword", "wd", "kw", "searchkey", "searchword", "query", "title", "s"):
        if key in query and query[key][0].strip():
            return query[key][0].strip()
    return ""
def make_search_url_template(url: str, keyword: str) -> str:
    """把真实关键词替换为 {{key}} 占位符，得到 searchUrl 模板。

    兼容 URL 中关键词为 URL 编码（%E7%BB%8D%E5%AE%8B）或明文（绍宋）两种形态。
    """
    template = url
    for candidate in (urllib.parse.quote(keyword), urllib.parse.quote(keyword, safe=""), keyword):
        if candidate in template:
            # 全量替换，避免多个相同词残留
            template = template.replace(candidate, "{{key}}")
            break
    # 若 q= 后没替换成功，用正则兜底
    if "{{key}}" not in template:
        template = re.sub(r"([?&](?:q|key|search|keyword|wd|kw|searchkey)=)[^&#]*", r"\1{{key}}", template)
    return template
def _page_has_search_results(html: str, keyword: str) -> bool:
    """判断搜索页是否真的返回了结果：
    1) 页面不含「无结果/表单页」强信号
    2) 详情页链接 + 页面含关键词 → 真结果页（关键词限定可排除「热门推荐/首页」假结果
       —— 无效搜索参数常返回首页/推荐页，靠详情链接信号会误判）
    3) 含精确书名的叶子元素
    """
    if not html or len(html) < 300:
        return False
    # 无结果/表单页强信号：直接判否
    if any(m in html for m in EMPTY_RESULT_MARKERS):
        return False
    # 详情页链接信号（cyppt 类 /novel26888/，koudaimh 类 /manhua/xxx）
    if re.search(r'/[a-z]*(?:novel|book|detail|read|comic|manhua|info|show|chapter)\d*/', html, re.I):
        # 关键词限定：页面必须真的包含搜索词（或其前 2 字符，兼容变体标题）
        if keyword in html:
            return True
        if len(keyword) >= 2 and keyword[:2] in html:
            return True
        return False
    # 关键词锚点（精确书名出现在页面）
    try:
        from core.html import make_soup
        soup = make_soup(html)
        for el in soup.find_all(True):
            txt = el.get_text(strip=True)
            if txt == keyword and not el.find_all(True):
                return True
    except Exception:
        if keyword in html:
            return True
    return False
def probe_search_endpoint(domain: str, keyword: str, timeout: int = 12) -> tuple:
    """
    探测站点常见搜索端点，返回第一个能搜到结果 (url_template, found_url, param_name)。

    返回 (template_url, 实际命中URL, 参数名)；全部失败返回 (None, None, None)。
    """
    from urllib.parse import quote
    base = f"https://{domain}"
    kw_encoded = quote(keyword, safe="")
    attempts = 0
    for tmpl in SEARCH_ENDPOINT_TEMPLATES:
        for param in COMMON_PARAM_NAMES:
            if attempts >= PROBE_MAX_ATTEMPTS:
                return None, None, None
            attempts += 1
            url = base + tmpl.format(p=param, kw=kw_encoded)
            try:
                html = fetch(url, timeout=timeout)
            except Exception:
                continue
            if not _page_has_search_results(html, keyword):
                continue
            template = base + tmpl.format(p=param, kw="{{key}}")
            return template, url, param
    return None, None, None
def _header_headers():
    """返回默认请求头。"""
    return {"User-Agent": DEFAULT_UA}

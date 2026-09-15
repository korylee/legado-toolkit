# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *

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


def fetch(url: str, timeout: int = 15,
          headers: dict = None, charset: str = "", proxy: str = "") -> str:
    """抓取页面 HTML。

    与 Legado 的 ``AnalyzeUrl`` 对齐的部分：
      - ``headers``：书源自身的 header；缺 User-Agent 时补默认 UA
        （对齐 BaseSource.kt 缺 UA 补 UA 的行为）
      - ``charset``：优先用它解码，失败按常见编码回退
      - ``proxy``：形如 ``http://host:port``；留空走直连。
        **只支持 http 代理**——urllib 的 ProxyHandler 不认 ``socks5://``
        （会抛 ``unknown url type: socks5``）。项目别处（cli/main.py --proxy 帮助、
        WORKFLOW.md、checker.py 注释）宣传的 socks5 同样不成立，是既有的文档失实，
        不属本模块要修的范围，但这里**不要**再写 socks5 以免加深误导。
    """
    h = {"User-Agent": DEFAULT_UA,
         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        # 值为空（含纯空白）的键不覆盖默认值。用 if v 挡不住 " "，而 urllib 发送前
        # 会 strip，结果服务端收到空 UA —— 与换行写法（已 strip）行为不一致。
        # None 单独挡：str(None) 是 "None"，会被当成真值把默认 UA 覆盖成字面量 "None"
        h.update({str(k): str(v) for k, v in headers.items()
                  if v is not None and str(v).strip()})

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
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
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

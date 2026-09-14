# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *

def fetch(url: str, timeout: int = 15) -> str:
    """抓取页面 HTML。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gbk", "gb2312", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
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
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
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

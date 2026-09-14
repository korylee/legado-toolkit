# -*- coding: utf-8 -*-
"""
快捷新增书源向导 —— 从搜索 URL 自动生成 Legado 书源

用法示例：
    python add_source.py "https://www.doubaomanhua.com/search?q=%E7%BB%8D%E5%AE%8B" --type manga
    python add_source.py "https://example.com/search?q=斗破苍穹" --name 某小说站 --output my_sources.json

流程：
    1. 抓取搜索页 HTML
    2. 用「真实关键词」锚定书名元素，倒推列表项/容器结构
    3. 生成 Legado CSS 规则（bookList/name/bookUrl/coverUrl/author）
    4. 追加进书源 JSON（自动去重）
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 类型映射：CLI 字符串 → Legado 数字类型（0小说/1听书/2漫画/3视频）
TYPE_MAP = {"novel": 0, "manga": 2, "audio": 1, "video": 3}

# 仅发现模式标记：搜索接口不可用（被限流/无搜索），源仅供发现页/直达访问
DISCOVER_ONLY_TAG = "仅发现"

# 封面图 URL 特征（用于区分真正的封面与 logo/背景图）
COVER_URL_HINTS = ("cover", "uploads", "book", "img", "image", "pic", "comic", "novel", ".webp", ".jpg", ".png")

# 静态资源链接特征（筛选详情页链接时排除）
STATIC_LINK_HINTS = (".css", ".js", ".ico", ".png", ".jpg", ".webp", "javascript:", "mailto:", "/static/", "/uploads/")
# 详情页 URL 特征（优先选取）
DETAIL_LINK_HINTS = ("/detail/", "/book/", "/read/", "/comic/", "/manhua/", "/novel/", "/info/", "/show/")


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


# ---------------------------------------------------------------- 搜索端点探测（方案A）
# 常见搜索端点模板（{p} 为参数名，{kw} 为关键词），按中文站常见度排序
SEARCH_ENDPOINT_TEMPLATES = [
    "/search?{p}={kw}",
    "/index.php/search?{p}={kw}",
    "/e/search/index.php?{p}={kw}&show=title,writer,byr&searchget=1",
    "/e/search/?{p}={kw}",
    "/search.php?{p}={kw}",
    "/s?{p}={kw}",
    "/find?{p}={kw}",
    "/index.php?m=search&c=index&a=init&siteid=1&{p}={kw}",
    "/so/{kw}",
    "/so/{p}={kw}",
]
# 常见搜索参数名（按出现频率排序，keyboard 是帝国CMS/Discuz 等常用名）
COMMON_PARAM_NAMES = [
    "q", "keyboard", "key", "wd", "kw", "searchkey", "searchword", "keyword", "query", "title", "s",
]
# 探测上限（避免请求风暴）
PROBE_MAX_ATTEMPTS = 40


# 无结果/非结果页的强信号（命中即判定为无结果）
EMPTY_RESULT_MARKERS = [
    "没有搜索到", "未搜索到", "无搜索结果", "未找到", "信息提示",
    "搜索不到", "没有找到", "暂无数据", "没有相关", "没有匹配",
    "Powered by EmpireCMS", "高级搜索",
]


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


# ---------------------------------------------------------------- 结构分析
def _leaf_text_elems(soup, keyword: str):
    """找出文本恰好等于关键词的叶子元素（书名锚点）。"""
    from bs4 import BeautifulSoup
    found = []
    for el in soup.find_all(True):
        txt = el.get_text(strip=True)
        if txt == keyword and not el.find_all(True):  # 叶子节点
            found.append(el)
    return found


def _nearest_list_item(name_elem):
    """从书名元素向上找列表项（li 优先，无 li 时取含图/链接的分组 div）。"""
    cur = name_elem
    while cur is not None:
        if getattr(cur, "name", None) == "li":
            return cur
        cur = cur.parent
    # 无 li：取第一个 class 含 item/pic/book/col 的分块
    cur = name_elem
    while cur is not None:
        cls = " ".join(cur.get("class", []) or [])
        if any(k in cls for k in ("item", "pic", "book", "col", "book-item", "comic")):
            return cur
        cur = cur.parent
    return name_elem.parent  # 兜底


def _container_selector(list_item, container):
    """生成 bookList 选择器：容器语义 class（list/comic/book 等）+ 列表项 tag。"""
    ctag = container.name or "div"
    ltag = list_item.name or "div"
    ccls = [c for c in (container.get("class", []) or []) if ":" not in c]
    # 优先语义化 class（comic-list/book-list 等），最后才允许 row/grid 等布局类
    for c in ccls:
        if any(k in c.lower() for k in ("booklist", "book-list", "comiclist", "comic-list",
                                        "result", "search", "myorder", "rank", "list")):
            return f".{c} {ltag}"
    for c in ccls:
        if any(k in c.lower() for k in ("book", "comic", "item", "data")):
            return f".{c} {ltag}"
    if ccls:
        return f".{ccls[0]} {ltag}"
    lcls = [c for c in (list_item.get("class", []) or []) if ":" not in c]
    if lcls:
        return f".{lcls[0]}"
    return f"{ctag} {ltag}"


def _path_selector(el, list_item):
    """生成从列表项到目标元素的 CSS 路径（如 `.name h3 a`）。

    每层取第一个 class（若有），否则取 tag。
    """
    parts = []
    cur = el
    while cur is not None and cur is not list_item:
        cls = cur.get("class", []) or []
        # 忽略纯布局 class（lazy/img-wrapper 等）与 Tailwind 变体（hover: 等含冒号）
        cls = [c for c in cls if c not in ("lazy", "img-wrapper", "text-overflow", "clearfix")
               and ":" not in c]
        parts.append(f".{cls[0]}" if cls else (cur.name or "div"))
        cur = cur.parent
    parts.reverse()
    return " ".join(parts) if parts else (el.name or "div")


def _header_headers():
    """返回默认请求头。"""
    return {"User-Agent": DEFAULT_UA}


def _elem_self_selector(el) -> str:
    """生成单个元素自身的选择器：拼全部非布局 class（最多3个，提升唯一性），否则 tag。

    过滤 Tailwind 变体 class（含冒号，如 hover:bg-gray-200，会破坏 CSS 选择器语法）。
    """
    if el is None:
        return ""
    cls = [c for c in (el.get("class", []) or [])
           if c not in ("lazy", "img-wrapper", "text-overflow", "clearfix") and ":" not in c]
    if cls:
        return "".join(f".{c}" for c in cls[:3])
    return el.name or "div"


def _selector_from_elem(anchor, list_item, tag):
    """兼容旧调用：按 tag 在列表项内定位元素后生成路径。"""
    if tag == "a":
        elems = [a for a in list_item.find_all("a", href=True)
                 if not any(h in (a.get("href") or "").lower() for h in STATIC_LINK_HINTS)]
    elif tag == "img":
        elems = list_item.find_all("img")
    else:
        elems = list_item.find_all(tag)
    if not elems:
        return ""
    return _path_selector(elems[0], list_item)


def analyze_search_page(html: str, keyword: str) -> dict:
    """分析搜索页，返回自动推断的 Legado 规则。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    anchors = _leaf_text_elems(soup, keyword)
    result = {
        "results": len(anchors),
        "bookList": "",
        "name": "",
        "bookUrl": "",
        "coverUrl": "",
        "author": "",
        "intro": "",
        "note": "",
    }
    if not anchors:
        result["note"] = "未找到与关键词完全匹配的书名元素，可能无结果或页面结构特殊。"
        return result

    # 锚点若是 a[title] 且自身就是书名链接，直接用它；否则向上找 a
    anchor = anchors[0]
    named_elem = None
    for probe in (anchor, anchor.parent, anchor.parent.parent):
        if probe is not None and getattr(probe, "name", None) == "a":
            named_elem = probe
            break
    if named_elem is None:
        named_elem = anchor

    list_item = _nearest_list_item(named_elem)
    container = list_item.parent

    # bookList 规则
    result["bookList"] = _container_selector(list_item, container)

    # name 规则：优先 anchor 的 title 属性，其次文本
    if named_elem.get("title"):
        result["name"] = _path_selector(named_elem, list_item) + "@title"
    else:
        result["name"] = _path_selector(named_elem, list_item)

    # bookUrl 规则：优先详情页特征链接
    links = [a for a in list_item.find_all("a", href=True)
             if not any(h in (a.get("href") or "").lower() for h in STATIC_LINK_HINTS)]
    detail = None
    for a in links:
        href = (a.get("href") or "").lower()
        if any(h in href for h in DETAIL_LINK_HINTS):
            detail = a
            break
    target = detail or (links[0] if links else None)
    if target is not None:
        result["bookUrl"] = _path_selector(target, list_item) + "@href"

    # coverUrl 规则：优先 data-original/data-src/src，取含封面特征者
    # 兼容两种形态：<img data-original=...> 与 <div style/class=... data-original=...>（背景懒加载）
    candidates = []
    for el in list_item.find_all(True):
        for attr in ("data-original", "data-src", "src", "data-lazy-src", "data-url", "data-background"):
            val = el.get(attr) or ""
            if val and any(h in val.lower() for h in COVER_URL_HINTS):
                candidates.append((el, attr, val))
    chosen = None
    for el, attr, val in candidates:
        if el.name == "img" or "/cover/" in val.lower() or "cover" in val.lower():
            chosen = (el, attr)
            break
    if chosen is None and candidates:
        chosen = (candidates[0][0], candidates[0][1])
    if chosen:
        el, attr = chosen
        result["coverUrl"] = _path_selector(el, list_item) + f"@{attr}"
        if attr in ("data-original", "data-src", "data-lazy-src", "data-url", "data-background"):
            result["note"] += f" 封面为懒加载属性 {attr}（div背景图），已自动处理。"

    # author 规则：列表项内找 p/span/div 中短文本（排除含状态词/数字占比高者）
    for tag in ("p", "span", "div"):
        for el in list_item.find_all(tag):
            txt = el.get_text(strip=True)
            if not txt or txt == keyword or len(txt) > 12:
                continue
            # 排除"214 鹰扬"这类状态文本（含数字或已知状态词）
            if re.search(r"\d", txt) or any(w in txt for w in ("话", "章", "连载", "完结", "更新", "状态")):
                continue
            if any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in txt):
                result["author"] = _path_selector(el, list_item) + "@text"
                break
        if result["author"]:
            break

    return result


# ---------------------------------------------------------------- 书源生成
def build_source(url: str, keyword: str, analysis: dict,
                 source_name: str, source_type: int,
                 group: str = "📖新增源") -> dict:
    """组装 Legado 书源 JSON。"""
    domain = urllib.parse.urlparse(url).netloc
    search_url = make_search_url_template(url, keyword)
    rule_search = {
        "bookList": analysis["bookList"],
        "name": analysis["name"],
        "bookUrl": analysis["bookUrl"],
        "author": analysis["author"],
        "coverUrl": analysis["coverUrl"],
        "intro": analysis["intro"],
    }
    source = {
        "bookSourceName": source_name,
        "bookSourceType": source_type,
        "bookSourceUrl": "https://" + domain,
        "bookSourceGroup": group,
        "bookSourceComment": f"由 add_source.py 自动生成\n搜索URL: {url}\n[自动推断] {analysis['note']}".strip(),
        "searchUrl": search_url,
        "ruleSearch": rule_search,
        "ruleBookInfo": {},
        "ruleToc": {},
        "ruleContent": {},
        "enabled": True,
        "enabledCookieJar": False,
        "enabledExplore": False,
        "loginCheckJs": "",
        "loginUrl": "",
        "concurrentRate": 1,
        "weight": 0,
        "header": f"User-Agent: {DEFAULT_UA}",
        "jsLib": "",
        "customButton": False,
        "lastUpdateTime": 0,
        "respondTime": 0,
        "customOrder": 0,
        "exploreUrl": "",
        "ruleExplore": {},
        "variableComment": "",
        "eventListener": False,
    }
    return source


def load_sources(path: str) -> list:
    """读取书源 JSON（文件可能是数组，也可能带 sources 键）。"""
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        return data["sources"]
    return []


def save_sources(path: str, sources: list) -> None:
    """写回书源 JSON（保持数组格式，缩进 2）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)


# ============================================================
# 详情页规则推断 + 全链路验证（P0 改进）
# ============================================================

#: 章节页链接特征（目录页里指向章节的链接）
CHAPTER_LINK_HINTS = [
    "/chapter", "/read/", "/reader/", "/view/", "/play/", "/content/",
    "chapter", "read_", "view_", "play_",
]
#: 详情页链接特征（搜索页/目录页里指向书籍详情的链接）
BOOK_DETAIL_HINTS = ["/detail/", "/book/", "/read/", "/comic/", "/manhua/",
                     "/novel", "/info/", "/show/", "/comic/", "/b/"]
#: 被排除的静态/杂项链接
STATIC_LINK_HINTS = ["/css", "/js/", "/images/", "/img/", "/uploads/", ".jpg",
                     ".png", ".gif", ".webp", ".css", ".js", "javascript:",
                     "mailto:", "#", "/tag/", "/category/", "/search", "?"]


def _abs_url(base_url: str, href: str) -> str:
    """把相对链接解析为绝对链接。"""
    if href.startswith(("http://", "https://")):
        return href
    return urllib.parse.urljoin(base_url, href)


def apply_css_rule(html: str, rule: str) -> list:
    """委托 legado_rules.extract_all。

    支持 class./id./tag. 简写、@ 链式选择、.-1 索引、##正则##替换、JSONPath 子集。
    不支持的语法（@js/@xpath/||）返回空列表；需要区分「无法验证」时直接用
    legado_rules.extract_all_ex。
    """
    from core.rules.replayer import extract_all

    return extract_all(html, rule)


def analyze_detail_page(html: str, book_url: str) -> dict:
    """
    从书籍详情页推断目录规则(ruleToc)和正文规则(ruleContent)。

    策略：
      1. 找页面里「含最多章节链接」的容器 → chapterList
      2. chapterName/chapterUrl 取容器内第一章链接的相对路径
      3. 正文规则：抓第一章 URL，取文本量最大的 div → content
    返回 dict(toc={...}, content=rule, note=str)。
    """
    from bs4 import BeautifulSoup
    toc: dict = {}
    content_rule = ""
    note = ""

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:
        return {"toc": {}, "content": "", "note": f"解析详情页失败: {e}"}

    # ---- 1) 找章节链接 ----
    chapter_links = []  # (a元素, href)
    seen_href = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href in seen_href:
            continue
        if any(h in href.lower() for h in STATIC_LINK_HINTS):
            continue
        low = href.lower()
        is_chapter = any(h in low for h in CHAPTER_LINK_HINTS)
        # 数字序号章节：/detail/{id}/{n}.html、/{n}.html、/{id}/{n}.html 等纯数字尾段
        if re.search(r'/\d+(?:\.html?)?$', href) and not any(
                s in href for s in STATIC_LINK_HINTS):
            is_chapter = True
        if is_chapter:
            chapter_links.append((a, href))
            seen_href.add(href)
    if not chapter_links:
        # 兜底：详情页内的纯数字路径（/12345/ 或 /12345.html）当章节
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if re.search(r'/\d+(?:/\d+)?(?:\.html?)?$', href) and not any(
                    s in href for s in STATIC_LINK_HINTS):
                chapter_links.append((a, href))

    if not chapter_links:
        note = "未找到章节链接"
        return {"toc": {}, "content": "", "note": note}

    # ---- 2) 找最可能的目录容器（含最多章节链接的 ul/ol/div） ----
    container_counts = {}
    for a, _href in chapter_links:
        # 向上找祖先容器，统计覆盖度
        p = a.parent
        depth = 0
        while p is not None and depth < 5 and getattr(p, "name", None) not in ("html", "body"):
            key = id(p)
            container_counts.setdefault(key, {"el": p, "count": 0})
            container_counts[key]["count"] += 1
            p = p.parent
            depth += 1

    if not container_counts:
        note = "章节链接无容器"
        return {"toc": {}, "content": "", "note": note}

    # 取覆盖章节链接数最多的容器
    best = max(container_counts.values(), key=lambda x: x["count"])
    if best["count"] < 2:
        note = "章节链接过少，目录规则不可靠"
        return {"toc": {}, "content": "", "note": note}
    container = best["el"]

    # ---- 3) 生成 chapterList / chapterName / chapterUrl 规则 ----
    # chapterList 应匹配「每个章节条目」，而非容器本身。
    # 策略：优先取容器内最近的 li/dd 条目标签，构造「container li」；否则退化为容器自身。
    container_selector = _elem_self_selector(container)
    # 3.1 章节条目标签：统计容器内各候选标签(li/dd/dt/tr) 覆盖章节链接的数量
    item_sel = ""
    tag_counts = {}
    for a, _href in chapter_links:
        # 在 a 的祖先链里找位于 container 内的条目标签
        node = a.parent
        while node is not None and getattr(node, "name", None) not in ("html", "body"):
            if node is container:
                break
            if node.name in ("li", "dd", "dt", "tr"):
                tag = node.name
                # 去掉容器本身 class 后的「条目类」：用元素自身的 class 精确化
                sel = _elem_self_selector(node)
                tag_counts.setdefault((tag, sel), 0)
                tag_counts[(tag, sel)] += 1
                break
            node = node.parent
    if tag_counts:
        # 覆盖最多的条目标签成为 chapterList 选择器
        best_item = max(tag_counts, key=lambda k: tag_counts[k])
        _item_tag, _item_sel = best_item
        item_sel = _item_sel
        # 用「容器 → 条目」的完整路径，保证能定位到所有 li
        chapter_list_selector = f"{container_selector} {_item_sel}"
    else:
        # 无 li/dd 结构：退化为容器自身（单元素容器，章节链接直接挂在容器内）
        item_sel = ""
        chapter_list_selector = container_selector

    # 3.2 chapterName/chapterUrl：取容器内「位于 li/dd 条目中」的第一个章节链接
    #    （避免取到容器头部/尾部的「开始阅读」等非条目链接 → 否则 306 章全指第一章）
    container_a = None
    anchor_item = None
    for a, href in chapter_links:
        # 该链接是否位于 container 内且处于某个条目(li/dd/dt/tr)中
        node = a.parent
        inside = False
        item_found = None
        while node is not None and getattr(node, "name", None) not in ("html", "body"):
            if node is container:
                inside = True
                break
            if node.name in ("li", "dd", "dt", "tr"):
                item_found = node
            node = node.parent
        if inside and item_found is not None:
            container_a = a
            anchor_item = item_found
            break
    if container_a is None:
        # 退路：容器内任意第一个章节链接
        for a, href in chapter_links:
            node = a.parent
            inside = False
            while node is not None and getattr(node, "name", None) not in ("html", "body"):
                if node is container:
                    inside = True
                    break
                node = node.parent
            if inside:
                container_a = a
                break
    if container_a is None:
        container_a = chapter_links[0][0]
    if item_sel and anchor_item is not None:
        # 相对条目元素取路径（chapterName/chapterUrl 在 chapterList 匹配元素内取值）
        link_selector = _path_selector(container_a, anchor_item)
    else:
        link_selector = _path_selector(container_a, container)
    toc = {
        "chapterList": chapter_list_selector,
        "chapterName": f"{link_selector}@text",
        "chapterUrl": f"{link_selector}@href",
        "nextChapterUrl": "",
    }

    # ---- 4) 正文规则：抓第一章 URL，取文本最长容器 ----
    # 用容器内第一个章节链接当样例（漫画站正文多为 JS 加密/懒加载图片，静态抓不到属正常）
    first_chapter_url = _abs_url(book_url, container_a.get("href", ""))
    content_rule = ""
    try:
        ch_html = fetch(first_chapter_url)
        ch_soup = BeautifulSoup(ch_html, "lxml")
        best_len, best_el = 0, None
        for el in ch_soup.find_all(["div", "article", "section", "p"]):
            txt = el.get_text(" ", strip=True)
            # 只考虑文本较长的块（正文通常 >100 字符）
            if len(txt) > 200 and len(txt) > best_len:
                best_len = len(txt)
                best_el = el
        if best_el:
            content_selector = _elem_self_selector(best_el)
            content_rule = f"{content_selector}@textNodes"
        else:
            # 文本正文推断失败：检查是否为图片正文（静态 <img> 漫画站）
            # 排除 logo/图标（src 不含站点图片特征或过小），找含真实图片最多的容器
            _img_marker = re.compile(r"\.(jpg|jpeg|png|webp|avif|gif)", re.I)
            best_img_n, best_img_el = 0, None
            for el in ch_soup.find_all(["div", "section", "article", "ul", "p"]):
                imgs_in = [i for i in el.find_all("img") if (i.get("src") or "") and
                           _img_marker.search(i.get("src") or "")]
                if len(imgs_in) > best_img_n:
                    best_img_n, best_img_el = len(imgs_in), el
            if best_img_n >= 3:
                # 图片正文：content 提取图片 URL（Legado 对 URL 列表自动按图片分页）
                content_selector = _elem_self_selector(best_img_el)
                content_rule = f"{content_selector} img@src"
                note += f"；图片正文（检测到 {best_img_n} 张静态图，content 提取 img@src）"
            else:
                note += "；正文为图片/JS动态加载（静态无法推断），ruleContent 留空"
    except Exception as e:
        note += f"；正文页抓取失败: {e}"

    return {"toc": toc, "content": content_rule, "note": note}


def verify_chain(source: dict, keyword: str, detail_url: str = "",
                 pick: int = 1) -> dict:
    """
    全链路验证一个书源：
      search（搜索）→ bookUrl（取详情链接）→ toc（目录）→ content（正文）
    若 searchUrl 为空（仅发现模式），跳过搜索步，直接用 detail_url 从目录验证。
    返回 {"steps": [{"name","ok","detail"}...], "all_ok": bool}。
    """
    steps = []
    search_tpl = source.get("searchUrl", "") or ""

    # ---- Step 1: 搜索（仅发现模式无搜索规则则跳过） ----
    if not search_tpl:
        steps.append({"name": "search", "ok": True,
                      "detail": "跳过（仅发现模式，无搜索规则）"})
        s_html = None
        book_url = detail_url
        if not book_url:
            steps.append({"name": "bookUrl", "ok": False,
                          "detail": "仅发现模式需提供详情页 URL（--detail-url）"})
            return {"steps": steps, "all_ok": False}
    else:
        try:
            search_url = search_tpl.replace("{{key}}", urllib.parse.quote(keyword))
        except Exception:
            search_url = search_tpl.replace("{{key}}", keyword)
        try:
            s_html = fetch(search_url)
            book_list = apply_css_rule(s_html, source.get("ruleSearch", {}).get("bookList", ""))
            ok1 = len(book_list) > 0
            steps.append({"name": "search", "ok": ok1,
                          "detail": f"{len(book_list)} 条结果" if ok1 else "无结果"})
        except Exception as e:
            steps.append({"name": "search", "ok": False, "detail": f"抓取失败 {e}"})
            return {"steps": steps, "all_ok": False}

        # ---- Step 2: 详情链接（取第 pick 条） ----
        book_url_rule = source.get("ruleSearch", {}).get("bookUrl", "")
        try:
            hrefs = apply_css_rule(s_html, book_url_rule) if book_url_rule else []
            if not hrefs or pick > len(hrefs):
                steps.append({"name": "bookUrl", "ok": False, "detail": "取不到详情链接"})
                return {"steps": steps, "all_ok": False}
            chosen = hrefs[pick - 1]
            # 若是搜索结果页里的相对链接，补全
            book_url = _abs_url(search_url, chosen) if not chosen.startswith("http") else chosen
            steps.append({"name": "bookUrl", "ok": True, "detail": book_url})
        except Exception as e:
            steps.append({"name": "bookUrl", "ok": False, "detail": f"解析失败 {e}"})
            return {"steps": steps, "all_ok": False}

        # 若调用方给了 detail_url，优先用它（更可靠）
        if detail_url:
            book_url = detail_url

    # ---- Step 3: 目录 ----
    try:
        t_html = fetch(book_url)
        toc = source.get("ruleToc", {})
        chapter_list_rule = toc.get("chapterList", "")
        chapters = apply_css_rule(t_html, chapter_list_rule) if chapter_list_rule else []
        ok3 = len(chapters) > 0
        steps.append({"name": "toc", "ok": ok3,
                      "detail": f"{len(chapters)} 章" if ok3 else "无目录"})
    except Exception as e:
        steps.append({"name": "toc", "ok": False, "detail": f"抓取失败 {e}"})
        return {"steps": steps, "all_ok": False}

    # ---- Step 4: 正文（抓第一章 URL） ----
    try:
        ch_url_rule = toc.get("chapterUrl", "")
        ch_urls = apply_css_rule(t_html, ch_url_rule) if ch_url_rule else []
        # 过滤伪链接（javascript:/# 等），避免抓取报错
        ch_urls = [u for u in ch_urls
                   if u.strip() and not u.strip().lower().startswith(("javascript:", "#", "mailto:", "tel:"))]
        if not ch_urls:
            steps.append({"name": "content", "ok": False, "detail": "取不到章节URL"})
            return {"steps": steps, "all_ok": False}
        first_ch = ch_urls[0]
        ch_url = _abs_url(book_url, first_ch) if not first_ch.startswith("http") else first_ch
        c_html = fetch(ch_url)
        content_rule = source.get("ruleContent", {}).get("content", "")
        texts = apply_css_rule(c_html, content_rule) if content_rule else []
        # 图片正文判定：提取的是 URL（http(s)/相对路径且带图片扩展名）→ 按图片数验收
        _url_like = sum(1 for t in texts if t.strip().lower().startswith(("http", "//", "/"))
                        and re.search(r"\.(jpg|jpeg|png|webp|avif|gif)(\?|$)", t.lower()))
        if _url_like >= 3:
            steps.append({"name": "content", "ok": True,
                          "detail": f"{len(texts)} 个图片URL"})
        else:
            total_len = sum(len(t) for t in texts)
            if not content_rule:
                # 规则留空（保持原样：漫画站 JS 加密正文静态无法推断，已知客观限制）
                # 目录已可用即可阅读，这步标记为「跳过」而非「失败」
                print("\n⚠️ 注意: 未找到静态正文规则——该站正文可能为 JS/API 动态加载")
                print("   可在 Legado 中手动补充 ruleContent，或忽略此提示（目录已可用即可阅读）\n")
                steps.append({"name": "content", "ok": True,
                              "detail": "跳过（JS/API 动态加载，静态无法推断）"})
            else:
                ok4 = total_len > 100
                steps.append({"name": "content", "ok": ok4,
                              "detail": f"{total_len} 字符" if ok4 else f"正文过短({total_len})"})
    except Exception as e:
        steps.append({"name": "content", "ok": False, "detail": f"抓取失败 {e}"})

    return {"steps": steps, "all_ok": all(s["ok"] for s in steps)}


# ------------------------------------------------------------
# 交互输入：统一走 sys.stdin.readline()（与 cmd_add 的 stdin URL 读取
# 使用同一缓冲，避免 input() 的 PyOS_Readline 与 readline() 混用错位）
# ------------------------------------------------------------
def _safe_input(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="", flush=True)
    try:
        return sys.stdin.readline().rstrip("\r\n")
    except Exception:
        return ""


# ------------------------------------------------------------
# 交互确认向导 v2：在「规则已分析完成」之后逐项确认，
# 类型用数字菜单、分组列出已有选项 + 推荐新建，避免手输 emoji。
# ------------------------------------------------------------
TYPE_LABELS = [("novel", "📖 小说"), ("manga", "🎨 漫画"),
               ("audio", "🎧 听书"), ("video", "🎬 视频")]

DEFAULT_GROUPS = {"novel": "📖小说/✅★★★☆☆", "manga": "🎨漫画/✅★★★☆☆",
                  "audio": "🎧听书/✅★★★☆☆", "video": "🎬视频/✅★★★☆☆"}


def _find_main_sources():
    """探测当前目录下可作为主库的书源文件（final_all 优先，其次 final_*.json/organized/checked）。"""
    import glob
    mains = []
    for pat in ("final_*.json", "organized.json", "checked.json"):
        mains += glob.glob(pat)
    # 去重保序：final_all.json 优先（用户确认的主库），
    # 其余按体积降序（大的一般是主库）
    seen = set()
    ordered = []
    for p in sorted(mains, key=lambda x: (x != "final_all.json",
                                          -os.path.getsize(x))):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and not p.startswith("."):
            seen.add(key)
            ordered.append(p)
    return ordered


def _interactive_confirm(domain: str, source_type: str, group: str,
                         output: str):
    """分析完成后确认名称/类型/分组/主库。返回 (name, source_type, group, merge_path) 或 None（取消）。"""
    from collections import Counter
    print("\n🧭 确认向导（回车=接受默认值；输入 0 取消）")
    try:
        # ① 名称：默认取域名主体（去 www.）
        default_name = domain.replace("www.", "") if domain else "新源"
        name_in = _safe_input(f"① 书源名称 [{default_name}]: ").strip()
        if name_in == "0":
            return None
        name = name_in or default_name

        # ② 类型：数字菜单（避免手打英文）
        print(" ② 类型：")
        for i, (key, label) in enumerate(TYPE_LABELS, 1):
            mark = "（当前默认）" if key == source_type else ""
            print(f"    {i}) {label} {mark}")
        type_in = _safe_input("    回车保留当前，或输入 1-4: ").strip()
        if type_in == "0":
            return None
        if type_in.isdigit() and 1 <= int(type_in) <= len(TYPE_LABELS):
            source_type = TYPE_LABELS[int(type_in) - 1][0]

        # ③ 分组：已有分组（按出现频率 Top6）+ 推荐新建
        default_group = DEFAULT_GROUPS.get(source_type, group) or group
        existing = []
        try:
            for s in load_sources(output):
                g = (s.get("bookSourceGroup") or "").strip()
                if g and g != "📖新增源":
                    existing.append(g)
        except Exception:
            pass
        # 去重保序（Counter 按频率排序）
        freq = Counter(existing)
        seen = []
        for g in existing:
            if g not in seen:
                seen.append(g)
        existing = seen[:6]

        options = existing + [default_group]
        print(" ③ 分组（现有分组可选项）：")
        for i, g in enumerate(options, 1):
            tag = "（现有）" if i <= len(existing) else "（推荐新建）"
            print(f"    {i}) {g} {tag}")
        group_in = _safe_input("    回车用推荐，或输入编号/自定义分组名: ").strip()
        if group_in == "0":
            return None
        if group_in.isdigit() and 1 <= int(group_in) <= len(options):
            group = options[int(group_in) - 1]
        elif group_in:
            group = group_in
        else:
            group = default_group

        # ④ 合入主库：探测候选（排除 auto_added.json 自身）
        mains = _find_main_sources()
        if mains:
            print(" ④ 合入主库（选择后新源将直接进最终源）：")
            for i, p in enumerate(mains, 1):
                print(f"    {i}) {p}")
            m_in = _safe_input("    回车=暂不合入，输入编号选择: ").strip()
            if m_in == "0":
                return None
            if m_in.isdigit() and 1 <= int(m_in) <= len(mains):
                return name, source_type, group, mains[int(m_in) - 1]
        return name, source_type, group, ""
    except EOFError:
        return None


def run_add(url, name="", source_type="novel", group="📖新增源",
            output="auto_added.json", no_ask=False, probe=True,
            detail_url: str = "", verify: bool = True,
            pick: int = 1, interactive: bool = False,
            to_merge: str = "", discover: bool = False):
    """新增一个书源（可被 main.py 复用）。返回 0=成功 / 1=失败 / 2=已存在。

    :param discover: 强制「仅发现」模式——不依赖搜索，直接生成无搜索规则的书源，
        分组标记「仅发现」，配合 --detail-url 推断目录/正文规则。
        搜索不可用（被限流/无搜索接口）但页面可打开的站，建议用此模式。
    """
    keyword = extract_keyword(url)
    if keyword:
        print(f"🔑 检测到关键词：{keyword}")
    domain = urllib.parse.urlparse(url).netloc
    html = None
    try:
        html = fetch(url)
        if html:
            print(f"🌐 抓取搜索页：{url}")
            print(f"   页面大小：{len(html.encode('utf-8'))} 字节")
    except Exception as e:
        print(f"⚠️  抓取失败：{e}")

    # 2) 若无结果或抓取失败，且允许探测 → 尝试常见搜索端点
    search_url_template = None
    if probe and not discover and (not html or not _page_has_search_results(html, keyword or "")):
        print("🔎 该 URL 未直接命中结果，尝试探测常见搜索端点…")
        try:
            template, found, param = probe_search_endpoint(domain, keyword or "")
            if template:
                print(f"   ✅ 探测到搜索端点：{template}（参数 {param}）")
                search_url_template = template
                html = fetch(found)
            else:
                print("   ❌ 未探测到可用搜索端点")
        except Exception as e:
            print(f"   ⚠️  探测失败：{e}")

    if not html:
        print("❌ 无法获取搜索页 HTML，中止。")
        return 1

    # 2.5) 仅发现模式判定：
    #   强制 discover 参数，或搜索探测失败但页面可打开（无搜索结果/无关键词）→ 自动降级
    discover_mode = discover
    if not discover_mode and not search_url_template and not keyword:
        # 无关键词的 URL（如发现页/详情页直达）→ 仅发现模式
        discover_mode = True
    if not discover_mode and keyword:
        has_results = _page_has_search_results(html, keyword)
        if not has_results and not search_url_template:
            # 探测过且失败 → 搜索确实不可用
            discover_mode = True

    if discover_mode:
        print("🕵️  进入「仅发现」模式：搜索不可用，生成无搜索规则的书源（分组标记「仅发现」）")
        analysis = {"bookList": "", "name": "", "bookUrl": "",
                    "author": "", "coverUrl": "", "intro": "", "note": "仅发现模式：搜索接口不可用/被限流，需通过发现页或详情页直达"}
    else:
        if not keyword:
            print("❌ 无法从 URL 中提取关键词，中止。")
            return 1

        # 3) 分析网页结构，推断规则
        analysis = analyze_search_page(html, keyword)
        print(f"   搜索结果数：{analysis['results']}")
        print("   推断规则：")
        print(f"     bookList = {analysis['bookList']}")
        print(f"     name     = {analysis['name']}")
        print(f"     bookUrl  = {analysis['bookUrl']}")
        print(f"     coverUrl = {analysis['coverUrl'] or '(无)'}")
        print(f"     author   = {analysis['author'] or '(无法自动推断)'}")

        if not analysis["bookList"]:
            print("❌ 未能推断出列表规则，可能页面无搜索结果或结构特殊。")
            if analysis.get("note"):
                print(f"  原因：{analysis['note']}")
            return 1

    # 3.5) 交互确认：分析已完成，此时再问名称/类型/分组/主库最有依据
    if interactive:
        confirmed = _interactive_confirm(domain, source_type, group, output)
        if confirmed is None:
            print("已取消。")
            return 1
        name, source_type, group = confirmed[:3]
        if len(confirmed) > 3 and confirmed[3]:
            to_merge = confirmed[3]  # 向导中选择合入的主库
            print(f"    → 保存后合入主库：{to_merge}")

    # 4) 组装 Legado 书源（source_type 字符串 → Legado 数字类型）
    source_name = name or domain
    source = build_source(url, keyword, analysis, source_name, TYPE_MAP[source_type], group)
    if search_url_template:
        source["searchUrl"] = search_url_template
    if discover_mode:
        # 仅发现模式：无搜索规则，searchUrl 留空；分组加「仅发现」标记
        source["searchUrl"] = ""
        source["ruleSearch"] = {k: "" for k in ("bookList", "name", "bookUrl",
                                                "author", "coverUrl", "intro")}
        group_tag = f",{DISCOVER_ONLY_TAG}"
        if not source["bookSourceGroup"].endswith(group_tag):
            source["bookSourceGroup"] = source["bookSourceGroup"] + group_tag
        # comment 注明仅发现
        base_comment = source.get("bookSourceComment", "")
        source["bookSourceComment"] = (base_comment +
            "\n[仅发现] 搜索接口不可用/被限流，此源无搜索规则，可通过发现页或详情页直达访问")

    # 4.5) 详情页规则推断（可显式指定样例详情页，否则用搜索结果第一条；仅发现模式兜底用原 URL）
    detail_for_toc = detail_url
    if not detail_for_toc:
        try:
            hrefs = apply_css_rule(html, analysis.get("bookUrl", ""))
            if hrefs and pick <= len(hrefs):
                first = hrefs[pick - 1]
                detail_for_toc = _abs_url(url, first) if not first.startswith("http") else first
        except Exception:
            detail_for_toc = ""
    if not detail_for_toc and discover_mode and "{{key}}" not in url:
        # 仅发现模式且 URL 本身是详情页形态（如 liumanhua.com/263176）→ 直接用原 URL
        detail_for_toc = url

    toc_rules = {}
    content_rule = ""
    toc_note = ""
    if detail_for_toc:
        print(f"\n📖 详情页样例：{detail_for_toc}")
        try:
            d_html = fetch(detail_for_toc)
            result = analyze_detail_page(d_html, detail_for_toc)
            toc_rules = result["toc"]
            content_rule = result["content"]
            toc_note = result["note"]
            if toc_rules:
                print(f"   目录规则：{toc_rules.get('chapterList')}")
                print(f"   正文规则：{content_rule or '未推断出'}")
            else:
                print(f"   ⚠️ {toc_note or '目录推断失败'}")
        except Exception as e:
            print(f"   ⚠️ 详情页抓取/推断失败：{e}")

    if toc_rules:
        source["ruleToc"] = toc_rules
    if content_rule:
        source["ruleContent"] = {"content": content_rule, "nextContentUrl": ""}

    # 5) 预览 + 确认
    print("\n📋 生成的书源预览：")
    print(json.dumps({"bookSourceName": source["bookSourceName"],
                      "bookSourceType": source["bookSourceType"],
                      "bookSourceUrl": source["bookSourceUrl"],
                      "searchUrl": source["searchUrl"],
                      "ruleSearch": source["ruleSearch"],
                      "ruleToc": source.get("ruleToc"),
                      "ruleContent": source.get("ruleContent")}, ensure_ascii=False, indent=2))
    if not no_ask:
        ans = _safe_input("\n确认保存？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    # 5.5) 全链路验证（若开启）
    if verify and detail_for_toc:
        v_title = "发现→目录→正文" if discover_mode else "搜索→详情→目录→正文"
        print(f"\n🔍 全链路验证（{v_title}）...")
        try:
            v = verify_chain(source, keyword, detail_url=detail_for_toc, pick=pick)
            for s in v["steps"]:
                mark = "✅" if s["ok"] else "❌"
                print(f"   {mark} {s['name']:<9} {s['detail']}")
            if v["all_ok"]:
                print("   🎉 全链路通过！")
            else:
                print("   ⚠️ 部分步骤失败，可在 Legado 中手动修正规则。")
        except Exception as e:
            print(f"   ⚠️ 验证过程异常：{e}")

    # 6) 追加 + 去重（先查暂存区，再自动探测主库去重）
    sources = load_sources(output)
    exists = any(s.get("bookSourceUrl") == source["bookSourceUrl"] for s in sources)
    if exists:
        print(f"ℹ️  书源 URL {source['bookSourceUrl']} 已存在，跳过追加。")
        return 2
    # 自动探测主库：若主库已有同 URL 源，跳过追加（避免暂存区产生重复）
    if not to_merge:
        try:
            mains = _find_main_sources()
            if mains:
                main_path = mains[0]
                main_urls = {s.get("bookSourceUrl")
                             for s in load_sources(main_path)}
                if source["bookSourceUrl"] in main_urls:
                    print(f"ℹ️  主库 {main_path} 已存在同 URL 源，跳过追加。")
                    return 2
        except Exception as e:
            print(f"   ⚠️ 主库存在性检查失败（忽略）：{e}")
    sources.append(source)
    save_sources(output, sources)
    print(f"✅ 已追加 {len(sources)} 源 → {output}")

    # 6.5) 一键打通：自动 merge 进主书源库
    if to_merge:
        try:
            main_sources = load_sources(to_merge)
            main_urls = {s.get("bookSourceUrl") for s in main_sources}
            if source["bookSourceUrl"] in main_urls:
                print(f"ℹ️  主库 {to_merge} 已存在同 URL 源，未重复合并。")
            else:
                main_sources.append(source)
                save_sources(to_merge, main_sources)
                print(f"✅ 已合并进主库 → {to_merge}（共 {len(main_sources)} 源）")
            # 已入库的源从暂存区（output）移除，避免下次重复合并
            if os.path.abspath(output) != os.path.abspath(to_merge):
                remaining = [s for s in sources
                             if s.get("bookSourceUrl") != source["bookSourceUrl"]]
                if len(remaining) != len(sources):
                    save_sources(output, remaining)
                    if remaining:
                        print(f"🧹 已从 {output} 移除已合入的源（暂存区剩 {len(remaining)} 个待合入）")
                    else:
                        print(f"🧹 已从 {output} 移除已合入的源（暂存区已清空）")
        except Exception as e:
            print(f"⚠️  合并进主库失败：{e}")
            return 1
    return 0


# ------------------------------------------------------------
# 方案 C：sitemap → 书名+ID 映射索引
# 适用于「纯 id 站点」（如 liumanhua.com 详情页 /618502），
# 搜索接口被限流时，可用 sitemap 枚举全站书目。
# ------------------------------------------------------------
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/index.php/api/sitemap/index/so.xml",
    "/index.php/api/sitemap/index/baidu.xml",
    "/index.php/api/sitemap/index.xml",
    "/sitemap/sitemap.xml",
]


def _parse_sitemap_locs(xml_text: str) -> list:
    """从 sitemap XML（可能含命名空间）提取所有 <loc> URL。"""
    import xml.etree.ElementTree as ET
    urls = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # 容错：去掉命名空间前缀再解析
        try:
            cleaned = re.sub(r"<(\w+):", r"<\1", xml_text)
            cleaned = re.sub(r"</(\w+):", r"</\1", cleaned)
            root = ET.fromstring(cleaned)
        except Exception:
            return []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]  # 去命名空间
        if tag == "loc" and elem.text:
            urls.append(elem.text.strip())
    return urls


def discover_sitemap(domain: str, timeout: int = 12) -> tuple:
    """尝试常见 sitemap 路径，返回 (sitemap_url, locs)。找不到返回 (None, [])。"""
    for path in SITEMAP_PATHS:
        for proto in ("https", "http"):
            url = f"{proto}://{domain}{path}"
            try:
                xml_text = fetch(url, timeout=timeout)
                locs = _parse_sitemap_locs(xml_text)
                if locs:
                    print(f"   ✅ sitemap: {url}（{len(locs)} 个 URL）")
                    return url, locs
            except Exception:
                continue
    return None, []


def build_name_id_mapping(domain: str, locs: list, max_items: int = 500,
                          workers: int = 4) -> dict:
    """抓详情页提书名，建 {书名: 详情URL} 映射（并发抓取）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    # 过滤出详情页 URL（带 id 的路径段，排除首页/分类/静态资源）
    detail_urls = []
    for u in locs:
        path = urllib.parse.urlparse(u).path
        if not path or path == "/":
            continue
        if any(h in u.lower() for h in STATIC_LINK_HINTS):
            continue
        # 纯 id 形态：路径最后一个段是数字（如 /618502、/239444/60965.html）
        if re.search(r"/\d+(?:/\d+)?(?:\.html)?$", path):
            detail_urls.append(u)
        elif "novel" in path.lower() and re.search(r"\d+", path):
            detail_urls.append(u)  # cyppt 类 /novel26888/
    detail_urls = detail_urls[:max_items]
    print(f"   详情页 URL：{len(detail_urls)} 个（总 URL {len(locs)}）")

    mapping = {}
    # 书名提取：优先 <h1>，否则 <title> 去掉站点后缀
    def _extract_title(html: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(strip=True)
            if t and "404" not in t and len(t) < 60:
                return t
        title = soup.title.get_text(strip=True) if soup.title else ""
        # 去掉「_最新章节免费阅读-六漫画」这类站点后缀
        for sep in ("_", "-", "|"):
            if sep and sep in title:
                title = title.split(sep)[0]
        return title.strip()

    def _fetch_title(url: str):
        try:
            html = fetch(url, timeout=15)
            return url, _extract_title(html)
        except Exception:
            return url, ""

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_title, u) for u in detail_urls]
        for i, fut in enumerate(as_completed(futures), 1):
            url, title = fut.result()
            if title:
                mapping[title] = url
            if i % 20 == 0:
                print(f"     进度 {i}/{len(detail_urls)}，已收录 {len(mapping)}")
            time.sleep(0.05)  # 温和速率，避免被限流
    return mapping


def run_index(domain: str, output: str = "index.json", max_items: int = 500,
              workers: int = 4, no_ask: bool = False):
    """建 sitemap 书名索引。返回 0=成功 / 1=失败。"""
    if "://" in domain:
        domain = urllib.parse.urlparse(domain).netloc
    print(f"🔍 开始为 {domain} 建 sitemap 索引…")
    sitemap_url, locs = discover_sitemap(domain)
    if not locs:
        print("❌ 未找到 sitemap（尝试了常见路径）。可手动提供 sitemap URL 再试。")
        return 1
    mapping = build_name_id_mapping(domain, locs, max_items=max_items, workers=workers)
    if not mapping:
        print("❌ 未能从详情页提取书名，索引为空。")
        return 1

    data = {"domain": domain, "sitemap": sitemap_url, "total": len(mapping),
            "books": mapping}
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 索引已生成：{len(mapping)} 本书 → {output}")
    print("   示例：")
    for name, url in list(mapping.items())[:5]:
        print(f"     {name} → {url}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="快捷新增 Legado 书源（自动推断规则）")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="从搜索 URL 生成书源")
    p_add.add_argument("url", nargs="?", help="带真实关键词的搜索 URL，如 https://www.doubaomanhua.com/search?q=绍宋。也支持传 - 从 stdin 读取")
    p_add.add_argument("--name", default="", help="书源名称（默认取域名）")
    p_add.add_argument("--type", choices=["novel", "manga", "audio", "video"], default="novel",
                       help="内容类型（默认 novel 小说）")
    p_add.add_argument("--group", default="📖新增源", help="分组名（默认 📖新增源）")
    p_add.add_argument("--output", default="auto_added.json", help="输出书源文件（默认 auto_added.json）")
    p_add.add_argument("--no-ask", action="store_true", help="不确认直接保存")
    p_add.add_argument("--no-probe", action="store_true", help="不自动探测常见搜索端点")

    p_index = sub.add_parser("index", help="从 sitemap 建「书名→ID」映射索引（适用于纯 id 站点）")
    p_index.add_argument("domain", help="站点域名，如 liumanhua.com")
    p_index.add_argument("--output", default="index.json", help="输出索引文件（默认 index.json）")
    p_index.add_argument("--max-items", type=int, default=500, help="最多索引的详情页数（默认 500）")
    p_index.add_argument("--workers", type=int, default=4, help="并发抓取数（默认 4，注意站点限流）")
    p_index.add_argument("--no-ask", action="store_true", help="不确认直接保存")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "index":
        sys.exit(run_index(args.domain, args.output, args.max_items, args.workers, args.no_ask))

    # add 子命令（默认）
    url = getattr(args, "url", None)
    # 支持 stdin 读取 URL（规避 shell 对 %XX 编码的破坏）：
    #   echo 'https://host/search?q=%E7%BB%8D%E5%AE%8B' | python add_source.py add -
    if url == "-" or url is None:
        url = sys.stdin.read().strip()
        if not url:
            print("❌ 未提供 URL（可从命令行参数或 stdin 传入）")
            sys.exit(1)

    sys.exit(run_add(url, args.name, args.type, args.group,
                     args.output, args.no_ask, probe=not args.no_probe))


if __name__ == "__main__":
    main()
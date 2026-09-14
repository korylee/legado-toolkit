# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *
from core.urls import abs_url as _abs_url
from core.fetch import fetch

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

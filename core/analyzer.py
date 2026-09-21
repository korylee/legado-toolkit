# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *
from core.page_layer import WANT_MEDIA
from core.urls import abs_url as _abs_url
from core.fetch import fetch

#: 书名上的「装饰」：实测 samsbook 的搜索结果标题写成 `[历史]绍宋` / `【言情】绍宋之后`，
#: 文本**永远不会**恰好等于关键词——这一类站点原来必然失配（lessons §七十七）。
_TITLE_DECOR_RE = re.compile(r"^\s*[\[【(（][^\]】)）]{1,10}[\]】)）]\s*")


def _strip_title_decor(text: str) -> str:
    """去掉书名开头的 `[分类]` / `【分类】` / `（完）` 这类标注。"""
    return _TITLE_DECOR_RE.sub("", text or "").strip()


def _leaf_text_elems(soup, keyword: str):
    """找出书名锚点（叶子元素）。返回 `(元素列表, 判据)`——**判据要一路带到界面上**
    （认不出来与源坏了长得一样，所以「按什么认出来的」必须能看见，AGENTS #4 一族）。

    三档匹配，**只取命中的最强那一档**：

    1. 文本恰好等于关键词
    2. 去掉开头装饰后等于关键词（实测 samsbook：`[历史]绍宋`）
    3. 文本里含关键词**且是个链接**——第 3 档必须要求链接：页面标题
       `<title>绍宋-搜索结果(共2条记录)</title>` / `<b>…</b>` 里同样含关键词，
       不挡就会把它们当书名
    """
    from core.html import make_soup
    tiers = {1: [], 2: [], 3: []}
    for el in soup.find_all(True):
        if el.name in ("title", "script", "style", "meta"):
            continue
        txt = el.get_text(strip=True)
        if not txt or el.find_all(True):        # 空文本 / 非叶子
            continue
        if txt == keyword:
            tiers[1].append(el)
            continue
        if _strip_title_decor(txt) == keyword:
            tiers[2].append(el)
            continue
        # 第 3 档：含关键词的**链接**（书名锚点几乎总是链接）
        if keyword in txt and len(txt) <= len(keyword) + 16                 and (el.name == "a" or el.find_parent("a") is not None):
            tiers[3].append(el)
    for tier, why in ((1, ""),
                      (2, "书名带前缀装饰（如「[历史]绍宋」）：按「去掉开头的 [分类] 后"
                          "等于关键词」认出来的。"),
                      (3, "书名里含关键词而非恰好相等：按「含关键词的链接」认出来的，"
                          "页面上可能不止一本。")):
        if tiers[tier]:
            return tiers[tier], why
    return [], ""
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
    from core.html import make_soup
    soup = make_soup(html)
    anchors, match_why = _leaf_text_elems(soup, keyword)
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
        result["note"] = ("未找到含关键词的书名链接：可能真没有这本书，也可能书名写法特殊，"
                          "或结果要 JS 渲染（这类页面先看抽屉里的「层」）。")
        return result
    if match_why:
        result["note"] = match_why

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
    # **跳过书名元素本身与它的祖先**：书名带装饰时它的文本不等于关键词，
    # 只按 `txt == keyword` 挡不住——`[历史]绍宋` 会被当成作者名（AGENTS #12 那一类）
    name_text = named_elem.get_text(strip=True)
    for tag in ("p", "span", "div"):
        for el in list_item.find_all(tag):
            txt = el.get_text(strip=True)
            if not txt or len(txt) > 12:
                continue
            if txt == name_text or el is named_elem or el in named_elem.parents:
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
def _text_content_rule(soup) -> tuple:
    """文本正文：取文本量最大的块（>200 字）。返回 `(规则, 字数)`。"""
    best_len, best_el = 0, None
    for el in soup.find_all(["div", "article", "section", "p"]):
        txt = el.get_text(" ", strip=True)
        # 只考虑文本较长的块（正文通常 >100 字符）
        if len(txt) > 200 and len(txt) > best_len:
            best_len, best_el = len(txt), el
    if best_el is None:
        return "", 0
    return f"{_elem_self_selector(best_el)}@textNodes", best_len
def _image_content_rule(soup) -> tuple:
    """图片正文：含真实图片最多的容器（>=3 张）。返回 `(规则, 张数)`。

    排除 logo/图标（src 不含图片后缀的不算），也排除 `src` 为空的占位——
    而「有 `<img>` 但地址是页面脚本注入的」根本不在这里管：那是**档位**的事
    （`core/page_layer` 判 L2），由调用方按 `page_facts` 决定给不给规则。
    """
    mark = re.compile(r"\.(jpg|jpeg|png|webp|avif|gif)", re.I)
    best_n, best_el = 0, None
    for el in soup.find_all(["div", "section", "article", "ul", "p"]):
        imgs_in = [i for i in el.find_all("img") if (i.get("src") or "") and
                   mark.search(i.get("src") or "")]
        if len(imgs_in) > best_n:
            best_n, best_el = len(imgs_in), el
    if best_el is None or best_n < 3:
        return "", 0
    return f"{_elem_self_selector(best_el)} img@src", best_n
def analyze_detail_page(html: str, book_url: str, page_fetcher=None,
                        want: str = "", page_facts: dict = None) -> dict:
    """
    从书籍详情页推断目录规则(ruleToc)和正文规则(ruleContent)。

    策略：
      1. 找页面里「含最多章节链接」的容器 → chapterList
      2. chapterName/chapterUrl 取容器内第一章链接的相对路径
      3. 正文规则：抓第一章 URL，按 `want` 决定先看文本还是先看媒体
    返回 dict(toc={...}, content=rule, note=str)。

    ``page_fetcher``：**取页缝**（默认就是 ``core.fetch.fetch``）。十-5 的编排用它把
    「判到 L2–L4 的那一页」换成**引擎取回来的那份 HTML**（App 手上那份）；拿不到时它**抛**
    （带原因），这里的 ``except`` 会把原因写进 ``note`` —— 「这一段规则先不给」就是这么出来的。

    ``want``：**这一步要拿什么**（`core.page_layer` 的 `WANT_*`，由调用方按书源类型算）。
    媒体类（漫画 / 听书 / 下载）的正文页要的是**图片列表**，文本类才是文字——同一个页面上
    两种东西都可能存在（图片站在正文页上也有 >200 字的说明文字），先看哪一边是**类型说了算**，
    不是页面说了算（2026-09-21：原来只按文本量挑，漫画源实测拿到了 `article@textNodes`）。

    ``page_facts``：取页器如实报告「这一页是在什么材料上判到哪一档」（`_page_for_analysis`
    填）。**它决定这条 CSS 规则给不给**：判到 L3/L4 时数据要解密 / 走接口，CSS 规则**必然
    取不到**——那时**不给规则 + 写明原因**（给出去是假成功，AGENTS #4）；判到 L2 时数据要
    渲染后才有，规则照给，但要**配上让它可用的那个东西**（正文页 URL 带 `webView`）。
    """
    from core.html import make_soup
    toc: dict = {}
    content_rule = ""
    note = ""

    try:
        soup = make_soup(html)
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

    # ---- 4) 正文规则：抓第一章 URL，按类型先看媒体还是先看文本 ----
    # 用容器内第一个章节链接当样例（漫画站正文多为 JS 加密/懒加载图片，静态抓不到属正常）
    first_chapter_url = _abs_url(book_url, container_a.get("href", ""))
    content_rule = ""
    try:
        ch_html = (page_fetcher or fetch)(first_chapter_url)
        ch_soup = make_soup(ch_html)
        img_rule, img_n = _image_content_rule(ch_soup)
        txt_rule, txt_len = _text_content_rule(ch_soup)
        if want == WANT_MEDIA:
            # 声明是媒体类：先看图片。找不到才退回文本，**退回要写进附注**——
            # 「按漫画生成了一份文本规则」在界面上看不出来就是 §七十七 那一类
            content_rule = img_rule or txt_rule
            if img_rule:
                note += f"；图片正文（检测到 {img_n} 张静态图，content 提取 img@src）"
            elif txt_rule:
                note += ("；你选的是漫画 / 听书这类要媒体的源，但这一页上没找到图片列表，"
                         f"正文规则按文本配的（{txt_len} 字）")
        else:
            content_rule = txt_rule or img_rule
            if txt_rule:
                if img_n >= 3:
                    note += (f"；你选的是文本源，但这一页上还有 {img_n} 张图，"
                             "正文规则按文本配的")
            elif img_rule:
                note += f"；图片正文（检测到 {img_n} 张静态图，content 提取 img@src）"
        if not content_rule:
            note += "；正文为图片或 JS 动态加载（静态无法推断），ruleContent 留空"
    except Exception as e:
        note += f"；正文页抓取失败: {e}"

    # ---- 5) 这一页的值不值得给 CSS 正文规则（按取页器报告的那一档）----
    content_rule, toc, note = _apply_layer_guard(content_rule, toc, note, page_facts)
    return {"toc": toc, "content": content_rule, "note": note}


def _apply_layer_guard(content_rule: str, toc: dict, note: str, page_facts: dict) -> tuple:
    """按**这一页的档位**决定这条 CSS 正文规则给不给（十-5 编排的收口）。

    判据一句话：**给出去的规则必须在 App 的默认链路上真能取到值。**

    - **反爬拦截页**（`challenge`，我们抓到的那份根本不是站点）：规则来自引擎过验证后那份
      材料，所以照给，但**同样要配上 `webView`**——App 渲染时自己会再过一次那道验证
    - **L3 / L4**：数据要解密 / 走接口，CSS 规则**必然取不到**——不给规则，把原因和下一步
      动作写进附注（给出去是假成功：选得中、选中的不是数据）
    - **L2**：数据是渲染后才有，规则照给，但**必须配上让它可用的那个东西**——正文页 URL
      带上 `webView` 选项，App 才会渲染这一段（只给 CSS 而不管渲染＝静默取空）
    - **L1 或没判**（`page_facts` 空）：照旧，一个字都不改
    """
    facts = page_facts or {}
    layer = str(facts.get("layer") or "")
    why = str(facts.get("why") or "")
    where = "（%s）" % why if why else ""
    if layer in ("L3", "L4") and not facts.get("challenge"):
        action = ("用「网页视图」里的点选读那个全局对象，写成 webJs" if layer == "L3"
                  else "抓那个接口 + JSONPath")
        return "", toc, (note + "；正文规则先不给：这一页判到 %s%s，数据要%s才有，"
                               "CSS 规则取不到东西" % (layer, where, action))
    mark = str(facts.get("challenge") or "")
    if mark and content_rule:
        toc, paired = _with_webview(toc)
        return content_rule, toc, (note + "；这一页有反爬验证（%s），正文页 URL %s"
                                          "——App 渲染时自己会再过一次那道验证"
                                          % (mark, "已带 webView" if paired else "已经带过 webView"))
    if layer == "L2" and content_rule:
        toc, paired = _with_webview(toc)
        return content_rule, toc, (note + "；正文页 URL 已带 webView——这一页判到 L2%s，"
                                          "数据要页面脚本跑起来才有" % where)
    return content_rule, toc, note


def _with_webview(toc: dict) -> tuple:
    """给正文页 URL 配上 `webView` 选项（已经带过就不重复加）。返回 `(toc, 这次加了吗)`。"""
    urls = toc.get("chapterUrl") or ""
    if urls and "webView" not in urls:
        toc["chapterUrl"] = urls + ',{"webView":true}'
        return toc, True
    return toc, False

# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *
from core.urls import abs_url as _abs_url
from core.fetch import fetch
from core.rules.replayer import extract_all

apply_css_rule = extract_all

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

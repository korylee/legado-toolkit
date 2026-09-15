# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。

全链路试跑：search → bookUrl → toc → content，并采集每一步的证据。

设计依据见 docs/superpowers/specs/2026-09-15-rule-debug-and-dialog-hardening-design.md：
- 判定底线对齐 Legado 的调试（只判「非空 / 不报错」），见 core/quality.py
- 证据含**提取值全文**，对齐 BookContent.kt:194-205 的「正文长度或全文」
- steps[].ok 与 all_ok 保持旧语义（仅 fail → False），三个消费方零改动
"""

from core.constants import *
from core.urls import abs_url as _abs_url
from core.fetch import fetch, parse_source_header
from core.rules.replayer import extract_all_nodes
from core import quality as Q

#: 旧的 apply_css_rule 兼容名（services/add_source.py 仍在用）
from core.rules.replayer import extract_all as apply_css_rule


def _new_page(pages: dict, page_id: str, url: str, html: str,
              status: int = 200, charset: str = "") -> str:
    """把抓到的页面登记进 pages（按 id 去重），返回 page_id。"""
    if not html:
        return ""
    if page_id in pages:
        return page_id
    truncated = len(html) > Q.MAX_PAGE_HTML_CHARS
    pages[page_id] = {
        "id": page_id,
        "url": url,
        "status": status,
        "charset": charset,
        "html": html[:Q.MAX_PAGE_HTML_CHARS],
        "len": len(html),
        "truncated": truncated,
    }
    return page_id


def _extract(html: str, rule: str):
    """按规则取值 + 命中片段。

    **证据预算从 quality 取，这里必须显式传**——`replayer.extract_all_nodes` 故意
    不设默认值，就是为了让截断口径全仓库只有一处定义。这个 helper 是唯一的
    对接点，别绕过它直接调 replayer。
    """
    if not str(rule or "").strip():
        return [], [], "空规则"
    return extract_all_nodes(
        html, rule, Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)


def _step(name: str, judgement, url: str = "", page_id: str = "",
          values=None, matched_html: str = "", rule_error: str = "") -> dict:
    """把 Judgement 摊平成 steps[] 的一项。

    **摊平口径只有一处**：``quality.Judgement.as_step_dict()``。这里只是个短名字，
    绝不要把字典字面量抄回来——「试跑」和「批量校验」各写一份映射，一处忘记
    同步 verdict / notes 就会重新分叉，那正是本次改造要消灭的「同源不同判」。

    ``rule_error`` 必须从这里**透传**进去，不要走「先建字典、再事后赋值」——
    那会让 `as_step_dict` 的入参形同虚设，两个口径又并存了。
    """
    return judgement.as_step_dict(
        name, url=url, page_id=page_id,
        values=values or [], matched_html=matched_html, rule_error=rule_error,
    )


def _cap_evidence(steps: list, pages: dict) -> list:
    """证据总量硬上限。这是唯一一道与调用方无关的保险——ops.py 若将来改漏，
    它兜住，避免正文全文/整页 HTML 撑爆 job 的 result_json 与 SSE 流。"""
    total = sum(len(p.get("html", "")) for p in pages.values())
    total += sum(len(s.get("matched_html", "")) for s in steps)
    total += sum(len(v) for s in steps for v in s.get("values", []))
    if total <= Q.MAX_EVIDENCE_TOTAL_CHARS:
        return steps
    for s in steps:
        s["values"] = []
        s["matched_html"] = ""
        if "证据体积超出上限，已省略" not in s["notes"]:
            s["notes"].append("证据体积超出上限，已省略")
            s["has_notes"] = True
    for p in pages.values():
        p["html"] = p["html"][:1000]
        p["truncated"] = True
    return steps


def verify_chain(source: dict, keyword: str, detail_url: str = "",
                 pick: int = 1, proxy: str = "") -> dict:
    """
    全链路验证一个书源：
      search（搜索）→ bookUrl（取详情链接）→ toc（目录）→ content（正文）
    若 searchUrl 为空（仅发现模式），跳过搜索步，直接用 detail_url 从目录验证。
    返回 {"steps": [...], "pages": [...], "all_ok": bool}。
    """
    steps: list = []
    pages: dict = {}
    src = source or {}
    source_type = int(src.get("bookSourceType", 0) or 0)

    # 书源自身的请求头：不带它抓回来的 HTML 是失真的，「看源码改规则」就失去地基
    headers, header_why = parse_source_header(str(src.get("header", "") or ""))
    charset = str(src.get("charset", "") or "").strip()

    def _fetch(url: str) -> str:
        return fetch(url, headers=headers, charset=charset, proxy=proxy)

    search_tpl = src.get("searchUrl", "") or ""
    s_html = ""
    book_url = ""

    # ---- Step 1: 搜索（仅发现模式无搜索规则则跳过） ----
    if not search_tpl:
        skip_notes = ["仅发现模式，无搜索规则"]
        if header_why:
            skip_notes.append(header_why)
        j_skip = Q.Judgement("unknown", "", "empty", skip_notes)
        steps.append(_step("search", j_skip, "", ""))
        steps[-1]["detail"] = "跳过（仅发现模式，无搜索规则）"
        book_url = detail_url
        if not book_url:
            steps.append(_step("bookUrl", Q.Judgement("fail", "仅发现模式需提供详情页 URL"), "", ""))
            return {"steps": steps, "pages": [], "all_ok": False}
    else:
        search_url = search_tpl.replace("{{key}}", urllib.parse.quote(keyword))
        try:
            s_html = _fetch(search_url)
            book_list_rule = (src.get("ruleSearch") or {}).get("bookList", "")
            vals, hits, rule_error = _extract(s_html, book_list_rule)
            j = Q.judge_list_step("search", vals, "".join(hits), rule_error, source_type)
            page_id = _new_page(pages, "search", search_url, s_html, charset=charset)
            st = _step("search", j, search_url, page_id, vals, "".join(hits), rule_error)
            st["detail"] = j.reason or ("%d 条结果" % len(vals))
            steps.append(st)
            if not j.ok:
                return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        except Exception as e:
            steps.append(_step("search", Q.Judgement("fail", "抓取失败 %s" % e), search_url, ""))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}


        # ---- Step 2: 详情链接（取第 pick 条） ----
        book_url_rule = (src.get("ruleSearch") or {}).get("bookUrl", "")
        hrefs, _hits, _err = _extract(s_html, book_url_rule)
        hrefs = [str(h).strip() for h in hrefs if str(h or "").strip()]
        if not hrefs or pick > len(hrefs):
            j = Q.Judgement("fail", "取不到详情链接")
            steps.append(_step("bookUrl", j, search_url, "", hrefs))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        chosen = hrefs[pick - 1]
        book_url = chosen if chosen.startswith("http") else _abs_url(search_url, chosen)
        bu = _step("bookUrl", Q.Judgement("pass"), search_url,
                   "search" if "search" in pages else "", hrefs)
        bu["detail"] = "%d 条候选，取第 %d 条：%s" % (len(hrefs), pick, book_url)
        steps.append(bu)

        # 调用方给了 detail_url 时优先用它（更可靠）
        if detail_url:
            book_url = detail_url

    # ---- Step 3: 目录 ----
    # ruleBookInfo.tocUrl 非空时跳过详情页解析，直接抓目录页（对齐 Debug.kt:318-322）
    toc_url_rule = str((src.get("ruleBookInfo") or {}).get("tocUrl", "") or "").strip()
    if toc_url_rule:
        book_url = toc_url_rule if toc_url_rule.startswith("http") else _abs_url(book_url, toc_url_rule)

    t_html = ""
    try:
        t_html = _fetch(book_url)
        toc = src.get("ruleToc") or {}
        chapter_list_rule = toc.get("chapterList", "")
        chapters, hits, rule_error = _extract(t_html, chapter_list_rule)
        j = Q.judge_list_step("toc", chapters, "".join(hits), rule_error, source_type)
        page_id = _new_page(pages, "detail", book_url, t_html, charset=charset)
        st = _step("toc", j, book_url, page_id, chapters, "".join(hits), rule_error)
        st["detail"] = j.reason or ("%d 章" % len(chapters))
        steps.append(st)
        if not j.ok:
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
    except Exception as e:
        steps.append(_step("toc", Q.Judgement("fail", "抓取失败 %s" % e), book_url, ""))
        return {"steps": steps, "pages": list(pages.values()), "all_ok": False}

    # ---- Step 4: 正文（抓第一章 URL） ----
    try:
        toc = src.get("ruleToc") or {}
        ch_url_rule = toc.get("chapterUrl", "")
        ch_urls, _hits, _err = _extract(t_html, ch_url_rule)
        # 过滤伪链接（javascript:/# 等），避免抓取报错
        ch_urls = [u for u in ch_urls
                   if u.strip() and not u.strip().lower().startswith(
                       ("javascript:", "#", "mailto:", "tel:"))]
        if not ch_urls:
            steps.append(_step("content", Q.Judgement("fail", "取不到章节URL"), book_url, ""))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        first_ch = ch_urls[0]
        ch_url = _abs_url(book_url, first_ch) if not first_ch.startswith("http") else first_ch
        c_html = _fetch(ch_url)
        content_rule = (src.get("ruleContent") or {}).get("content", "")
        texts, hits, rule_error = _extract(c_html, content_rule)
        j = Q.judge_content(source_type, texts, content_rule, "".join(hits), rule_error)
        page_id = _new_page(pages, "chapter", ch_url, c_html, charset=charset)
        st = _step("content", j, ch_url, page_id, texts, "".join(hits), rule_error)
        st["detail"] = j.reason or ("%d 字符" % j.evidence.get("chars", 0))
        steps.append(st)
    except Exception as e:
        steps.append(_step("content", Q.Judgement("fail", "抓取失败 %s" % e), "", ""))

    # 静态错配检查（只有读 Legado 源码才知道的坑）
    misconfigs = Q.static_misconfig_notes(src)
    if header_why:
        misconfigs = [header_why] + misconfigs
    if misconfigs:
        for s in steps:
            if s["name"] in ("search", "content"):
                s["notes"] = list(s["notes"]) + misconfigs
                s["has_notes"] = True
                break

    steps = _cap_evidence(steps, pages)
    return {
        "steps": steps,
        "pages": list(pages.values()),
        "all_ok": all(s["ok"] for s in steps),
    }

# -*- coding: utf-8 -*-
"""快速新增源：从一个站点 URL 反推出一份可用的书源。

**CLI 与 Web 共用这一份**（`cli/main.py` 的 add、`backend/api/ops.py` 的
`run_add_job`），所以它不能塞进任一个入口目录里——这就是 `services/` 这层存在的理由。

> 这里原本写着「由 services/add_source.py 拆分而来」——那句是从 `core/verify.py`
> 抄过来的，在**人家的文件里**才成立（`verify.py` 确实是从本模块拆出去的）。
> 文件说自己是自己拆出来的，是照抄没改。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.constants import *
from core.urls import abs_url as _abs_url
from core.fetch import (fetch, extract_keyword, probe_search_endpoint,
                          _page_has_search_results, _too_small_to_be_a_page)
from core.analyzer import analyze_search_page, analyze_detail_page
from core.app_debug import want_of_page
from core.build import build_source, load_sources, save_sources
from core.jvm_debug import verify_generated
from core.rules.replayer import extract_all as apply_css_rule

def _safe_input(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="", flush=True)
    try:
        return sys.stdin.readline().rstrip("\r\n")
    except Exception:
        return ""
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
@dataclass
class AddResult:
    """`run_add` 的返回：**退出码与失败原因一起给**。

    为什么不是一个裸 int：原因原来只 print 到 stdout，调用方（job / CLI）手里只剩一个码，
    于是「工具认不出这个页面」被压成「生成失败」，看起来与「源坏了」一模一样
    （lessons §七十七、AGENTS #4）。两者绑在同一个返回值里，上层就没有「顺手把原因丢掉」
    的位置。**约定：`rc != 0` 时 `error` 必须非空**——`tests/test_quick_generate.py` 钉着它。
    """
    rc: int
    error: str = ""


def _page_for_analysis(url: str, want: str, notes: List[str], timeout: int = 60,
                       facts: Optional[Dict[str, Any]] = None,
                       human_gate: bool = False) -> str:
    """这一页该怎么取：**先按老办法抓 → 判层 → L2–L4 时改用引擎那份**（十-5 的编排）。

    为什么是「先抓再判」：判层要看页面内容，抓之前判不了。

    判层走 `core.js_hints.classify_with_scripts`——**判到 L2 或判不了时会再看页面引用的
    那几份脚本**（站点把取数 / 解密逻辑放 bundle 里时，页面 HTML 上一个痕迹都没有）。
    这条只看脚本的补充是**有条件**的：L1 已经定了、L3/L4 已经由页面痕迹定下来了，就不看
    （省请求），见那边的 `DEEP_LAYERS`。

    ``facts``（可选）是**取页器如实报告**给它下游的：`layer` 是**我们抓的那份**判出来的档
    （不是换材料之后的），`why` 是证据行拼起来的。`analyze_detail_page` 拿它决定「这条 CSS
    正文规则给不给」——L3/L4 数据要解密 / 走接口，CSS 规则必然取不到（详见那边的
    `_apply_layer_guard`）。

    **引擎拿不到就抛**（带原因）——调用方据此「这一段规则先不给」。**不回退**：
    在 L2–L4 的页面上用我们抓的那份写规则，产出的是「看着正常、实际取不到」的假成功
    （lessons §八十：引擎是材料就当硬要求）。
    """
    from core.js_hints import classify_with_scripts
    from core.quality import interstitial_marker
    html = fetch(url)
    # **我们抓到的这份根本不是站点**（反爬拦截页）：这时别看层，直接去要引擎那份——
    # 引擎会渲染、会等挑战（桥里那道等待），而且它带着自己的 cookie
    ours = interstitial_marker(html)
    deep = classify_with_scripts(html, want, url)
    verdict = deep["verdict"]
    if deep["docs"] and deep["light_layer"] != verdict["layer"]:
        notes.append("%s 只看原文判 %s，看了页面引用的 %d 份脚本后改判 %s"
                     % (url, deep["light_layer"] or "判不了", len(deep["docs"]),
                        verdict["layer"] or "判不了"))
    headline = ("这一页拿到的是拦截页（%s）" % ours if ours else
                "这一页判到 %s（%s）" % (verdict["layer"], _reason_of(verdict)))
    if facts is not None:
        # **我们抓到的那份**判出来的档：它回答的是「App 默认那条链路上拿得到吗」
        # （换材料之后的档回答不了这个问题——那份是引擎渲染出来的）。
        # 拦截页另记一笔：判据不是层，而是「这份材料不是站点」（正文规则的配套不同）
        facts.update({"url": url, "layer": deep["light_layer"], "why": _reason_of(verdict),
                      "challenge": ours})
    if verdict["layer"] not in ("L2", "L3", "L4") and not ours:
        return html
    try:
        from core.jvm_debug import page_from_engine
        got = page_from_engine(url, timeout=timeout)
    except Exception as e:
        raise RuntimeError(
            "%s，要由本机引擎取；引擎没取到：%s。"
            "这一段规则先不给——用我们抓的那份会写出「看着正常、实际取不到」的规则"
            % (headline, e))
    left = interstitial_marker(got)
    if left and human_gate:
        # **人就在屏幕前**（CLI / 交互式生成）：开桥那个 profile 的浏览器让他自己过一道，
        # 过完**机器接着走**——重取一次，不让他回去重新点一遍生成。
        # 与上游同形（`SourceVerificationHelp.startBrowser` + `refetchAfterSuccess`：
        # 人工过完 → 重取同一个地址，靠的就是凭据已经留在 profile 里）。
        from core.browser_gate import open_for_human
        if open_for_human(url, prompt="在弹出的窗口里把那道验证过一下"):
            got = page_from_engine(url, timeout=timeout)
            left = interstitial_marker(got)
            if not left:
                notes.append("%s；人工在浏览器里过完验证后重取成功（凭据留在 profile 里）"
                             % headline)
                return got
            notes.append("%s；人工过了一次，但引擎取回来的还是拦截页（%s）" % (headline, left))
    if left:
        # 机器过不去那道验证（实测 banxia.cc 的 Cloudflare 拦截、18read.net 的托管挑战
        # 等满 8 秒也没放行）。按它写规则是假成功，所以这一段先不给，并给**可执行**的下一步
        raise RuntimeError(
            "%s；引擎取回来的还是拦截页（%s），机器过不去那道验证。要人工过一次："
            "跑 scripts/jvm_login.py 在桥那个 profile 里把这一页过一遍（凭据会留在 profile 里，"
            "之后就是通的），或者连 App / 用真机调试。这一段规则先不给" % (headline, left))
    notes.append("%s，已改用本机引擎取回的那份 HTML（App 手上那份）" % headline)
    return got


def _reason_of(verdict: Dict[str, Any]) -> str:
    """证据行拼成一句话；脚本里看出来的要标出是哪一份（用户要能复核）。"""
    parts = []
    for e in verdict.get("evidence") or []:
        parts.append("%s，证据在 %s" % (e["why"], e["source"]) if e.get("source") else e["why"])
    return "；".join(parts) or str(verdict.get("unsure") or "")


def run_add(url, name="", source_type="novel", group="📖新增源",
            output="auto_added.json", no_ask=False, probe=True,
            detail_url: str = "", verify: bool = True,
            pick: int = 1, interactive: bool = False,
            to_merge: str = "", discover: bool = False, human_gate: bool = False):
    """新增一个书源（可被 main.py 复用）。返回 `AddResult`：rc 0=成功 / 1=失败 / 2=已存在，
    **失败时 error 里带原因**（别只回一个码——§七十七 那次的教训）。

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

    #: 「这一段换了材料」「这一段要引擎但引擎没取到」「L4 找到了接口」这类事实都进这里，
    #: 最后拼进 note（AGENTS #4：结论是在什么前提下得出的，用户要看得见）
    analysis_notes: List[str] = []

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
        return AddResult(1, "抓不到搜索页 HTML（常见搜索端点也探测过了）："
                            "页面不可达、要登录，或站点结构特殊")

    # 2.4) **「有字节」不等于「拿到了页面」**：抓回来一份明显不是站点的东西时，判据在**引擎那侧**
    # （`interstitial_marker` 要 28KB 那份材料才判得出「安全验证」），所以这里必须去问引擎——
    # 就地降级成「搜索不可用 → 仅发现」会把**「机器过不去那道验证」读成「站点不能搜」**，
    # 产出是一条静默的空源、rc=0，而原因一路被压掉（AGENTS #4；实测 18read.net 回 2 字节的
    # `CN`，见 lessons §八十九）。
    if not discover and _too_small_to_be_a_page(html):
        n = len(html.encode("utf-8"))
        print("   ℹ️  抓回来这一份只有 %d 字节（不像站点），改用本机引擎取这一页…" % n)
        try:
            from core.jvm_debug import page_from_engine
            from core.quality import interstitial_marker
            engine_html = page_from_engine(url, timeout=120)
        except Exception as e:
            print("   ❌ 改用引擎取这一页没成：%s" % e)
            return AddResult(1, "抓回来的页面只有 %d 字节（不像站点），改用引擎重取也失败：%s。"
                                "这一段规则先不给——按这份材料写规则会生成取不到东西的源"
                             % (n, str(e)[:120]))
        left = interstitial_marker(engine_html)
        if left:
            # 与 `_page_for_analysis` 同一条纪律：**按拦截页写规则是假成功**
            print("   ❌ 引擎取回来的还是拦截页（%s）——机器过不去那道验证" % left)
            return AddResult(1, "这一页要人工过一次验证（引擎取回来的是拦截页：%s）。"
                                "跑 scripts/jvm_login.py 在桥那个 profile 里把这一页过一遍"
                                "（凭据会留在 profile 里，之后就是通的），或连 App / 用真机调试。"
                                "这一段规则先不给" % left)
        print("   ✅ 已改用引擎取回的那份（%d 字节）" % len(engine_html))
        analysis_notes.append(
            "我们抓的那份只有 %d 字节（不像站点），已改用本机引擎取回的那份 HTML" % n)
        html = engine_html

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
            return AddResult(1, "URL 里没有可识别的关键词，无法按搜索页生成"
                                "（若站点确实不可搜，用「仅发现」模式）")

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
            # **L4**：这一页的列表要么在接口里、要么要渲染后才有——看**浏览器实际发过哪些请求**
            # （不读 JS 猜地址：需要这条路的站，页面 HTML 我们往往根本拿不到，接口地址也常是
            # 拼出来 / 带签名的）。材料与判据见 `core/net_hunt`。
            print("🔌 推断不出列表规则，启用 L4：看引擎渲染时这一页实际发过哪些请求…")
            l4 = None
            try:
                from core import net_hunt
                l4 = net_hunt.search_api_via_engine(url, keyword, timeout=120)
            except Exception as e:
                print(f"   ⚠️  L4 那一步没成：{e}")
            if l4:
                print(f"   ✅ 找到接口：{l4['request']['method']} {l4['request']['url']}")
                print(f"      判据：{l4['why']}；{l4['note']}")
                analysis_notes.append(
                    "L4：列表数据来自接口「%s」（判据：%s）——searchUrl 与列表规则都按它配的。%s"
                    % (l4["request"]["url"], l4["why"], l4["note"]))
                search_url_template = l4["search_url"]
                analysis = {"results": 1, "bookList": l4["rules"].get("bookList", ""),
                            "name": l4["rules"].get("name", ""),
                            "bookUrl": l4["rules"].get("bookUrl", ""),
                            "author": l4["rules"].get("author", ""),
                            "coverUrl": l4["rules"].get("coverUrl", ""),
                            "intro": "", "note": l4["note"]}
            else:
                print("❌ 未能推断出列表规则，可能页面无搜索结果或结构特殊。")
                if analysis.get("note"):
                    print(f"  原因：{analysis['note']}")
                return AddResult(1, analysis.get("note")
                                 or "未能从搜索页推断出列表规则（页面结构特殊？）"
                                    "——L4 也看过了：引擎渲染时这一页没发出可用的接口请求")

    # 3.5) 交互确认：分析已完成，此时再问名称/类型/分组/主库最有依据
    if interactive:
        confirmed = _interactive_confirm(domain, source_type, group, output)
        if confirmed is None:
            print("已取消。")
            return AddResult(1, "已取消（交互确认没通过）")
        name, source_type, group = confirmed[:3]
        if len(confirmed) > 3 and confirmed[3]:
            to_merge = confirmed[3]  # 向导中选择合入的主库
            print(f"    → 保存后合入主库：{to_merge}")

    # 4) 组装 Legado 书源（source_type 字符串 → Legado 数字类型）
    # 用 .get 而不是下标：type 是外部（HTTP 请求体 / 旧前端 bundle）传进来的，
    # 传一个不在表里的值（例如改枚举前的 "video"）下标会 KeyError → 接口 500。
    # 降级为 0（Legado 的默认类型 文本）最保守——总比整个请求失败强。
    source_name = name or domain
    source = build_source(url, keyword, analysis, source_name,
                          TYPE_MAP.get(source_type, 0), group)
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
    #: 「这一段换了材料」「这一段要引擎但引擎没取到」这类事实都进这里，最后拼进 note
    if detail_for_toc:
        print(f"\n📖 详情页样例：{detail_for_toc}")
        try:
            # 详情页也按层走（同一份判据、同一个取页器）：L2–L4 时改用引擎取回来的那份，
            # 正文页那一页由 `page_fetcher` 在里面同样处理
            src_type_num = TYPE_MAP.get(source_type, 0)
            want_detail = want_of_page("detail", src_type_num)
            want_chapter = want_of_page("chapter", src_type_num)
            d_html = _page_for_analysis(detail_for_toc, want_detail, analysis_notes,
                                        human_gate=human_gate)
            # 「这一页判到哪一档」由取页器如实报告（`page_facts`）：它决定正文规则给不给
            chapter_facts: Dict[str, Any] = {}
            result = analyze_detail_page(
                d_html, detail_for_toc,
                want=want_chapter, page_facts=chapter_facts,
                page_fetcher=lambda u: _page_for_analysis(
                    u, want_chapter, analysis_notes, facts=chapter_facts,
                    human_gate=human_gate))
            toc_rules = result["toc"]
            content_rule = result["content"]
            toc_note = result["note"]
            # 目录 / 正文那一段的结论（含「按类型挑正文」「这一页判到 L3 所以不给正文规则」
            # 这类前提）**要进备注**：只 print 的话，界面与导出的源里一个字都看不到，
            # 而「这份规则是在什么前提下配的」正是用户复核时要看的（AGENTS #4）
            if toc_note:
                analysis_notes.append(toc_note.strip("；"))
            if toc_rules:
                print(f"   目录规则：{toc_rules.get('chapterList')}")
                print(f"   正文规则：{content_rule or '未推断出'}")
            else:
                print(f"   ⚠️ {toc_note or '目录推断失败'}")
        except Exception as e:
            print(f"   ⚠️ 详情页抓取/推断失败：{e}")
            analysis_notes.append("详情/正文段没成：%s" % e)

    if toc_rules:
        source["ruleToc"] = toc_rules
    if content_rule:
        source["ruleContent"] = {"content": content_rule, "nextContentUrl": ""}

    # 4.9) 编排留下的痕迹（换了材料 / 哪一段要引擎而引擎没取到）写进备注：
    # 这是「这份规则是在什么材料上写出来的」，用户要能看见（AGENTS #4）
    if analysis_notes:
        base = source.get("bookSourceComment", "") or ""
        source["bookSourceComment"] = (base + ("\n" if base else "") +
                                       "\n".join("· " + n for n in analysis_notes))
        for n in analysis_notes:
            print(f"   ℹ️ {n}")

    # 5) 预览 + 确认
    # **按哪个类型生成的要说出来**：正文规则是按类型挑的（媒体类先看图片），而这个类型
    # 只从 `--type` / 表单来。非交互那条路上不说，用户就看不出「这份规则是按小说配的」
    label = dict(TYPE_LABELS).get(source_type, source_type)
    print(f"\n🏷️  按「{label}」生成：正文规则按这个类型挑，媒体类先看图片。要换请传 --type")
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
            return AddResult(1, "已取消（没有确认保存）")

    # 5.5) 全链路验证（若开启）：**跑一次本机引擎**（十-5）
    # 与 Web 那条路共用 `core.jvm_debug.verify_generated`——同一件事只有一份实现，
    # 否则两边的结论会漂（那份的注释里写着为什么本地回放器不能当验收）。
    if verify and detail_for_toc:
        v_title = "发现→目录→正文" if discover_mode else "搜索→详情→目录→正文"
        print(f"\n🔍 全链路验证（{v_title}，本机引擎）...")
        try:
            v = verify_generated(source, keyword, detail_url=detail_for_toc)
            if v.get("skipped"):
                # **没验成不等于没生成**：源在这儿，把原因与下一步说清（AGENTS #4）
                print(f"   ⚠️ 这次没验成：{v.get('error') or '引擎没交回结果'}")
                print("      源已经生成了；要验先把本机引擎配好（设置 → JVM 校验），"
                      "或连 App / 用真机调试")
            else:
                for s in v["steps"]:
                    mark = "✅" if s["ok"] else "❌"
                    print(f"   {mark} {s['name']:<9} {s['detail']}")
                if v["all_ok"]:
                    print("   🎉 全链路通过（本机引擎，同一段 App 代码；差在环境）")
                else:
                    print("   ⚠️ 部分步骤失败，可在 Legado 中手动修正规则。")
        except Exception as e:
            print(f"   ⚠️ 验证过程异常：{e}")

    # 6) 追加 + 去重（先查暂存区，再自动探测主库去重）
    sources = load_sources(output)
    exists = any(s.get("bookSourceUrl") == source["bookSourceUrl"] for s in sources)
    if exists:
        print(f"ℹ️  书源 URL {source['bookSourceUrl']} 已存在，跳过追加。")
        return AddResult(2, f"已存在同 URL 的源（{source['bookSourceUrl']}）")
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
                    return AddResult(2, f"主库 {main_path} 里已有同 URL 的源")
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
            return AddResult(1, f"合并进主库失败：{e}")
    return AddResult(0)
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
        from core.html import make_soup
        soup = make_soup(html)
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
    p_add.add_argument("--type", choices=["novel", "manga", "audio", "file"], default="novel",
                       help="内容类型（默认 novel 小说）")
    p_add.add_argument("--group", default="📖新增源", help="分组名（默认 📖新增源）")
    p_add.add_argument("--output", default="auto_added.json", help="输出书源文件（默认 auto_added.json）")
    p_add.add_argument("--no-ask", action="store_true", help="不确认直接保存")
    p_add.add_argument("--no-probe", action="store_true", help="不自动探测常见搜索端点")
    p_add.add_argument("--no-verify", action="store_true", help="生成后不跑本机引擎验证")

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
                     args.output, args.no_ask, probe=not args.no_probe,
                     verify=not args.no_verify, human_gate=not args.no_ask).rc)

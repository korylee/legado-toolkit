# -*- coding: utf-8 -*-
"""书源类型重判定（P0-1）与失效归因（P0-2）。

解决两个问题：
  1. 漫画源被识别成小说源：add_source 的 --type 默认 novel，bookSourceType 从未实测。
     本模块用「静态信号 + 首页/正文实测」反推真实类型。
  2. 失效源归因过粗：checker 只判「域名通不通 + 搜索有没有命中」，
     死站 / 规则漂移 / 站点转型 全挤在 Health.DEAD 一个桶里。
     本模块对失效源重新探测，区分三者并给出可操作结论。

用法：
    python reclassify.py -i candidates.json --report   # 只看类型判定，不改文件
    python reclassify.py -i candidates.json --write    # 回写 bookSourceType / 分组
    python diagnose.py  -i out/checked.json -o out/diagnose.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from core.models import BOOK_SOURCE_TYPE_NAMES, Health
from core.tags import extract_user_tags_from_group

# ------------------------------------------------------------------ 类型信号表

#: 域名里出现这些片段 -> 偏漫画
MANGA_HOST_HINTS = (
    "manhua", "manga", "comic", "dm5", "acg", "kokomh", "hanman", "dongman",
    "guoman", "copymanga", "baozimh", "colamanga", "zaimanhua", "godamh",
    "wuxiamh", "mhl", "mh123", "mh160", "mhxq", "mhua", "manhuagui", "18mh",
)

#: 域名里出现这些片段 -> 偏小说
NOVEL_HOST_HINTS = (
    "xs", "book", "shu", "wenxue", "read", "txt", "biqu", "23us", "69shu",
    "zongheng", "qidian", "xbiquge", "bqg", "qu-la", "novel",
)

MANGA_TEXT_HINTS = ("漫画", "漫畫", "汉化", "漢化", "manga", "comic", "acg",
                    "动漫", "動漫", "图集", "圖集", "里番", "同人")

NOVEL_TEXT_HINTS = ("小说", "小說", "书屋", "書屋", "文学", "文學", "书城",
                    "書城", "阅读", "閱讀", "书院", "書院", "txt", "网文")


def _host(url: str) -> str:
    m = re.search(r"//([^/]+)", url or "")
    return (m.group(1) if m else url or "").lower()


def _text_of(source: Dict[str, Any]) -> str:
    """参与判定的文本：名称 + 注释 + group 里的**用户标签**。

    group 必须只取用户标签。系统标签（`📖小说` / `🎨漫画` / `待验证` …）是
    `organizer.group_title` 按 `bookSourceType` 生成的，而 `bookSourceType` 正是
    本模块要判的东西——读整个 group 会形成循环：

        类型错(默认 0) → group 写成「📖小说」 → 命中 "小说" → novel +3
                      → 单独就到阈值 3 → 再判成小说   ↺

    错的标签就此固化。实测：命中 "小说" 的 3540 条源里 3506 条的 group 带系统
    类型标签，剥掉后 119 条改判（32 条从「小说」纠正为「漫画」）。

    用户标签要留着——那是人主动打的真信号，`extract_user_tags_from_group`
    负责把两者分开（`漫画` 二字不算系统标签，`🎨漫画` 才算）。
    """
    parts = [
        str(source.get("bookSourceName", "") or ""),
        " ".join(extract_user_tags_from_group(source.get("bookSourceGroup", ""))),
        str(source.get("bookSourceComment", "") or ""),
    ]
    return " ".join(parts)


def infer_type_static(source: Dict[str, Any]) -> Tuple[int, int, int, List[str]]:
    """纯静态信号判定类型。

    返回 (候选类型, 漫画分, 小说分, 理由列表)；候选类型为 -1 表示证据不足、保持原样。
    """
    from core.rules.replayer import looks_like_image_rule

    host = _host(str(source.get("bookSourceUrl", "") or ""))
    text = _text_of(source)
    low_text = text.lower()
    content = source.get("ruleContent") or {}
    toc = source.get("ruleToc") or {}
    content_rule = str(content.get("content", "") or "")
    chapter_rule = str(toc.get("chapterUrl", "") or "")

    manga = 0
    novel = 0
    why: List[str] = []

    hits = [h for h in MANGA_HOST_HINTS if h in host]
    if hits:
        manga += 3
        why.append("域名含漫画特征 " + ",".join(hits[:3]))
    if any(h in low_text for h in MANGA_TEXT_HINTS):
        manga += 3
        why.append("名称/分组含漫画特征")
    # 这里原本还有一条 `ruleContent.image` 非空 → 漫画 +3 的信号，2026-09-16 删除。
    # 那个字段 Legado 的 ContentRule 里没有，本项目也从不写：实测库里的 3861 条源，
    # 非空的是 0 条。所以它从来没加上过分，是死代码——**别再按 image 字段加信号**，
    # 要判图片源就看 content 规则本身（下一行的 looks_like_image_rule）。
    if content_rule and looks_like_image_rule(content_rule):
        manga += 2
        why.append("正文规则取的是图片")
    if str(content.get("imageStyle", "") or "").upper() == "FULL":
        manga += 1
    if re.search(r"(manhua|comic|/mh/|/dm/|manga)", chapter_rule, re.I):
        manga += 1

    nhits = [h for h in NOVEL_HOST_HINTS if h in host]
    if nhits:
        novel += 2
        why.append("域名含小说特征 " + ",".join(nhits[:3]))
    if any(h in low_text for h in NOVEL_TEXT_HINTS):
        novel += 3
        why.append("名称/分组含小说特征")
    if content_rule and not looks_like_image_rule(content_rule):
        novel += 2
        why.append("正文规则取的是文本")

    declared = int(source.get("bookSourceType", 0) or 0)
    if declared == 2:
        manga += 1
    elif declared == 0:
        novel += 1

    if manga >= 3 and manga > novel:
        return 2, manga, novel, why
    if novel >= 3 and novel > manga:
        return 0, manga, novel, why
    return -1, manga, novel, why



# ------------------------------------------------------------------ 联网探测

HOMEPAGE_MANGA_PATH = re.compile(r"/(manhua|comic|manga|dm|mh)/", re.I)
HOMEPAGE_NOVEL_PATH = re.compile(r"/(novel|book|read|chapter|xiaoshuo)/", re.I)


async def _get(session, url, timeout=8.0, method="GET", headers=None, body=""):
    """返回 (status, text, err)。err 见 checker.classify_transport_error。"""
    import aiohttp

    h = headers or {}
    try:
        async with session.request(method, url, headers=h,
                                   timeout=aiohttp.ClientTimeout(total=timeout),
                                   data=body if body else None) as resp:
            body = await resp.read()
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = str(body)
            return resp.status, text, ""
    except asyncio.TimeoutError:
        return None, "", "timeout"
    except aiohttp.ClientConnectorDNSError:
        return None, "", "dns"
    except aiohttp.ClientConnectorCertificateError:
        # **证书错误与 TLS 握手错误是两个类，而且不是父子**（都继承
        # `ClientSSLError`）：所以这一支必须单独列，否则它会落到下面的
        # `except Exception` 变成 "other"，归因给出「可能是临时故障」——
        # 而同一个源在校验链路里已经被判成「🔐证书（关掉校验就能用）」。
        # 两侧码表不一致正是 lessons 里「同一件事两个入口、结论相反」那一类。
        return None, "", "cert"
    except aiohttp.ClientConnectorSSLError:
        return None, "", "tls"
    except aiohttp.ClientConnectionResetError:
        return None, "", "reset"
    except aiohttp.ServerDisconnectedError:
        return None, "", "reset"
    except Exception:
        return None, "", "other"


async def _transport_attribution(err: str, host: str) -> Tuple[str, str]:
    """传输层失败 → ``(归因桶, 说明)``。

    **口径必须与校验链路一致**（`core/checker` + `core/dns_check`）：这是同一件事的
    第二个入口，判成相反结论比"没有归因"更糟——用户是照着这份报告决定删源的。

    改之前这里对**任何**传输失败都写「死站：…」，配的建议动作还是"两次明确失败后
    淘汰"。而 DNS 失败里的大头是**本机解析被污染/被墙**（实测抽样 6 个域名里 5 个
    是这一档），那些源开代理就能用——照报告清理就会把它们删掉。
    """
    from core import dns_check
    from core.checker import err_desc      # 描述文案的唯一实现，别在这儿再抄一份
    if err in ("reset", "tls"):
        # 连接重置 / TLS 握手失败：校验链路判「需翻墙」（被墙典型特征）
        return "需翻墙", "需翻墙：%s" % err_desc(err)
    if err == "cert":
        return "其他", "证书不被信任（关掉证书校验或用 http 复检）"
    if err != "dns":
        # 超时、其他网络错误：校验链路判「待复检」（一次失败不定性），
        # 这里也不给淘汰建议——归到「其他」，由人再看
        return "其他", "无法归因：%s（可能是临时故障）" % err_desc(err)
    verdict, note = await dns_check.probe(host)
    if verdict == dns_check.POLLUTED:
        return "需翻墙", "需翻墙：本机解析不到，但%s" % note
    if verdict == dns_check.GONE:
        return "死站", "死站：域名已注销（%s）" % note
    return "其他", "DNS 解析失败且%s，不下结论" % note


def homepage_signals(html: str) -> Tuple[int, int, List[str]]:
    """从首页 HTML 提取「偏漫画/偏小说」信号。"""

    from bs4 import BeautifulSoup

    manga = 0
    novel = 0
    why: List[str] = []
    low = (html or "").lower()
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return 0, 0, why
    title = ""
    if soup.title and soup.title.string:
        title = str(soup.title.string)
    metas = []
    for tag in soup.find_all("meta"):
        name = str(tag.get("name", "") or "").lower()
        if name in ("keywords", "description", "og:title"):
            metas.append(str(tag.get("content", "") or ""))
    head_text = (title + " " + " ".join(metas)).lower()
    if any(h in head_text for h in MANGA_TEXT_HINTS):
        manga += 3
        why.append("首页标题/关键词含漫画特征")
    if any(h in head_text for h in NOVEL_TEXT_HINTS):
        novel += 3
        why.append("首页标题/关键词含小说特征")
    hrefs = [str(a.get("href", "") or "") for a in soup.find_all("a", href=True)][:400]
    m_paths = sum(1 for h in hrefs if HOMEPAGE_MANGA_PATH.search(h))
    n_paths = sum(1 for h in hrefs if HOMEPAGE_NOVEL_PATH.search(h))
    if m_paths >= 5:
        manga += 2
        why.append("首页链接多为漫画路径 (%d)" % m_paths)
    if n_paths >= 5:
        novel += 2
        why.append("首页链接多为小说路径 (%d)" % n_paths)
    return manga, novel, why


def combine_type(source: Dict[str, Any], home: Optional[Tuple[int, int, List[str]]] = None):
    """静态 + 首页信号合并判定。返回 (类型, 理由)。类型 -1 = 保持原样。"""
    t, ms, ns, why = infer_type_static(source)
    if home:
        ms += home[0]
        ns += home[1]
        why = why + list(home[2])
    if ms >= 3 and ms > ns:
        return 2, why
    if ns >= 3 and ns > ms:
        return 0, why
    return -1, why



# ------------------------------------------------------------------ 失效归因（P0-2）

#: 归因结论 -> 建议动作
ACTION_OF = {
    "死站": "两次明确失败后淘汰",
    "需翻墙": "保留，开代理（或换 DNS）复检",
    "需验证": "保留，人工或改 UA/Cookie 复检",
    "规则漂移": "保留，进 AI 修复队列（域名活着，规则过期）",
    "站点转型": "改 bookSourceType 或按新类型重建规则",
    "疑似可用": "复检一次；可能是缓存误判或临时故障",
}


async def diagnose_source(session, source, timeout=8.0, keywords=None):
    # 对单个书源做归因探测：域名可达性 -> 反爬特征 -> 类型信号 -> 搜索页可解析性
    from core.checker import (parse_search_request, build_domain_url,
                              classify_http_status, DEFAULT_UA)
    from core.rules.replayer import parse_list

    keywords = keywords or ["海贼王", "斗破苍穹"]
    url = str(source.get("bookSourceUrl", "") or "")
    res = {"url": url,
           "name": str(source.get("bookSourceName", "") or ""),
           "declared": int(source.get("bookSourceType", 0) or 0),
           "reachable": False, "transport": "", "http_status": 0,
           "type_guess": -1, "type_why": [], "search_state": "",
           "book_list_count": None, "attribution": "", "bucket": ""}
    if not url:
        res["attribution"] = "无 bookSourceUrl"
        res["bucket"] = "其他"
        return res

    domain = build_domain_url(url)
    status, text, err = await _get(session, domain, timeout,
                                   headers={"User-Agent": DEFAULT_UA})
    res["http_status"] = status or 0
    res["transport"] = err
    if status is None:
        # 传输层失败**不能一律归成「死站」**：DNS 失败里的大头是解析被污染/被墙，
        # 那些源开代理就能用。判定口径与校验链路共用（见 _transport_attribution）
        host = domain.split("//", 1)[-1].split("/")[0]
        res["bucket"], res["attribution"] = await _transport_attribution(err, host)
        return res
    res["reachable"] = True

    # 判定表只有一份（`classify_http_status`）——原来这里自己写了一遍，
    # 与域名探测在 503 / 404 / 500 / 登录页四类输入上分叉过。
    h = classify_http_status(status, text, bool(source.get("enabledCookieJar")))
    if h == Health.AUTH:
        res["attribution"] = "需验证：反爬/登录墙 (status=%s)" % status
        res["bucket"] = "需验证"
        return res
    if h == Health.DEAD:
        res["attribution"] = "死站：HTTP %s" % status
        res["bucket"] = "死站"
        return res

    guess, why = combine_type(source, homepage_signals(text))
    res["type_guess"] = guess
    res["type_why"] = why

    search_url = str(source.get("searchUrl", "") or "")
    book_list_rule = str((source.get("ruleSearch") or {}).get("bookList", "") or "")
    if not search_url:
        res["search_state"] = "无搜索规则"
        res["attribution"] = "站点可达但无搜索规则（仅发现源）"
        res["bucket"] = "规则漂移"
        return res

    hit_any = False
    parsed_any = False
    last_status = 0
    for kw in keywords[:2]:
        try:
            surl, method, headers, s_body = parse_search_request(search_url, kw)
        except Exception:
            continue
        if surl.startswith("/"):
            surl = domain + surl
        elif not surl.startswith(("http://", "https://")):
            surl = domain + "/" + surl
        headers = dict(headers or {})
        headers.setdefault("User-Agent", DEFAULT_UA)
        s_status, s_text, s_err = await _get(session, surl, timeout, method=method,
                                             headers=headers, body=s_body)
        last_status = s_status or 0
        if s_status is None:
            continue
        if s_status in (401, 403, 429):
            res["attribution"] = "需验证：搜索接口被拦截 (status=%s)" % s_status
            res["bucket"] = "需验证"
            return res
        if s_status >= 400:
            continue
        if kw in (s_text or ""):
            hit_any = True
        if book_list_rule:
            nodes, perr = parse_list(s_text, book_list_rule)
            if nodes:
                parsed_any = True
                res["book_list_count"] = len(nodes)
                break
        else:
            parsed_any = hit_any
            break

    if parsed_any:
        if guess >= 0 and guess != res["declared"]:
            res["attribution"] = "站点转型：实测类型 %s != 声明 %s" % (
                BOOK_SOURCE_TYPE_NAMES.get(guess, guess),
                BOOK_SOURCE_TYPE_NAMES.get(res["declared"], res["declared"]))
            res["bucket"] = "站点转型"
        else:
            res["search_state"] = "搜索可解析"
            res["attribution"] = "疑似可用：搜索能解析出列表（原判失效可能过期/误判）"
            res["bucket"] = "疑似可用"
        return res

    if hit_any and not book_list_rule:
        res["search_state"] = "有响应但无 bookList 规则"
    else:
        res["search_state"] = "搜索无结果或列表解析为空 (status=%s)" % last_status
    res["attribution"] = "规则漂移：域名活着(%s)但搜索规则跑不出结果" % status
    res["bucket"] = "规则漂移"
    return res



# ------------------------------------------------------------------ CLI

def load_sources(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_sources(path: str, data: Any) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_reclassify(args) -> int:
    data = load_sources(args.input)
    if not isinstance(data, list):
        print("输入必须是书源数组（JSON array）")
        return 2
    changed = []
    for s in data:
        if not isinstance(s, dict):
            continue
        declared = int(s.get("bookSourceType", 0) or 0)
        guess, why = combine_type(s)
        if guess >= 0 and guess != declared:
            changed.append((s, declared, guess, why))
    print("总源数: %d，类型判定与声明不一致: %d" % (len(data), len(changed)))
    for s, d, g, why in changed[:args.limit]:
        print("  %-28s %s -> %s | %s" % (
            str(s.get("bookSourceName", ""))[:28],
            BOOK_SOURCE_TYPE_NAMES.get(d, d),
            BOOK_SOURCE_TYPE_NAMES.get(g, g),
            "; ".join(why[:2])))
    if args.write and changed:
        for s, d, g, why in changed:
            s["bookSourceType"] = g
        out = args.output or args.input
        save_sources(out, data)
        print("已回写 %d 条到 %s（重跑 organize 可刷新分组）" % (len(changed), out))
    return 0


async def _diagnose_all(sources, args):
    import aiohttp
    sem = asyncio.Semaphore(args.concurrency)
    # force_close：归因同样是**一场多主机扫描**，空闲连接池会随进度涨到几千个
    # （aiohttp 的 limit 只管在飞的连接），理由与实测见 core/checker.py 的同一处
    connector = aiohttp.TCPConnector(limit=args.concurrency, limit_per_host=5,
                                     force_close=True)

    async def one(session, s):
        async with sem:
            try:
                return await diagnose_source(session, s, args.timeout, keywords=args.keywords)
            except Exception as e:
                return {"url": s.get("bookSourceUrl", ""), "name": s.get("bookSourceName", ""),
                        "bucket": "其他", "attribution": "探测异常: %s" % type(e).__name__,
                        "declared": s.get("bookSourceType", 0), "type_guess": -1}

    async with aiohttp.ClientSession(connector=connector) as session:
        return await asyncio.gather(*[one(session, s) for s in sources])


def cmd_diagnose(args) -> int:
    data = load_sources(args.input)
    if not isinstance(data, list):
        print("输入必须是书源数组（JSON array）")
        return 2
    if args.only_dead:
        # **筛的是「最近一次校验结果」，不是 build_record 的默认值。**
        # 原来这里写的是 `build_record(s, 0).health != Health.OK`，而 build_record
        # **根本不读校验结果**（health 恒为默认的 SKIPPED）——条件永远为真，这个开关
        # 从没筛掉过任何东西：`--only-dead` 照样对全部 3861 条发请求，而用户以为只测
        # 失效的。名不副实、且不报错。
        #
        # 读的是**与 check 命令同一个后端**（默认管理库；NDJSON 目录要
        # `--cache-dir` + `LEGADO_LEGACY_CACHE=1`）。后端由 AsyncChecker 自己判，
        # 这里**不能** import cli/main 的 `_resolve_check_cache`——core 层反向依赖
        # CLI 是 lessons §十 记过的坑
        from core.checker import AsyncChecker
        from core.models import Health
        from core.loader import _normalize_url
        probe = AsyncChecker(cache_dir=getattr(args, "cache_dir", "") or None)
        try:
            checks = probe.load_cache()
        finally:
            probe.close()
        if not checks:
            # 不静默：空缓存时这个开关等于没开，用户至少要知道
            print("警告: 读不到校验结果，--only-dead 本次不筛"
                  "（默认读管理库；结果在 NDJSON 目录就加 --cache-dir 并设 "
                  "LEGADO_LEGACY_CACHE=1）")
        before = len(data)
        data = [s for s in data
                if str((checks.get(_normalize_url(str(s.get("bookSourceUrl", "") or "")))
                        or {}).get("health", "")) != Health.OK]
        print("--only-dead：%d → %d 条（按最近一次校验筛掉「可用」的源）" % (before, len(data)))
    print("待归因源: %d" % len(data))
    results = asyncio.run(_diagnose_all(data, args))
    buckets = Counter(r.get("bucket", "其他") for r in results)
    lines = ["# 失效归因报告", "", "共 %d 个源。" % len(results), "",
             "| 归因 | 数量 | 建议动作 |", "|---|---:|---|"]
    for b, n in buckets.most_common():
        lines.append("| %s | %d | %s |" % (b, n, ACTION_OF.get(b, "")))
    lines += ["", "## 明细", "", "| 归因 | 源名 | 域名 | 声明类型 | 实测类型 | 说明 |",
              "|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r.get("bucket", ""), str(r.get("name", ""))[:24], _host(str(r.get("url", "")))[:32],
            BOOK_SOURCE_TYPE_NAMES.get(r.get("declared", 0), r.get("declared", 0)),
            BOOK_SOURCE_TYPE_NAMES.get(r.get("type_guess", -1), "未判定"),
            str(r.get("attribution", ""))[:60]))
    text = "\n".join(lines)
    if args.output:
        save_text(args.output, text)
        print("报告已写入 %s" % args.output)
    print(text[:1200])
    return 0


def save_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reclassify", description="书源类型重判定 + 失效归因")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("reclassify", help="按实测信号重判书源类型（漫画/小说）")
    r.add_argument("-i", "--input", required=True, help="书源 JSON 数组")
    r.add_argument("-o", "--output", help="输出文件（默认覆盖输入）")
    r.add_argument("--write", action="store_true", help="回写 bookSourceType")
    r.add_argument("--limit", type=int, default=30, help="控制台明细条数")
    r.set_defaults(func=cmd_reclassify)

    d = sub.add_parser("diagnose", help="对失效源重新探测并归因（死站/规则漂移/站点转型/需验证）")
    d.add_argument("-i", "--input", required=True, help="书源 JSON 数组")
    d.add_argument("-o", "--output", help="Markdown 报告输出路径")
    d.add_argument("--only-dead", action="store_true", help="只探测非 OK 的源")
    d.add_argument("-c", "--concurrency", type=int, default=20)
    d.add_argument("-t", "--timeout", type=float, default=8.0)
    d.add_argument("--keywords", nargs="*", default=None, help="搜索探测关键词")
    d.set_defaults(func=cmd_diagnose)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

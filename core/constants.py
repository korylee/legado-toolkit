# -*- coding: utf-8 -*-
"""从 add_source.py 抽出的模块级常量。"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request


DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TYPE_MAP = {"novel": 0, "manga": 2, "audio": 1, "video": 3}
DISCOVER_ONLY_TAG = "仅发现"
COVER_URL_HINTS = ("cover", "uploads", "book", "img", "image", "pic", "comic", "novel", ".webp", ".jpg", ".png")
STATIC_LINK_HINTS = (".css", ".js", ".ico", ".png", ".jpg", ".webp", "javascript:", "mailto:", "/static/", "/uploads/")
DETAIL_LINK_HINTS = ("/detail/", "/book/", "/read/", "/comic/", "/manhua/", "/novel/", "/info/", "/show/")
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
COMMON_PARAM_NAMES = [
    "q", "keyboard", "key", "wd", "kw", "searchkey", "searchword", "keyword", "query", "title", "s",
]
PROBE_MAX_ATTEMPTS = 40
EMPTY_RESULT_MARKERS = [
    "没有搜索到", "未搜索到", "无搜索结果", "未找到", "信息提示",
    "搜索不到", "没有找到", "暂无数据", "没有相关", "没有匹配",
    "Powered by EmpireCMS", "高级搜索",
]
CHAPTER_LINK_HINTS = [
    "/chapter", "/read/", "/reader/", "/view/", "/play/", "/content/",
    "chapter", "read_", "view_", "play_",
]
BOOK_DETAIL_HINTS = ["/detail/", "/book/", "/read/", "/comic/", "/manhua/",
                     "/novel", "/info/", "/show/", "/comic/", "/b/"]
STATIC_LINK_HINTS = ["/css", "/js/", "/images/", "/img/", "/uploads/", ".jpg",
                     ".png", ".gif", ".webp", ".css", ".js", "javascript:",
                     "mailto:", "#", "/tag/", "/category/", "/search", "?"]
TYPE_LABELS = [("novel", "📖 小说"), ("manga", "🎨 漫画"),
               ("audio", "🎧 听书"), ("video", "🎬 视频")]
DEFAULT_GROUPS = {"novel": "📖小说/✅★★★☆☆", "manga": "🎨漫画/✅★★★☆☆",
                  "audio": "🎧听书/✅★★★☆☆", "video": "🎬视频/✅★★★☆☆"}
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/index.php/api/sitemap/index/so.xml",
    "/index.php/api/sitemap/index/baidu.xml",
    "/index.php/api/sitemap/index.xml",
    "/sitemap/sitemap.xml",
]

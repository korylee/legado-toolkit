# -*- coding: utf-8 -*-
"""URL 工具。从 add_source 抽出，避免 core 反向依赖 services。"""

from urllib.parse import urljoin


def abs_url(base_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base_url or "", href)

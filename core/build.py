# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。"""

from core.constants import *
from core.fetch import make_search_url_template

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

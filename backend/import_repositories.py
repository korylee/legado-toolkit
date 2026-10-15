"""Static declarative adapters for remote source repositories.

Repository definitions are trusted application configuration, never user input. They
contain fixed HTTPS endpoints and data-only JSON path maps; no expression is executed.
"""

import json
import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from core.paths import data_path


def _valid_browse_config(browse: Any) -> bool:
    if not isinstance(browse, dict):
        return False
    listing = browse.get("list")
    pagination = browse.get("pagination")
    return (
        isinstance(listing, dict)
        and isinstance(listing.get("path"), str)
        and listing["path"].startswith("/")
        and isinstance(pagination, dict)
        and isinstance(pagination.get("page_param"), str)
        and bool(pagination["page_param"])
        and type(pagination.get("page_size")) is int
        and pagination["page_size"] > 0
    )


def _html_field(root: Any, spec: Dict[str, Any]) -> Any:
    nodes = root.select(spec["selector"])
    values = []
    for node in nodes:
        value = node.get(spec["attribute"]) if spec.get("attribute") else node.get_text(" ", strip=True)
        value = str(value or "").strip()
        pattern = spec.get("pattern")
        if pattern:
            match = re.search(pattern, value)
            value = match.group(1).strip() if match else ""
        if value:
            values.append(value)
        if not spec.get("multiple"):
            break
    return " · ".join(values) if spec.get("multiple") else (values[0] if values else "")


def _normalize_html_index(document: str, html_spec: Dict[str, Any], page: int,
                          page_size: int) -> Dict[str, Any]:
    soup = BeautifulSoup(document, "html.parser")
    items = []
    for card in soup.select(html_spec["item_selector"]):
        item = {name: _html_field(card, field)
                for name, field in html_spec["fields"].items()}
        if not str(item.get("id") or "").strip():
            raise ValueError("HTML 仓库列表条目缺少有效标识")
        if not str(item.get("name") or "").strip():
            raise ValueError("HTML 仓库列表条目缺少名称")
        item["id"] = str(item["id"])
        item["name"] = str(item["name"])
        if "total" in item and item["total"]:
            item["total"] = int(item["total"])
        items.append(item)
    count_spec = html_spec["pagination"]
    count_node = soup.select_one(count_spec["total_selector"])
    count_text = count_node.get_text(" ", strip=True) if count_node else ""
    match = re.search(count_spec["total_pattern"], count_text)
    if not match:
        raise ValueError("HTML 仓库响应缺少总数")
    return {"items": items,
            "pagination": {"page": page, "pageSize": page_size,
                          "total": int(match.group(1))}}


def _load_repositories() -> Dict[str, Dict[str, Any]]:
    config_path = data_path("config", "import_repositories.json")
    try:
        with open(config_path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法加载书源仓库配置：%s" % config_path) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("repositories"), list):
        raise RuntimeError("书源仓库配置必须包含 repositories 数组")

    repositories: Dict[str, Dict[str, Any]] = {}
    for config in payload["repositories"]:
        if not isinstance(config, dict):
            raise RuntimeError("书源仓库配置项必须是对象")
        repository_id = config.get("id")
        if not isinstance(repository_id, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9-]{0,63}", repository_id):
            raise RuntimeError("书源仓库 id 格式无效")
        if repository_id in repositories:
            raise RuntimeError("书源仓库 id 重复：%s" % repository_id)
        try:
            base = urlsplit(config.get("base_url", ""))
            port = base.port
        except (TypeError, ValueError) as exc:
            raise RuntimeError("书源仓库 base_url 无效：%s" % repository_id) from exc
        if (base.scheme != "https" or not base.hostname or port not in (None, 443)
                or base.username or base.password or base.path or base.query or base.fragment):
            raise RuntimeError("书源仓库 base_url 必须是 HTTPS 源站：%s" % repository_id)
        views = config.get("views")
        if not isinstance(views, list) or not views:
            raise RuntimeError("书源仓库必须配置至少一个 view：%s" % repository_id)
        view_types = set()
        for view in views:
            if (not isinstance(view, dict)
                    or view.get("type") not in ("browse", "collections")
                    or not isinstance(view.get("label"), str)
                    or not view["label"] or view["type"] in view_types):
                raise RuntimeError("书源仓库 view 配置无效：%s" % repository_id)
            view_types.add(view["type"])
            if view["type"] == "browse":
                browse = config.get("browse")
                details = config.get("details")
                if (not _valid_browse_config(browse)
                        or not isinstance(details, dict)
                        or not isinstance(details.get("path_template"), str)
                        or "{id}" not in details["path_template"]):
                    raise RuntimeError("browse view 核心配置无效：%s" % repository_id)
            if view["type"] == "collections":
                listing = config.get("collection_list")
                details = config.get("collection_details")
                if (not isinstance(listing, dict)
                        or not isinstance(listing.get("path_template"), str)
                        or not listing["path_template"].startswith("/")
                        or not isinstance(details, dict)
                        or (not details.get("url_field")
                            and (not isinstance(details.get("path_template"), str)
                                 or "{collection_id}" not in details["path_template"]))):
                    raise RuntimeError("书源合集操作核心配置无效：%s" % repository_id)
                if (listing.get("format") == "html"
                        and (type(listing.get("page_size")) is not int
                             or listing["page_size"] < 1)):
                    raise RuntimeError("书源合集 HTML page_size 无效：%s" % repository_id)
        if not isinstance(config.get("display"), dict):
            raise RuntimeError("书源仓库 display 配置无效：%s" % repository_id)
        repositories[repository_id] = config
    return repositories


_REPOSITORIES = _load_repositories()


def _get_path(value: Any, path: str) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _list_payload(value: Any, configured_path: str) -> Any:
    """读取列表字段；配置路径优先，兼容常见 data/items 信封。"""
    result = _get_path(value, configured_path)
    if isinstance(result, list):
        return result
    if isinstance(value, dict):
        for key in ("items", "data", "results", "list"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
            if isinstance(candidate, dict):
                nested = candidate.get("items") or candidate.get("results") or candidate.get("list")
                if isinstance(nested, list):
                    return nested
    return result


def get_repository(repository_id: str) -> Optional[Dict[str, Any]]:
    return _REPOSITORIES.get(str(repository_id or ""))


def supports_view(config: Dict[str, Any], view_type: str) -> bool:
    return any(view.get("type") == view_type for view in config.get("views", []))


def list_repositories() -> list[dict]:
    """Return configured browse views and display maps, not transport internals."""
    out = []
    for config in _REPOSITORIES.values():
        browse = config.get("browse") or {}
        out.append({
            "id": config["id"],
            "name": config["name"],
            "views": [{**view, "id": view["type"]}
                      for view in config.get("views", [])],
            "capabilities": {
                "list": isinstance(browse.get("list"), dict),
                "search": bool((browse.get("search") or {}).get("query_param")),
            },
            "display": dict(config["display"]),
        })
    return out


def _normalize_listing(spec: Dict[str, Any], payload: Any) -> Dict[str, Any]:
    raw_items = _list_payload(payload, spec["response_items"])
    if not isinstance(raw_items, list):
        raise ValueError("仓库响应缺少列表")
    fields = spec["fields"]
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = {name: _get_path(raw, path) for name, path in fields.items()}
        if item.get("id") is None or not str(item.get("id")).strip():
            continue
        item["id"] = str(item["id"])
        if "name" in item:
            item["name"] = str(item.get("name") or "未命名书源")
        if "url" in item:
            item["url"] = str(item.get("url") or "")
        items.append(item)
    page = _get_path(payload, spec.get("response_page", "")) or 1
    page_size = (_get_path(payload, spec.get("response_page_size", ""))
                 or spec.get("page_size", 20))
    total = _get_path(payload, spec["response_total"])
    try:
        page = max(int(page), 1)
        page_size = max(int(page_size), 1)
        total = max(int(total), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("仓库响应分页字段无效") from exc
    return {"items": items,
            "pagination": {"page": page, "pageSize": page_size, "total": total}}


def _browse_spec(config: Dict[str, Any], query: str) -> Dict[str, Any]:
    browse = config["browse"]
    spec = {**browse["pagination"], **(browse.get("response") or {}), **browse["list"]}
    query = str(query or "").strip()
    if not query:
        return spec
    search = config["browse"].get("search")
    if not isinstance(search, dict) or not search.get("query_param"):
        raise ValueError("该书源仓库不支持搜索")
    fixed_params = dict(spec.get("fixed_params", {}))
    fixed_params.update(search.get("fixed_params", {}))
    spec.update({key: value for key, value in search.items() if key != "fixed_params"})
    spec["fixed_params"] = fixed_params
    spec["fixed_params"][search["query_param"]] = query
    return spec


def browse_format(config: Dict[str, Any], query: str) -> str:
    return _browse_spec(config, query).get("format", "json")


def browse_url(config: Dict[str, Any], query: str, page: int) -> str:
    spec = _browse_spec(config, query)
    params = dict(spec.get("fixed_params", {}))
    params[spec["page_param"]] = str(page)
    if spec.get("page_size_param") and spec.get("format", "json") != "html":
        params[spec["page_size_param"]] = str(spec["page_size"])
    return config["base_url"] + spec["path"] + "?" + urlencode(params)


def normalize_browse_result(config: Dict[str, Any], payload: Any,
                            query: str, page: int = 1) -> Dict[str, Any]:
    spec = _browse_spec(config, query)
    if spec.get("format", "json") == "html":
        return _normalize_html_index(
            payload, spec["html"], page, spec["page_size"])
    return _normalize_listing(spec, payload)


def _operation_url(config: Dict[str, Any], spec: Dict[str, Any], *,
                   path_values: Optional[Dict[str, str]] = None,
                   params: Optional[Dict[str, Any]] = None) -> str:
    path = spec["path_template"].format(**(path_values or {}))
    query = dict(spec.get("fixed_params", {}))
    query.update({key: str(value) for key, value in (params or {}).items()})
    suffix = "?" + urlencode(query) if query else ""
    return config["base_url"].rstrip("/") + path + suffix


def collection_list_url(config: Dict[str, Any], page: int = 1) -> str:
    spec = config["collection_list"]
    params = {}
    if spec.get("page_param"):
        params[spec["page_param"]] = page
    if spec.get("page_size_param"):
        params[spec["page_size_param"]] = spec["page_size"]
    return _operation_url(config, spec, params=params)


def normalize_collections(config: Dict[str, Any], payload: Any,
                          page: int = 1) -> Dict[str, Any]:
    spec = config["collection_list"]
    if spec.get("format") == "html":
        return _normalize_html_index(payload, spec["html"], page, spec["page_size"])
    raw_items = _get_path(payload, spec["response_items"])
    if not isinstance(raw_items, list):
        raise ValueError("书源集响应缺少列表")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = {name: _get_path(raw, path) for name, path in spec["fields"].items()}
        if item.get("id") is None or not str(item.get("id")).strip():
            continue
        item["id"] = str(item["id"])
        item["name"] = str(item.get("name") or "未命名书源集")
        items.append(item)
    page_size = int(spec.get("page_size") or max(len(items), 1))
    return {"items": items,
            "pagination": {"page": page, "pageSize": page_size, "total": len(items)}}


def collection_details_url(config: Dict[str, Any], collection_id: str,
                           collection_url: str = "") -> str:
    spec = config["collection_details"]
    if spec.get("url_field"):
        if not collection_url:
            raise ValueError("书源合集缺少详情链接")
        return str(collection_url).strip()
    collection_id = str(collection_id or "").strip()
    if not collection_id or len(collection_id) > 128 or any(
            not (char.isalnum() or char in "-_.") for char in collection_id):
        raise ValueError("书源集标识无效")
    return _operation_url(
        config, spec,
        path_values={"collection_id": quote(collection_id, safe="")})


def normalize_collection_details(config: Dict[str, Any], payload: Any) -> list[dict]:
    items = _list_payload(payload, config["collection_details"].get("response_items", ""))
    if not isinstance(items, list):
        raise ValueError("书源集详情响应不是书源数组")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("书源集详情包含非书源对象")
    if not items:
        raise ValueError("书源集详情没有书源")
    return items


def extract_json_link(document: str, page_url: str, selector: str) -> str:
    soup = BeautifulSoup(document, "html.parser")
    for anchor in soup.select(selector):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        parsed = urlsplit(href)
        if parsed.scheme.lower() == "yuedu":
            source = parse_qs(parsed.query).get("src", [""])[0]
            if source:
                return urljoin(page_url, source)
        elif ".json" in parsed.path.lower():
            return urljoin(page_url, href)
    raise ValueError("仓库详情页没有找到 JSON 下载链接")


def detail_url(config: Dict[str, Any], item_id: str) -> str:
    item_id = str(item_id or "").strip()
    if (not item_id or len(item_id) > 128 or item_id in (".", "..")
            or any(not (char.isalnum() or char in "-_.") for char in item_id)):
        raise ValueError("仓库条目标识无效")
    return config["base_url"] + config["details"]["path_template"].format(
        id=quote(item_id, safe=""))


def normalize_details(config: Dict[str, Any], payload: Any) -> list[dict]:
    items = _list_payload(payload, config["details"]["response_items"])
    if not isinstance(items, list):
        raise ValueError("仓库详情响应不是书源数组")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("仓库详情数组包含非书源对象")
    return items



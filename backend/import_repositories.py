"""Static declarative adapters for remote source repositories.

Repository definitions are trusted application configuration, never user input. They
contain fixed HTTPS endpoints and data-only JSON path maps; no expression is executed.
"""

import json
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode, urlsplit

from core.paths import data_path


def _valid_browse_config(browse: Any) -> bool:
    if not isinstance(browse, dict) or set(browse) - {"list", "search", "pagination", "response"}:
        return False
    listing = browse.get("list")
    pagination = browse.get("pagination")
    response = browse.get("response")
    if (not isinstance(listing, dict)
            or not isinstance(listing.get("path"), str)
            or not listing["path"].startswith("/")
            or ("fixed_params" in listing and not isinstance(listing["fixed_params"], dict))
            or not isinstance(pagination, dict)
            or not all(isinstance(pagination.get(key), str) and pagination[key]
                       for key in ("page_param", "page_size_param", "response_page",
                                   "response_page_size", "response_total"))
            or type(pagination.get("page_size")) is not int
            or pagination["page_size"] < 1
            or not isinstance(response, dict)
            or not isinstance(response.get("response_items"), str)
            or not isinstance(response.get("fields"), dict)
            or "id" not in response["fields"]
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in response["fields"].items())):
        return False
    search = browse.get("search")
    if search is None:
        return True
    return (
        isinstance(search, dict)
        and set(search) <= {"query_param", "path", "fixed_params"}
        and isinstance(search.get("query_param"), str)
        and bool(search["query_param"])
        and ("path" not in search or (
            isinstance(search["path"], str) and search["path"].startswith("/")
        ))
        and ("fixed_params" not in search or isinstance(search["fixed_params"], dict))
    )


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
        view_ids = set()
        for view in views:
            if (not isinstance(view, dict)
                    or view.get("type") not in ("browse", "collections")
                    or not isinstance(view.get("id"), str)
                    or not isinstance(view.get("label"), str)
                    or not view["id"] or not view["label"] or view["id"] in view_ids):
                raise RuntimeError("书源仓库 view 配置无效：%s" % repository_id)
            view_ids.add(view["id"])
            if view["type"] == "browse":
                browse = config.get("browse")
                details = config.get("details")
                if (not _valid_browse_config(browse)
                        or not isinstance(details, dict)
                        or not isinstance(details.get("path_template"), str)
                        or "{id}" not in details["path_template"]
                        or not isinstance(details.get("response_items"), str)):
                    raise RuntimeError("browse view 配置不完整：%s" % repository_id)
            if view["type"] == "collections" and any(
                    key not in config for key in ("collection_list", "collection_details")):
                raise RuntimeError("书源集 view 缺少列表/详情配置：%s" % repository_id)
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
            "views": [dict(view) for view in config.get("views", [])],
            "capabilities": {
                "list": isinstance(browse.get("list"), dict),
                "search": bool((browse.get("search") or {}).get("query_param")),
            },
            "display": dict(config["display"]),
        })
    return out


def _normalize_listing(spec: Dict[str, Any], payload: Any) -> Dict[str, Any]:
    raw_items = _get_path(payload, spec["response_items"])
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
    spec = {**browse["pagination"], **browse["response"], **browse["list"]}
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


def browse_url(config: Dict[str, Any], query: str, page: int) -> str:
    spec = _browse_spec(config, query)
    params = dict(spec.get("fixed_params", {}))
    params[spec["page_param"]] = str(page)
    params[spec["page_size_param"]] = str(spec["page_size"])
    return config["base_url"] + spec["path"] + "?" + urlencode(params)


def normalize_browse_result(config: Dict[str, Any], payload: Any,
                            query: str) -> Dict[str, Any]:
    return _normalize_listing(_browse_spec(config, query), payload)


def _operation_url(config: Dict[str, Any], spec: Dict[str, Any], *,
                   path_values: Optional[Dict[str, str]] = None,
                   params: Optional[Dict[str, Any]] = None) -> str:
    path = spec["path_template"].format(**(path_values or {}))
    query = dict(spec.get("fixed_params", {}))
    query.update({key: str(value) for key, value in (params or {}).items()})
    suffix = "?" + urlencode(query) if query else ""
    return config["base_url"].rstrip("/") + path + suffix


def collection_list_url(config: Dict[str, Any]) -> str:
    spec = config["collection_list"]
    params = {}
    if spec.get("page_param"):
        params[spec["page_param"]] = 1
    if spec.get("page_size_param"):
        params[spec["page_size_param"]] = spec["page_size"]
    return _operation_url(config, spec, params=params)


def normalize_collections(config: Dict[str, Any], payload: Any) -> list[dict]:
    spec = config["collection_list"]
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
    return items


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
    items = _get_path(payload, config["collection_details"].get("response_items", ""))
    if not isinstance(items, list):
        raise ValueError("书源集详情响应不是书源数组")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("书源集详情包含非书源对象")
    if not items:
        raise ValueError("书源集详情没有书源")
    return items


def detail_url(config: Dict[str, Any], item_id: str) -> str:
    item_id = str(item_id or "").strip()
    if (not item_id or len(item_id) > 128 or item_id in (".", "..")
            or any(not (char.isalnum() or char in "-_.") for char in item_id)):
        raise ValueError("仓库条目标识无效")
    return config["base_url"] + config["details"]["path_template"].format(
        id=quote(item_id, safe=""))


def normalize_details(config: Dict[str, Any], payload: Any) -> list[dict]:
    items = _get_path(payload, config["details"]["response_items"])
    if not isinstance(items, list):
        raise ValueError("仓库详情响应不是书源数组")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("仓库详情数组包含非书源对象")
    return items



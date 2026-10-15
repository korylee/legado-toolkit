# -*- coding: utf-8 -*-
# 外部书源安全导入：解析前端回传的 JSON，按 URL + 规则指纹三分类。
#
# 语义（**外部源导入的唯一实现**——CLI 那条独立的台账链路已于 2026-09-19 删除，
# 它写的是另一个库、且那个库在本机根本不存在）：
#   - 新 URL         -> 写入管理库，按原分组推断健康状态（无信号默认待验证）
#   - 同 URL 同规则   -> 重复，跳过
#   - 同 URL 不同规则 -> 冲突，不覆盖候选库；原始内容留存 data/imports/conflicts/
import asyncio
import ipaddress
import json
import os
import socket
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.deps import get_store
from backend.schemas import (ImportBody, ImportUrlRequest, RepositoryCollectionRequest,
                             RepositoryDetailsRequest, RepositoryBrowseRequest)
from core.checker import static_rule_complete
from core.loader import _normalize_url, fingerprint
from core.models import BOOK_SOURCE_TYPE_NAMES
from core.organizer import STATUS_GROUP_NAMES, infer_health_from_group
from core.paths import data_path
from core.sanitize import clean_source
from core.tags import extract_user_tags_from_group, merge_group

router = APIRouter()

_REMOTE_JSON_MAX_BYTES = 8 * 1024 * 1024
_REMOTE_JSON_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=10)


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, addresses):
        self.addresses = addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {"hostname": host, "host": address, "port": port,
             "family": af, "proto": socket.IPPROTO_TCP,
             "flags": socket.AI_NUMERICHOST}
            for af, address in self.addresses
        ]

    async def close(self):
        return None


async def _public_https_target(value: str):
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "JSON 链接格式不正确") from exc
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username or parsed.password or port not in (None, 443)):
        raise HTTPException(400, "只支持无账号信息的 HTTPS JSON 链接（默认端口或 443）")
    host = parsed.hostname.rstrip(".")
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM),
            timeout=5)
    except asyncio.TimeoutError as exc:
        raise HTTPException(502, "JSON 链接域名解析超时") from exc
    except OSError as exc:
        raise HTTPException(400, "链接域名无法解析：%s" % exc) from exc
    addresses = []
    for family, _socktype, _proto, _canonname, sockaddr in records:
        address = sockaddr[0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise HTTPException(400, "链接域名解析结果无效") from exc
        if not ip.is_global:
            raise HTTPException(400, "链接域名必须解析到公网地址")
        pair = (family, address)
        if pair not in addresses:
            addresses.append(pair)
    if not addresses:
        raise HTTPException(400, "链接域名没有可用的公网地址")
    return parsed, host, addresses


async def _fetch_remote_payload(url: str, *, follow_public_redirects: bool = False,
                                allowed_redirect_hosts: Optional[set[str]] = None,
                                as_text: bool = False, _redirects: int = 0) -> Any:
    parsed, _host, addresses = await _public_https_target(url)
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(addresses), use_dns_cache=False)
    redirect_target = None
    try:
        async with aiohttp.ClientSession(
                connector=connector, timeout=_REMOTE_JSON_TIMEOUT,
                trust_env=False) as session:
            async with session.get(
                    parsed.geturl(), allow_redirects=False,
                    headers={"Accept": "text/html,application/json" if as_text else "application/json"}) as response:
                if 300 <= response.status < 400:
                    location = response.headers.get("Location", "")
                    if not follow_public_redirects:
                        raise HTTPException(400, "JSON 链接发生跳转，请改用最终 HTTPS 链接")
                    if not location or _redirects >= 3:
                        raise HTTPException(400, "JSON 链接跳转次数过多或目标无效")
                    redirect_target = urljoin(parsed.geturl(), location)
                    redirect_host = (urlsplit(redirect_target).hostname or "").rstrip(".").lower()
                    if (allowed_redirect_hosts is not None
                            and redirect_host not in allowed_redirect_hosts):
                        raise HTTPException(400, "JSON 链接跳转到未配置的主机")
                elif response.status != 200:
                    raise HTTPException(502, "获取 JSON 失败（HTTP %d）" % response.status)
                else:
                    if (response.content_length is not None
                            and response.content_length > _REMOTE_JSON_MAX_BYTES):
                        raise HTTPException(413, "JSON 文件超过 8 MB 上限")
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > _REMOTE_JSON_MAX_BYTES:
                            raise HTTPException(413, "JSON 文件超过 8 MB 上限")
                        chunks.append(chunk)
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HTTPException(502, "获取 JSON 失败：%s" % exc) from exc
    if redirect_target:
        return await _fetch_remote_payload(
            redirect_target, follow_public_redirects=True,
            allowed_redirect_hosts=allowed_redirect_hosts,
            as_text=as_text, _redirects=_redirects + 1)
    content = b"".join(chunks)
    try:
        if as_text:
            return content.decode("utf-8-sig")
        return json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "链接内容不是有效的 UTF-8 文档") from exc


async def _fetch_remote_json(url: str) -> str:
    data = await _fetch_remote_payload(url)
    if not isinstance(data, list):
        raise HTTPException(400, "链接内容不是书源 JSON 数组")
    return json.dumps(data, ensure_ascii=False)


def _repository_or_404(repository_id: str) -> Dict[str, Any]:
    from backend.import_repositories import get_repository

    repository = get_repository(repository_id)
    if repository is None:
        raise HTTPException(404, "书源仓库不存在")
    return repository


def _validate_repository_url(repository: Dict[str, Any], url: str) -> None:
    base = urlsplit(repository["base_url"])
    target = urlsplit(url)
    if (base.scheme != "https" or not base.hostname or base.port not in (None, 443)
            or target.scheme != "https" or target.hostname != base.hostname
            or target.port not in (None, 443) or target.username or target.password):
        raise HTTPException(500, "书源仓库请求超出配置主机")


async def _fetch_repository_payload(
        repository: Dict[str, Any], url: str, *, follow_public_redirects: bool = False,
        allowed_redirect_hosts: Optional[set[str]] = None) -> Any:
    _validate_repository_url(repository, url)
    return await _fetch_remote_payload(
        url, follow_public_redirects=follow_public_redirects,
        allowed_redirect_hosts=allowed_redirect_hosts)


async def _fetch_repository_text(repository: Dict[str, Any], url: str) -> str:
    _validate_repository_url(repository, url)
    return await _fetch_remote_payload(url, as_text=True)


async def _fetch_json_from_html_detail(
        repository: Dict[str, Any], page_url: str, spec: Dict[str, Any]) -> Any:
    from backend.import_repositories import extract_json_link

    document = await _fetch_repository_text(repository, page_url)
    try:
        json_url = extract_json_link(
            document, page_url, spec["json_link_selector"])
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    return await _fetch_repository_payload(
        repository, json_url,
        follow_public_redirects=bool(spec.get("follow_public_redirects")),
        allowed_redirect_hosts=set(spec.get("allowed_redirect_hosts", []))
        if spec.get("allowed_redirect_hosts") is not None else None)


@router.get("/repositories")
def list_import_repositories():
    from backend.import_repositories import list_repositories

    return {"items": list_repositories()}
@router.post("/repositories/{repository_id}/browse")
async def browse_import_repository(
        repository_id: str, body: RepositoryBrowseRequest,
        st=Depends(get_store)):
    from backend.import_repositories import (browse_format, browse_url,
                                             normalize_browse_result, supports_view)

    repository = _repository_or_404(repository_id)
    if not supports_view(repository, "browse"):
        raise HTTPException(400, "该书源仓库不支持浏览")
    query = body.query.strip()
    try:
        url = browse_url(repository, query, body.page)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = (await _fetch_repository_text(repository, url)
               if browse_format(repository, query) == "html"
               else await _fetch_repository_payload(repository, url))
    try:
        result = normalize_browse_result(repository, payload, query, body.page)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    live_urls = st.live_fingerprints()
    for item in result["items"]:
        url = _import_key(str(item.get("url") or ""))
        item["in_library"] = url in live_urls if url else None
    return result






@router.get("/repositories/{repository_id}/collections")
async def list_repository_collections(
        repository_id: str, page: int = Query(default=1, ge=1, le=10000)):
    from backend.import_repositories import (collection_list_url, normalize_collections,
                                             supports_view)

    repository = _repository_or_404(repository_id)
    if not supports_view(repository, "collections") or "collection_list" not in repository:
        raise HTTPException(400, "该书源仓库不支持书源集浏览")
    spec = repository["collection_list"]
    url = collection_list_url(repository, page)
    payload = (await _fetch_repository_text(repository, url)
               if spec.get("format") == "html"
               else await _fetch_repository_payload(repository, url))
    try:
        return normalize_collections(repository, payload, page)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/repositories/{repository_id}/collection-details")
async def get_import_repository_collection(repository_id: str,
                                          body: RepositoryCollectionRequest):
    from backend.import_repositories import (collection_details_url,
                                             normalize_collection_details, supports_view)

    repository = _repository_or_404(repository_id)
    if not supports_view(repository, "collections") or "collection_details" not in repository:
        raise HTTPException(400, "该书源仓库不支持书源合集导入")
    try:
        url = collection_details_url(
            repository, body.collection_id, body.collection_url or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    detail_spec = repository["collection_details"]
    if detail_spec.get("format") == "html_json":
        payload = await _fetch_json_from_html_detail(repository, url, detail_spec)
    elif detail_spec.get("url_field"):
        payload = await _fetch_remote_payload(url, follow_public_redirects=True)
    else:
        payload = await _fetch_repository_payload(repository, url)
    try:
        return {"sources": normalize_collection_details(repository, payload)}
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/repositories/{repository_id}/details")
async def get_import_repository_details(
        repository_id: str, body: RepositoryDetailsRequest):
    from backend.import_repositories import detail_url, normalize_details

    repository = _repository_or_404(repository_id)
    semaphore = asyncio.Semaphore(5)

    async def fetch_one(item_id: str):
        try:
            url = detail_url(repository, item_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        async with semaphore:
            detail_spec = repository["details"]
            if detail_spec.get("format") == "html_json":
                payload = await _fetch_json_from_html_detail(repository, url, detail_spec)
            else:
                payload = await _fetch_repository_payload(repository, url)
        try:
            sources = normalize_details(repository, payload)
        except ValueError as exc:
            raise HTTPException(502, "仓库详情格式无效（%s）" % item_id) from exc
        if not sources:
            raise HTTPException(502, "仓库详情没有书源（%s）" % item_id)
        return {"id": item_id, "sources": sources}

    return {"items": await asyncio.gather(
        *(fetch_one(item_id) for item_id in dict.fromkeys(body.ids)))}


@router.post("/url")
async def fetch_import_url(body: ImportUrlRequest):
    content = await _fetch_remote_json(body.url)
    return {"content": content, "source": body.url.strip()}


@router.post("/preview")
def preview_import(body: ImportBody, st=Depends(get_store)):
    data = _parse_import_content(body.content)
    overwrite = body.conflict_strategy == "overwrite"
    known_user_tags = st.known_user_tags()
    live_fingerprints = st.live_fingerprints()
    items = []
    counts = {"new": 0, "duplicate": 0, "conflict": 0,
              "update": 0, "invalid": 0, "rules_incomplete": 0}

    for index, original in enumerate(data):
        if not isinstance(original, dict):
            counts["invalid"] += 1
            items.append({"index": index, "name": "", "url": "", "status": "invalid"})
            continue
        source = deepcopy(original)
        clean_source(source)
        url = _import_url(source)
        name = str(source.get("bookSourceName", "") or "")
        if not url:
            counts["invalid"] += 1
            items.append({"index": index, "name": name, "url": "", "status": "invalid"})
            continue

        existing = live_fingerprints.get(url) or None
        current_fingerprint = fingerprint(source)
        if existing is None:
            status = "new"
            _normalize_group(source, known_user_tags)
            live_fingerprints[url] = fingerprint(source)
            counts["new"] += 1
        elif current_fingerprint == existing:
            status = "duplicate"
            counts["duplicate"] += 1
        elif overwrite:
            status = "update"
            counts["update"] += 1
            _normalize_group(source, known_user_tags)
            live_fingerprints[url] = fingerprint(source)
        else:
            status = "conflict"
            counts["conflict"] += 1

        incomplete = static_rule_complete(source) is False
        if incomplete:
            counts["rules_incomplete"] += 1
        items.append({"index": index, "name": name, "url": url,
                      "status": status, "rules_incomplete": incomplete})
    return {"total": len(data), "counts": counts, "items": items}


def _parse_import_content(raw_content: str):
    content = str(raw_content or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "JSON 解析失败: %s" % exc) from exc
    if not isinstance(data, list):
        raise HTTPException(400, "内容不是书源数组（应为 JSON 数组）")
    return data


def _normalize_group(source, allowed_user_tags):
    """为导入的新源规范系统标签：按原分组推断健康状态，无信号默认「待验证」。

    保留外部源的健康状态（可用/失效/需翻墙等）；用户标签只保留白名单里的
    （``Store.known_user_tags()``：默认 R18 / 正版 + 库里已有的用户标签），
    其余直接清掉——决策见 lessons §三十二。
    """
    source_type = int(source.get("bookSourceType", 0) or 0)
    type_tag = BOOK_SOURCE_TYPE_NAMES.get(source_type, "")
    raw_group = str(source.get("bookSourceGroup", "") or "")
    user_tags = [t for t in extract_user_tags_from_group(raw_group)
                 if t in allowed_user_tags]
    status_tag = STATUS_GROUP_NAMES.get(infer_health_from_group(raw_group), "待验证")
    source["bookSourceGroup"] = merge_group([type_tag, status_tag], user_tags)
    return source


def _import_key(url: str) -> str:
    """导入身份键：URL 去 fragment 再归一（署名不是源身份）。"""
    return _normalize_url(str(url or "").split("#", 1)[0].strip())


def _import_url(source) -> str:
    """把 `bookSourceUrl` 就地换成去 fragment 的原文，返回导入身份键。

    必须就地改写：`raw_json` 存的是这里的原文，只改比对键会让它与 `source_url` 列分叉。
    比对键与入库键也必须是同一个，否则 `upsert_sources` 按 `source_url` 找不到那条，
    判定说「覆盖」而实际新增一行。
    """
    raw = str(source.get("bookSourceUrl", "") or "")
    source["bookSourceUrl"] = raw.split("#", 1)[0].strip()
    return _import_key(source["bookSourceUrl"])


def _write_conflicts(conflicts):
    """把冲突源原样留存到文件，返回文件路径（无冲突返回空串）。"""
    if not conflicts:
        return ""
    out_dir = data_path("imports", "conflicts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "conflict_%s.json"
                        % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conflicts, f, ensure_ascii=False, indent=2)
    return path


@router.post("")
def import_sources(body: ImportBody, st=Depends(get_store)):
    data = _parse_import_content(body.content)

    new_count = duplicate_count = conflict_count = invalid_count = updated_count = 0
    overwrite = str(getattr(body, "conflict_strategy", "keep") or "keep") == "overwrite"
    conflict_urls = []
    conflict_sources = []

    # 导入边界的用户标签白名单：默认标签 + 库里已有标签。
    # 同一份名单在 Store.upsert_sources 里是第二道防线。
    allowed_user_tags = st.known_user_tags()

    # **一次把在用的指纹全捞进来**，别在循环里逐条查。逐条查 + 逐条写实测是
    # O(n²)：3000 条要走 3000 次 SELECT、3000 次事务提交，60 秒（每条 20ms）。
    # 分类全在内存里做，写入合并到循环后的两次调用——那是同一个数量级里最省的
    # 改法：upsert_sources 每次调用内部还要全表扫一遍 known_user_tags()，
    # 调 3000 次就是扫 3000 遍。
    live_fp = st.live_fingerprints()
    to_create: List[Dict[str, Any]] = []
    to_overwrite: List[Dict[str, Any]] = []
    # 真正落库的 URL（新建 + 覆盖）：导入后校验要按它提交范围——重复/冲突/
    # 无效的都没落库，不该进校验范围（重复的结论已是新的，校验是纯浪费）。
    imported_urls: List[str] = []

    for item in data:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        clean_source(item)
        url = _import_url(item)
        if not url:
            invalid_count += 1
            continue

        # 只比「在用」的那行。回收站里同 URL 的历史版本**不参与**——它们不在用，
        # 不构成「已存在」，于是「删了再导入」会直接新建一行在用的（旧版留在回收站
        # 可回滚），而不是被判冲突挡在门外。
        existing_fp = live_fp.get(url) or None
        if existing_fp is None:
            _normalize_group(item, allowed_user_tags)
            to_create.append(item)
            new_count += 1
            imported_urls.append(url)
            # 同一份文件里出现两次同一个 URL 时，第二次要算「重复」而不是又建一遍——
            # 批量化之后循环里不再落库，所以得自己把状态补上
            live_fp[url] = fingerprint(item)
        elif fingerprint(item) == existing_fp:
            duplicate_count += 1
        elif overwrite:
            # 用户选了「用导入的覆盖」。用户标签不受影响：upsert 的 DO UPDATE
            # 不含 user_tags（那是用户在界面上勾的，不该被外部源改写）
            _normalize_group(item, allowed_user_tags)
            to_overwrite.append(item)
            updated_count += 1
            imported_urls.append(url)
            live_fp[url] = fingerprint(item)
        else:
            conflict_count += 1
            conflict_urls.append(url)
            conflict_sources.append(item)

    # 落库合并成两次（每次一个事务）。要覆盖的那批走 upsert；新建的那批里
    # 不可能有还留在回收站里的同 URL——有的话它也不在 live_fp 里，会落到这里，
    # 而部分唯一索引只约束在用的行，所以照样能插进去（旧版仍留在回收站）
    if to_overwrite:
        st.upsert_sources(to_overwrite)
    if to_create:
        st.upsert_sources(to_create)

    return {
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "updated_count": updated_count,
        "conflict_count": conflict_count,
        "invalid_count": invalid_count,
        "total": len(data),
        "conflict_urls": conflict_urls,
        "conflict_file": _write_conflicts(conflict_sources),
        # 真正落库的 URL（新建 + 覆盖）：导入后校验按它提交范围
        "imported_urls": imported_urls,
    }

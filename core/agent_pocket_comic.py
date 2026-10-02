# -*- coding: utf-8 -*-
"""Guarded strategy generation for dynamic comic content.

This module only turns verified runtime *shapes* into a bounded draft strategy. It
never decrypts a page, copies image URLs, calls a model, or claims that a draft has
passed App verification.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_RUNTIME_FIELD = "params.chapter_images"


def _target(context: Mapping[str, Any]) -> Mapping[str, Any]:
    target = context.get("target")
    return target if isinstance(target, Mapping) else {}


def _runtime(context: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = context.get("runtime")
    return runtime if isinstance(runtime, Mapping) else {}


def _failure(code: str, reason: str, *, needed: str = "") -> dict[str, Any]:
    out = {"ready": False, "code": code, "reason": reason}
    if needed:
        out["needed"] = needed
    return out


def build_pocket_comic_strategy(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a strategy only after concrete chapter/runtime/image checks pass.

    The input is a normalized AgentContext. A successful result contains an ES5
    WebView script and a generic image content rule; it contains no site URL,
    encryption key, or copied image address.
    """
    context = context if isinstance(context, Mapping) else {}
    if str(context.get("layer") or "").upper() != "L3":
        return _failure("not_l3", "当前页面不是 L3 动态内容，不能套用口袋漫画策略")
    target = _target(context)
    if str(target.get("step") or "") != "content":
        return _failure("content_step_required", "必须从正文步骤进入，不能从搜索或目录猜正文策略")
    if target.get("chapter_selected") is not True:
        return _failure("chapter_required", "请先选择一个具体章节再检查运行时图片")

    runtime = _runtime(context)
    object_types = {str(item).lower() for item in (runtime.get("object_types") or [])}
    if not ({"dict", "object"} & object_types):
        return _failure("params_not_object", "运行时 params 不是对象，不能生成正文策略")
    keys = {str(item) for item in (runtime.get("keys") or [])}
    if _RUNTIME_FIELD not in keys and "chapter_images" not in keys:
        return _failure("chapter_images_missing", "运行时没有确认 params.chapter_images")

    counts = runtime.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    image_count = int(counts.get("chapter_images") or 0)
    accessible_count = int(counts.get("chapter_images_accessible") or 0)
    blob_count = int(counts.get("chapter_images_blob") or 0)
    if image_count <= 0:
        return _failure("images_empty", "params.chapter_images 没有图片")
    if blob_count:
        return _failure("images_blob_only", "图片地址包含 blob，不能把临时地址写入书源")
    if accessible_count != image_count:
        return _failure("images_not_accessible", "图片可访问性尚未逐张确认", needed="chapter_images_accessible")

    web_js = (
        "var imgs = params.chapter_images;"
        "var urls = [];"
        "if (Object.prototype.toString.call(imgs) === '[object Array]') {"
        " for (var i = 0; i < imgs.length; i++) {"
        "  if (imgs[i] && String(imgs[i]).indexOf('http') === 0) urls.push(String(imgs[i]));"
        " }"
        "} else if (imgs && String(imgs).indexOf('http') === 0) {"
        " urls.push(String(imgs));"
        "}"
        "result = urls.join('\\n');"
    )
    content_rule = (
        "@js:result.split('\\n').filter(function(x){return x;})"
        ".map(function(x){return '<img src=\"' + x + '\">';}).join('')"
    )
    return {
        "ready": True,
        "strategy": {
            "requires_webview": True,
            "runtime_field": _RUNTIME_FIELD,
            "content_mode": "media",
        },
        "draft": {
            "ruleContent": {
                "webJs": web_js,
                "content": content_rule,
                "imageStyle": "FULL",
            },
        },
        "verification": {"required": True, "method": "app"},
        "verification_note": "草稿仍需连 App 实测正文图片数量与可访问性",
    }

# -*- coding: utf-8 -*-
"""全局设置接口（当前只有校验参数）。

三个端点返回同一个形状，前端存下来就能实现「恢复默认」「与默认值比较」
「按后端给的上下界渲染表单」，不必在 JS 里再硬编码一份：

    {"values": {...}, "defaults": {...}, "limits": {...}}

这与 AGENTS.md 硬性约定 #7（系统标签枚举只在后端定义）是同一类规则。
"""

from fastapi import APIRouter, HTTPException

from backend.schemas import SettingsPatch
from core import settings_store

router = APIRouter()

#: 代理只认 http/https，理由见 core/settings_store._PROXY_SCHEMES
#: （aiohttp 原生不支持 socks5；fetch.py 已就此事立过「不要再写 socks5」的规矩）
_PROXY_SCHEMES = ("http://", "https://")


def _payload(values):
    return {
        "values": values,
        "defaults": settings_store.DEFAULTS,
        "limits": settings_store.LIMITS,
    }


def _reject_unsupported_proxy(value) -> None:
    """代理地址写错必须当场报错，不能让它在 core 层被静默收敛成空串。

    静默收敛的表现是「填了代理、保存成功、但所有请求其实还在直连」——
    而用户以为代理生效了，会去怀疑源而不是配置。
    """
    if not value:
        return
    if not str(value).strip().lower().startswith(_PROXY_SCHEMES):
        raise HTTPException(
            400, "代理地址只支持 http:// 或 https:// 开头（socks5 需要额外依赖 "
                 "aiohttp_socks，本项目未安装，填了也连不上）")


@router.get("")
def get_settings():
    return _payload(settings_store.load())


@router.patch("")
def patch_settings(body: SettingsPatch):
    # exclude_unset 是**关键**：没传的键不能出现在 patch 里。少了它，一次
    # 「只改并发」的保存会把其余 5 项一起打回默认——打回的值本身合法，
    # 界面上完全看不出来，是这次改动里唯一会静默丢数据的地方
    data = body.model_dump(exclude_unset=True)
    _reject_unsupported_proxy((data.get("check") or {}).get("proxy"))
    return _payload(settings_store.update(data))


@router.post("/reset")
def reset_settings():
    return _payload(settings_store.reset())

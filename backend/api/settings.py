# -*- coding: utf-8 -*-
"""全局设置接口（这台机器怎么出去 + 本机引擎的环境）。

三个端点返回同一个形状，前端存下来就能实现「恢复默认」「与默认值比较」
「按后端给的上下界渲染表单」，不必在 JS 里再硬编码一份：

    {"values": {...}, "defaults": {...}, "limits": {...}}

这与 AGENTS.md 硬性约定 #7（系统标签枚举只在后端定义）是同一类规则。
"""

from fastapi import APIRouter, HTTPException

from backend.schemas import SettingsPatch
from core import settings_store

router = APIRouter()

#: 引擎那个代理**只认 http://**（十-3）。三条理由都在那一个值上：
#: ① 上游 `HttpHelper.getProxyClient` 是拿正则 `(http|socks4|socks5)://…` 去匹配的，
#: `https://` 匹配不到、它会 `ms.first()` **抛异常**（留着比丢掉更糟）；
#: ② 我们自己那条抓取链（`core/fetch.py`）不认 socks；③ 一个值要两处都能用。
_PROXY_SCHEMES = ("http://",)


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

    **只填 host:port 是合法的**（`core.settings_store._to_app_proxy` 会补成 http://），
    所以这里与那边同一套判据：带 scheme 的只认 http://，不带的按 host:port 形状收。
    """
    import re

    if not value:
        return
    s = str(value).strip()
    if s.lower().startswith(_PROXY_SCHEMES):
        return
    if re.match(r"^[\w.-]+:\d{2,5}(@.*@.*@)?$", s):
        return
    if True:
        raise HTTPException(
            400, "代理只支持 http:// 开头（可以只填 host:port，会自动补 http://）。"
                 "socks 与 https 这条链用不了：上游用正则匹配代理串，匹配不到会直接报错")


@router.get("")
def get_settings():
    return _payload(settings_store.load())


@router.patch("")
def patch_settings(body: SettingsPatch):
    # exclude_unset 是**关键**：没传的键不能出现在 patch 里。少了它，一次
    # 「只改并发」的保存会把其余 5 项一起打回默认——打回的值本身合法，
    # 界面上完全看不出来，是这次改动里唯一会静默丢数据的地方
    data = body.model_dump(exclude_unset=True)
    _reject_unsupported_proxy((data.get("network") or {}).get("proxy"))
    return _payload(settings_store.update(data))


@router.post("/reset")
def reset_settings():
    return _payload(settings_store.reset())

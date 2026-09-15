# -*- coding: utf-8 -*-
"""全局设置 JSON 存储（本地单用户，原子写）。

与 ``llm_store`` 的分工：llm_store 存**多套模型配置**（profiles 列表，可增删改），
这里存**单份全局设置**（固定键值）。两者同放 ``data/config/``，但结构不同，
所以各自一份读写——共同点只有 4 行 json 落盘，抽出来会造出一个只有两个调用方、
参数还得配对的抽象。

**收敛纪律**：谁都不能信任写进来的值（前端表单、未来的 CLI、手改的文件）。
所有出口都过 :func:`coerce`，非法值一律回落默认而不是报错——设置坏掉不该让
校验任务起不来。``probe_depth`` 的收敛口径与 ``checker.py`` 一致（不在 1/2/3
就落 1），两处必须是同一个规则，否则「设置里存 3」和「实际按 1 跑」会分家。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Optional

from core.paths import data_path

SETTINGS_NAME = "settings.json"
VERSION = 1

#: 全局设置的**唯一权威来源**。前端不硬编码默认值——``GET /api/settings`` 把这份
#: 原样下发（含 defaults），「恢复默认」直接用后端给的值，避免两处各存一份漂移
#: （AGENTS.md 硬性约定 #7 记过系统标签枚举的同类事故）。
#:
#: 按 section 分区（当前只有 check）：后续加「App 连接」等平行设置时不用再动
#: schema，也不会和已有键挤在同一层。
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "check": {
        "concurrency": 50,
        "timeout": 8.0,
        "probe_depth": 1,
        "probe_search": True,
        "verify_ssl": True,
        "proxy": "",
        #: 缓存有效期（天）。可用源留久一点；其余状态一律短 TTL——「待验证」
        #: 「需代理复检」长期停在旧结论上，比多校验几次更糟。
        #: 这两个数是**唯一权威**，core/checker.py 从这里引用默认值。
        "cache_ttl_ok": 14,
        "cache_ttl_other": 7,
    },
}

#: 探测深度的合法取值。``AsyncChecker`` 的收敛口径就是这三个数
#: （``core/checker.py`` 直接 import 这里的常量），改这里即两边同时改。
PROBE_DEPTHS = (1, 2, 3)

#: 各键的合法区间。**「clamp 到多少」的唯一定义处**——前端表单的
#: ``el-input-number`` 上下界由 ``GET /api/settings`` 的 ``limits`` 下发，
#: 不在 JS 里再写一份。
LIMITS: Dict[str, tuple] = {
    "concurrency": (1, 200),
    "timeout": (1.0, 120.0),
    "probe_depth": PROBE_DEPTHS,
    "cache_ttl_ok": (1, 365),
    "cache_ttl_other": (1, 365),
}

#: 代理只认 http/https。**故意不含 socks5**：aiohttp 原生不支持（要 ``aiohttp_socks``，
#: 本项目未装），``core/fetch.py`` 的 ``proxy`` 说明已就此立过规矩——「不要再写 socks5
#: 以免加深误导」（CLI 帮助与 WORKFLOW.md 里那处 socks5 是既有的文档失实）。
#: API 层会在写入前拦下 socks5 并给出明确报错，这里只是手改文件时的最后一道兜底。
_PROXY_SCHEMES = ("http://", "https://")

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off", "")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_int(value: Any, default: int, lo: int, hi: int) -> int:
    # bool 是 int 的子类，不挡掉的话 True 会变成并发数 1
    if isinstance(value, bool):
        return default
    try:
        return int(_clamp(int(float(value)), lo, hi))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    # nan 必须显式挡掉：json 里写不出 nan，但 NaN/Infinity 能被 Python 的 json
    # 读进来，而 min/max 对 nan 的比较恒为 False，clamp 会静默给出错值
    if not math.isfinite(f):
        return default
    return _clamp(f, lo, hi)


def _to_bool(value: Any, default: bool) -> bool:
    """``bool("false")`` 是 True，所以字符串必须显式认。

    词表与 ``backend/__main__.py`` 解析 LEGADO_RELOAD 的口径一致
    （1/true/yes/on）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_proxy(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return s if s.lower().startswith(_PROXY_SCHEMES) else ""


def _to_probe_depth(value: Any) -> int:
    """不在 ``PROBE_DEPTHS`` 里一律落 1，与 ``AsyncChecker`` 的收敛同一规则。

    「回落」而不是「落到最近的合法值」：5 是垃圾输入，不是「比 3 更深」。
    """
    if isinstance(value, bool):
        return 1
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 1
    return n if n in PROBE_DEPTHS else 1


#: ``(section, key)`` → 收敛函数。**没登记就是未知键**，由 coerce 返回 None 丢弃。
#: 默认值一律从 DEFAULTS 取，不在这里写第二遍字面量。
_SPECS: Dict[tuple, Any] = {
    ("check", "concurrency"): lambda v: _to_int(
        v, DEFAULTS["check"]["concurrency"], *LIMITS["concurrency"]),
    ("check", "timeout"): lambda v: _to_float(
        v, DEFAULTS["check"]["timeout"], *LIMITS["timeout"]),
    ("check", "probe_depth"): _to_probe_depth,
    ("check", "probe_search"): lambda v: _to_bool(
        v, DEFAULTS["check"]["probe_search"]),
    ("check", "verify_ssl"): lambda v: _to_bool(
        v, DEFAULTS["check"]["verify_ssl"]),
    ("check", "proxy"): _to_proxy,
    ("check", "cache_ttl_ok"): lambda v: _to_int(
        v, DEFAULTS["check"]["cache_ttl_ok"], *LIMITS["cache_ttl_ok"]),
    ("check", "cache_ttl_other"): lambda v: _to_int(
        v, DEFAULTS["check"]["cache_ttl_other"], *LIMITS["cache_ttl_other"]),
}


def coerce(section: str, key: str, value: Any) -> Any:
    """把任意输入收敛成该键的合法值；**未知键返回 None**（调用方据此丢弃）。

    对 ``ops.py`` 这类取参数的地方也是唯一入口——payload 传来的值同样要过这里，
    不然「设置里存 50、payload 里传 9999」会绕过区间检查直接进 Semaphore。
    """
    spec = _SPECS.get((section, key))
    if spec is None:
        return None
    return spec(value)


def settings_path() -> str:
    """env 覆盖优先——测试靠它隔离，不必碰真实 data/config/。"""
    env = os.getenv("LEGADO_SETTINGS")
    if env:
        return env
    d = data_path("config")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, SETTINGS_NAME)


def _empty() -> Dict[str, Any]:
    # 逐 section 复制：DEFAULTS 是模块级共享对象，直接返回会被调用方改坏
    out: Dict[str, Any] = {"schema_version": VERSION}
    for section, values in DEFAULTS.items():
        out[section] = dict(values)
    return out


def _atomic_save(data: Dict[str, Any]) -> None:
    path = settings_path()
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load() -> Dict[str, Any]:
    """读全局设置。文件缺失／损坏／字段缺失一律**逐键**回落 DEFAULTS。

    逐键而不是整段替换：文件里只写了一半的键（手改过、或将来加新键）时，
    没写的那几个要拿到默认值，不能整体作废成空。
    """
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = _empty()
    for section, values in DEFAULTS.items():
        raw = data.get(section)
        if not isinstance(raw, dict):
            continue
        for key in values:
            if key in raw:
                out[section][key] = coerce(section, key, raw[key])
    return out


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新并落盘，返回更新后的全量设置。

    未知的 section／key 直接丢弃，不抛错——前端多传了一个字段不该让保存失败。
    """
    data = load()
    for section, values in DEFAULTS.items():
        raw = (patch or {}).get(section)
        if not isinstance(raw, dict):
            continue
        for key in values:
            if key in raw:
                data[section][key] = coerce(section, key, raw[key])
    _atomic_save(data)
    return data


def reset() -> Dict[str, Any]:
    data = _empty()
    _atomic_save(data)
    return data


def resolve_check(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """算出「这次校验实际用哪套参数」：全局设置打底，``override`` 里非 None 的键覆盖。

    这是校验参数取值的**唯一入口**（``backend/api/ops.py`` 调它）。三层优先级
    「本次覆盖 > 全局设置 > 内置默认」中，后两层由 :func:`load` 完成，这里只做第一层。

    **判据必须是 ``is not None``，不能是 ``payload.get(k) or 全局``**：
    ``False`` / ``0`` / ``""`` 都是有意义的值——
      - ``probe_search=False`` = 明确要跳过搜索探测
      - ``proxy=""`` = 明确要本次直连（哪怕全局配了代理）
    用 ``or`` 会把这三个当成「没传」，覆盖**静默失效**，现象只是「参数好像没生效」，
    几乎不可能从界面上查出来。

    反过来 ``None`` 视同「没传」→ 用全局值：覆盖表单是从全局值预填的，
    用户把输入框清空 = 想回落全局，而不是想回落编译期默认。

    覆盖值同样过 ``coerce``——它也是不可信输入，传 concurrency=9999 不该
    绕过区间检查直接进 Semaphore。
    """
    cfg = load()["check"]
    if isinstance(override, dict):
        for key in DEFAULTS["check"]:
            value = override.get(key)
            if value is not None:
                cfg[key] = value
    for key in DEFAULTS["check"]:
        cfg[key] = coerce("check", key, cfg[key])
    return cfg


__all__ = ["DEFAULTS", "LIMITS", "PROBE_DEPTHS", "SETTINGS_NAME", "VERSION",
           "coerce", "load", "reset", "resolve_check", "settings_path",
           "update"]

# -*- coding: utf-8 -*-
"""全局设置 JSON 存储（本地单用户，原子写）。

与 ``llm_store`` 的分工：llm_store 存**多套模型配置**（profiles 列表，可增删改），
这里存**单份全局设置**（固定键值）。两者同放 ``data/config/``，但结构不同，
所以各自一份读写——共同点只有 4 行 json 落盘，抽出来会造出一个只有两个调用方、
参数还得配对的抽象。

**收敛纪律**：谁都不能信任写进来的值（前端表单、未来的 CLI、手改的文件）。
所有出口都过 :func:`coerce`，非法值一律回落默认而不是报错——设置坏掉不该让
校验任务起不来。``probe_depth`` 的收敛口径就是 :func:`_to_probe_depth`
（不在 ``PROBE_DEPTHS`` 里就落 :data:`DEPTH_HOME`）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

from core.paths import data_path

SETTINGS_NAME = "settings.json"
#: 设置文件的 schema 版本。2 = 探测深度合并成一根四档轴（见 `_migrate_legacy`）。
#: **加一就意味着 `_migrate_legacy` 多一条迁移分支**，不是单纯的标记位。
VERSION = 2

#: 探测深度：一根轴四档，分别对应实际验证阶段。
#:
#:   1 主页  仅域名探测（1 次请求）
#:   2 搜索  搜索探测 + 命中判定（+1~2 次）
#:   3 目录  详情页 + 目录页，比对章节数（+2 次）
#:   4 正文  章节页抓一章全文（+1 次）
#:
#: 合并前这里是「深度 1/2/3」**加**一个独立的 `probe_search` 开关，两根轴能配出
#: 非法组合：关掉搜索探测却选 2/3 档——目录/正文的门要求 `search_hit`，于是永远
#: 进不去，深度白设。合成一根轴后，设置直接对应实际验证阶段。
PROBE_DEPTHS = (1, 2, 3, 4)
DEPTH_HOME, DEPTH_SEARCH, DEPTH_TOC, DEPTH_CONTENT = PROBE_DEPTHS

#: **下发给界面渲染下拉**的那份（``LIMITS["probe_depth"]`` 用它）：从搜索档起，
#: **不含主页档**。
#:
#: 为什么主页档不再作为选项（2026-09-20 定）：本地探测链每档本来都**先打主页
#: 探测**（判不可达就直接停、不再发后续请求），所以「谁不可达 / 证书坏 / 需翻墙」
#: 这套产出，搜索档拿到的与主页档**逐字相同**，
#: 对不可达的源花的请求数也一样；主页档唯一独有的只是「可达的源也省掉后续请求」这种
#: 更快的扫地（而它本来也不是默认档）。删掉它换来的是**引擎的挡位词表对齐**——
#: 档位剩 搜索/目录/正文，与 ``JVM_DEPTHS`` 三项一一对应，界面上不再出现实现分叉。
#:
#: ⚠️ **这与「合法性」是两件事，别合并**（``PROBE_DEPTHS`` 不动）：
#: ① ``_migrate_legacy`` 把「没开搜索探测」的 v1 配置映射成 :data:`DEPTH_HOME`；
#: ② :func:`_to_probe_depth` 对非法值（5/0/None）的兜底也落在它上面；③ 历史
#: ``checks.probe_depth`` 行里存着 1。删掉合法性会同时打断这三条。
#:
#: **编号也不许平移**（2/3/4 → 1/2/3）：``checks.probe_depth`` 记的是「当时实际跑到
#: 第几档」，一平移整列历史值的含义就变了（AGENTS #5b）。所以这一改动**不涉及
#: ``CACHE_VERSION``**：编号含义没变、历史缓存仍然可比。
PROBE_DEPTH_CHOICES = (DEPTH_SEARCH, DEPTH_TOC, DEPTH_CONTENT)

#: JVM 校验的探测深度（S3）。与上面的 ``PROBE_DEPTHS`` 是两根轴：那根是本地
#: 回放的（1/2/3/4），这根是 App 真引擎跑到哪一段——两者编号独立，不要互相换算。
#: 取值与 ``appservice`` 的 ``ValidateService.DEPTH_*`` 一一对应。
JVM_DEPTHS = ("search", "toc", "content")

#: 全局设置的**唯一权威来源**。前端不硬编码默认值——``GET /api/settings`` 把这份
#: 原样下发（含 defaults），「恢复默认」直接用后端给的值，避免两处各存一份漂移
#: （AGENTS.md 硬性约定 #7 记过系统标签枚举的同类事故）。
#:
#: 按 section 分区（当前只有 check）：后续加「App 连接」等平行设置时不用再动
#: schema，也不会和已有键挤在同一层。
DEFAULTS: Dict[str, Dict[str, Any]] = {
    #: 这台机器**怎么出去**（环境类配置，不是「这次怎么跑」）。放全局是因为它同时被
    #: 两条引擎路（调试 / 批量校验）与生成后的验证读——按「这一次怎么跑」放弹框里，就会
    #: 出现「批量校验走了代理、调试没走」这种查不出来的不一致（AGENTS #8 的同一条意思）。
    "network": {
        #: 形如 ``http://host:port``（``host:port`` 会被补成 http://）。**只认 http**：
        #: 上游还支持 socks4/5，但我们自己那条抓取链（`core/fetch.py`）只支持 http——
        #: 一个值两处用，就按两处都能用的那个来（socks 值在这儿等于给 App 用、给我们炸）。
        "proxy": "",
    },
    #: **批量校验与调试的默认参数**，不是用户设置：`JvmSettingsPatch` 只收环境键
    #: （app_repo / android_sdk_dir / daemon_port），下面这些设置页没有控件——它们存在的
    #: 意义是「本次这一次跑什么」的种子与兜底。想改这一次就在批量校验弹框里改
    #: （`frontend/src/components/CheckJvmForm.vue`），那才是它们唯一的作用面；
    #: 改这里的值等于改代码常量。
    #: 默认值与区间仍然只有这一份（AGENTS #8）：`JVM_RUN_PARAMS` 的本次覆盖走
    #: `coerce` 收敛，前端经 `GET /api/settings` 拿种子。
    "jvm": {
        "app_repo": "",
        "android_sdk_dir": "",
        "keyword": "我",
        #: **批量校验**的每源总预算（各段共享）。25 = 吞吐优先：一段慢就把整源判成
        #: 「这一轮没验成」，而不是无限等它。**不再要「>60s 才分得清两种超时」**：
        #: 25 < App 的 okhttp callTimeout 60s，两种超时不可能混淆（归因只有「我们的
        #: 预算用尽」一种）；要分辨就给到 60s 以上，但那是调试那条路的事
        #: （`debug.chain_timeout` / `debug.stage_timeout`）。上界压在 LIMITS.jvm_check_timeout。
        "check_timeout": 25,
        "concurrency": 8,
        #: 默认停在**搜索**档：S1/S2 的历史结论就是这一档，深一档（目录+正文）
        #: 会让全量耗时成倍增长（搜索档实测 17 分钟 / 3774 条）。**默认值不许
        #: 静默改变既有行为的成本**——想验得更深由用户在设置里选，界面上会写清代价。
        "depth": "search",
        #: 批量分块大小：每块一次 JVM 占用，块间交还 lane（调试可插队）、
        #: 块完成即入库（取消/崩溃后可恢复）。默认 25 实测可调。
        "chunk_size": 25,
        #: daemon 端口窗口的起点（0 = 按 data 目录推导，见
        #: `core.jvm_daemon._daemon_base_port`）。**不是「这次怎么跑」**：改它等于改这台机器上
        #: 引擎的落点，所以放全局；也不进表单（界面上用不着，冲突时错误文案会给出动作）。
        "daemon_port": 0,
    },
    "debug": {
        #: 调试的两根预算，按「跑整链」还是「跑单步」分：
        #: - `chain_timeout`：整链墙钟上限（没指定具体步骤时用）。**必须大于桥的渲染
        #:   预算**：App 的 BackstageWebView 渲染上限固定 60s（App 侧的值，我们不改），
        #:   同值时渲染永远先被整链掐死。90 = 渲染吃满 60s 后仍留得出排队与收尾。
        #: - `stage_timeout`：单步调试按**段**取的那一份（跑哪一步就吃哪一段的预算）。
        #:   各段共享一根的话，慢的搜索段会把正文段挤掉——调试是单源、有人盯着，
        #:   宁可多等，也不该让「前面慢」惩罚「后面能不能验」。
        #:   依据 2026-10-21 本机实测（搜索 0.18–0.86s、正文 4.1/9.6s、渲染上限 60s）；
        #:   content 那一段不能低于 60s（App 的渲染上限），其余给观测值的若干倍。
        "chain_timeout": 90,
        "stage_timeout": {"search": 30, "explore": 30, "info": 30, "toc": 60, "content": 90},
    },
}

#: 各键的合法区间。**「clamp 到多少」的唯一定义处**——前端表单的上下界由
#: ``GET /api/settings`` 下发（AGENTS #8：默认值与区间不许在调用点再写一遍）。
LIMITS: Dict[str, tuple] = {
    "jvm_check_timeout": (5, 120),
    "jvm_concurrency": (1, 32),
    "jvm_chunk_size": (5, 200),
    # 0 = 自动（按 data 目录落到独占槽位）；显式值直接当窗口起点用
    "jvm_daemon_port": (1024, 65535),
    "debug_chain_timeout": (30, 300),
    #: 单步调试的预算范围：既管 `debug.stage_timeout` 的每个段，也管请求里显式给的
    #: timeout（一个请求就是一段）。与整链那根分开——整链 30s 起才有意义，单步 30s
    #: 也是同一把尺子，不必再来一个数字。
    "debug_stage_timeout": (30, 300),
    # 枚举型（与 probe_depth 同形）：前端据此渲染下拉，不在 JS 里再写一份
    "jvm_depth": JVM_DEPTHS,
}

_PROXY_SCHEMES = ("http://", "https://")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_int(value: Any, default: int, lo: int, hi: int) -> int:
    # bool 是 int 的子类，不挡掉的话 True 会变成并发数 1
    if isinstance(value, bool):
        return default
    try:
        # **OverflowError 也要接住**：`int(float("inf"))` 抛的是它（不是 ValueError）——
        # 漏掉的话，一个手写成 `1e400` 的设置会让**每一个**读设置的接口 500
        # （json.loads("1e400") 给的是 inf）。这是被测试抓出来的真 bug，别删那一项
        return int(_clamp(int(float(value)), lo, hi))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_proxy(value: Any) -> str:
    """代理串。**顺手把 ``host:port`` 补成 ``http://host:port``**——那是用户最常粘的形态，
    而两种消费方都要求带 scheme（urllib 只认带 scheme 的代理；上游 `AnalyzeUrl` 更是拿正则
    `(http|socks4|socks5)://…` 去匹配，匹配不到会直接抛异常）。补全比静默丢掉好：丢掉的表现
    是「代理好像没生效」而没有任何提示。

    仍然只认 http / https（`_PROXY_SCHEMES`）：socks 上游支持、我们的抓取链不支持，
    一个值要两处都能用。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(_PROXY_SCHEMES):
        return s
    # 没有 scheme 的形态：`host:port` / `host:port@user@pass@`（与上游正则的字段顺序一致）
    if re.match(r"^[\w.-]+:\d{2,5}(@.*@.*@)?$", s):
        return "http://" + s
    return ""


def _to_app_proxy(value: Any) -> str:
    """交给**本机引擎**的那个代理串：只认 ``http://host:port``（``host:port`` 自动补 http://）。

    **为什么不能像 `_to_proxy` 那样收 https**：上游是拿正则
    ``(http|socks4|socks5)://(.*):(\\d{2,5})(@.*@.*)?`` 去匹配的（`HttpHelper.getProxyClient`），
    匹配不到时它直接 ``ms.first()`` —— **抛异常**。也就是说 `https://…` 在我们这边「合法」，
    到 App 里会把这次请求炸掉，而现象只是「这一条源取不到东西」。

    socks 同理不收：上游支持、但我们自己那条抓取链不支持（`core/fetch.py` 只支持 http），
    一个值要两处都能用。**被拒的值存成空串**——界面上那句提示是用户唯一能看到的理由，
    所以提示里要写清只认 http。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if s.lower().startswith("http://"):
        return s
    # 没有 scheme：`host:port` / `host:port@user@pass@`（字段顺序与上游正则一致）
    if re.match(r"^[\w.-]+:\d{2,5}(@.*@.*@)?$", s):
        return "http://" + s
    return ""


def _to_path(value: Any) -> str:
    """目录路径：去引号与首尾空白；不存在也保留（自检接口负责报「不可达」，
    设置层不该因为目录暂时不存在的输入抛错——用户可能先填后建）。"""
    s = str(value or "").strip().strip('"').strip("'")
    return s


def _to_probe_depth(value: Any) -> int:
    """不在 ``PROBE_DEPTHS`` 里一律落 :data:`DEPTH_HOME`。

    「回落」而不是「落到最近的合法值」：5 是垃圾输入，不是「比 4 更深」。
    """
    if isinstance(value, bool):
        return DEPTH_HOME
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return DEPTH_HOME
    return n if n in PROBE_DEPTHS else DEPTH_HOME


def _to_daemon_port(value: Any) -> int:
    """daemon 端口窗口的起点：**0 = 自动**（按 data 目录落到独占槽位）。

    越界/垃圾输入回落 0（自动）而不是 clamp 到区间边上：自动落点一定在 Windows 动态端口
    范围（49152–65535）下面，而 clamp 出来的值可能正好落进去——症状会是「引擎时不时起不来」，
    不是报错。显式值在 ``LIMITS["jvm_daemon_port"]`` 里的原样接受（那是用户的选择）。
    """
    if isinstance(value, bool):
        return 0
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    lo, hi = LIMITS["jvm_daemon_port"]
    return n if lo <= n <= hi else 0


def _to_stage_timeouts(value: Any) -> Dict[str, int]:
    """调试的按段预算：**键集合就是 DEFAULTS 里那五个段**（同 `core.debug_keys.TARGETS`）。

    未知键丢掉、缺的键补默认值、越界按 ``LIMITS["debug_stage_timeout"]`` 收敛。
    **不做整段替换**：前端只改其中一段时，其余段要保住原值——整段替换会让没提到的段
    悄悄回默认值（AGENTS #17 那类静默漂移）。
    """
    base = dict(DEFAULTS["debug"]["stage_timeout"])
    if not isinstance(value, dict):
        return base
    lo, hi = LIMITS["debug_stage_timeout"]
    for key in base:
        if key in value:
            base[key] = _to_int(value[key], base[key], lo, hi)
    return base


def _migrate_legacy(data: Dict[str, Any]) -> None:
    """旧形状的搬运（就地改 ``data``，在逐键回落之前跑）。

    **代理从「校验参数」搬成「这台机器怎么出去」**（十-3）：`check.proxy` 原来只服务本地
    校验链，而它在两条引擎路上同样必要。新键没写、老键有值时就搬一次——**不动老键**，
    老键随 `check.*` 一起退役（十-4）。
    """
    net = data.get("network")
    if not isinstance(net, dict):
        net = {}
        data["network"] = net
    check = data.get("check")
    old_proxy = check.get("proxy") if isinstance(check, dict) else ""
    if not str(net.get("proxy") or "").strip() and str(old_proxy or "").strip():
        net["proxy"] = old_proxy
    # 键改名（2026-10-21）：`jvm.timeout` → `jvm.check_timeout`、`debug.timeout` →
    # `debug.chain_timeout`。存量文件里写的是旧键名——不搬的话 `load()` 会静默回落
    # 默认值：界面上「我设的那个数不见了」，而这次的默认值恰好也是 25/90，看起来
    # 一切正常（AGENTS #17 的同一个坑）。只搬一次，不动其他键；老键由下一次
    # `update()` 落盘时自然消失。
    for section, old_key, new_key in (("jvm", "timeout", "check_timeout"),
                                      ("debug", "timeout", "chain_timeout")):
        raw_section = data.get(section)
        if isinstance(raw_section, dict) and old_key in raw_section and new_key not in raw_section:
            raw_section[new_key] = raw_section.pop(old_key)


#: ``(section, key)`` → 收敛函数。**没登记就是未知键**，由 coerce 返回 None 丢弃。
#: 默认值一律从 DEFAULTS 取，不在这里写第二遍字面量。
_SPECS: Dict[tuple, Any] = {
    ("network", "proxy"): _to_app_proxy,
    ("jvm", "app_repo"): _to_path,
    ("jvm", "android_sdk_dir"): _to_path,
    ("jvm", "keyword"): lambda v: (str(v).strip() or DEFAULTS["jvm"]["keyword"]),
    ("jvm", "check_timeout"): lambda v: _to_int(
        v, DEFAULTS["jvm"]["check_timeout"], *LIMITS["jvm_check_timeout"]),
    ("jvm", "concurrency"): lambda v: _to_int(
        v, DEFAULTS["jvm"]["concurrency"], *LIMITS["jvm_concurrency"]),
    ("jvm", "chunk_size"): lambda v: _to_int(
        v, DEFAULTS["jvm"]["chunk_size"], *LIMITS["jvm_chunk_size"]),
    ("jvm", "daemon_port"): _to_daemon_port,
    ("jvm", "depth"): lambda v: (str(v).strip().lower()
                                 if str(v).strip().lower() in JVM_DEPTHS
                                 else DEFAULTS["jvm"]["depth"]),
    ("debug", "chain_timeout"): lambda v: _to_int(
        v, DEFAULTS["debug"]["chain_timeout"], *LIMITS["debug_chain_timeout"]),
    ("debug", "stage_timeout"): _to_stage_timeouts,
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
    _migrate_legacy(data)          # 旧的两根轴（深度 + 搜索开关）折成一根
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


def resolve_proxy() -> str:
    """这次出去走哪个代理（``network.proxy``）。

    **唯一入口**：两条引擎路（调试 / 批量校验）与生成后的验证都读它。分散读的后果是
    「批量校验走了代理、调试没走」——两边都「正常」，只有用户能看出网络出口不一样。
    """
    return str(load()["network"]["proxy"] or "")


def debug_chain_timeout() -> int:
    """单次调试的**整链**墙钟预算（秒，``debug.chain_timeout``）。

    **唯一入口**：本机引擎与连 App 两条调试路都读它（口径同 ``resolve_proxy``：
    分散读会让两条通道的预算不一样而没人知道）。没指定具体步骤时吃这个值；
    显式传值由路由按 ``LIMITS["debug_chain_timeout"]`` 拒收，不静默夹。CLI 的 argparse
    默认值是独立的另一条链路（AGENTS #8），不从这里统一。
    """
    return int(load().get("debug", {}).get("chain_timeout")
               or DEFAULTS["debug"]["chain_timeout"])


def debug_stage_timeout(stage: str) -> int:
    """单步调试按**段**取的预算（秒，``debug.stage_timeout``）。

    段名与 ``core.debug_keys.TARGETS`` 同集合（`search` / `explore` / `info` / `toc` /
    `content`）。不认识的段**回落到整链预算**，不编一个「默认段」——拼错的段名静默拿到
    某个预算，正是「原因没走到用户眼前」那类错（AGENTS #4）。
    """
    stages = load().get("debug", {}).get("stage_timeout") or {}
    try:
        got = int((stages or {}).get(str(stage or ""), 0) or 0)
    except (TypeError, ValueError):
        got = 0
    return got or debug_chain_timeout()


__all__ = ["DEFAULTS", "DEPTH_CONTENT", "DEPTH_HOME", "DEPTH_SEARCH", "DEPTH_TOC",
           "LIMITS", "PROBE_DEPTHS", "PROBE_DEPTH_CHOICES", "SETTINGS_NAME", "VERSION",
           "coerce", "debug_chain_timeout", "debug_stage_timeout", "load", "reset",
           "resolve_proxy", "settings_path", "update"]

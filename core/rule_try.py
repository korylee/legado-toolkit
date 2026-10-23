# -*- coding: utf-8 -*-
"""轻量验证（rule_try）的客户端：**唯一一份**。

「这一步的整页 HTML + 一条规则」直接过引擎（App 真解析器），返回取到的值——
不走调试链（不发请求、不跑事件流），把「填错建议规则 → 重调整步空跑」的循环
从分钟级压到秒级。**它只回答「选不选得中」，不替代整步验收**（AGENTS #3）：
URL 规则的 ``{{key}}`` 求值、JS 规则的全局对象都不在它手上。

两条拉起路径与调试同一套（``core.jvm_daemon``）：
优先常驻 daemon（op=rule_try，秒级）；daemon 不可用回落一次性 Gradle（先编译，
一定跑得对，但慢）。执行体与协议见 Kotlin 侧 ``RuleTry.kt`` / ``AppServiceDaemon.kt``。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import uuid
from typing import Any, Dict, Optional

from core.jvm_daemon import DaemonError, ensure, _exchange

#: 页面 HTML / 源 JSON 的临时文件放这（daemon 从文件读：几百 KB 的正文不该挤一行 JSON）。
#: 单独一个子目录：`app_probe/runs/` 归批量运行的目录，两边混放会让「运行目录保留策略」
#: 的 glob 多认一堆临时文件。
_TRY_DIR = ("app_probe", "rule_try")

#: 一次 rule_try 的预算（秒）。daemon 路径是纯内存解析，几秒足够；回落 Gradle
#: 那次是**先编译再跑**，按分钟计——两种路径的超时在 run() 里分开给。
_DAEMON_TIMEOUT_SEC = 30
_GRADLE_TIMEOUT_MIN = 10

#: 原因码（消费侧按码取词，前端文案在 `debugMaterial.js` 的 `UNFAITHFUL_TEXT`）。
UNFAITHFUL_RULE_EMPTY = "rule_empty"
UNFAITHFUL_MATERIAL_IS_SCRIPT = "material_is_script"
UNFAITHFUL_RESULT_ABSENT = "result_absent"
#: 页面脚本要验得动，得先有**页面变量的形状**（`page_vars`，侧车采集）：老侧车 / 设备
#: 通道没采到它时，注进 `params` 的是空对象，脚本必然取不到东西——那结论是假的。
UNFAITHFUL_PAGE_VARS_ABSENT = "page_vars_absent"
UNFAITHFUL_CODES = (UNFAITHFUL_RULE_EMPTY, UNFAITHFUL_MATERIAL_IS_SCRIPT,
                    UNFAITHFUL_RESULT_ABSENT, UNFAITHFUL_PAGE_VARS_ABSENT)


def rule_shape(rule: str) -> str:
    """规则形态：`container` / `selector` / `js_result`（`@js:` 且读 `result`）/
    `js_other` / `empty`。

    前端 `debugMaterial.js` 的 `ruleShape` 与这份同构——它只为显隐按钮，判据在后端。
    """
    text = str(rule or "").strip()
    if not text:
        return "empty"
    if text.startswith("@js:"):
        return "js_result" if "result" in text else "js_other"
    # 列表容器规则（`class.list@tag.a`）：末段仍是选择器，不是取值；与 `getElements` 同一口径
    if re.search(r"@(tag|class|id)\.", text) and not re.search(r"@(text|href|src|textNodes|ownText|html)$", text):
        return "container"
    return "selector"


def unfaithful_reason(material_kind: str, rule: str) -> str:
    """这条规则快验答得**忠实**吗：返回原因码，空串 = 可以跑。

    判据 = 「这种材料能不能验这种形态的规则」（`core.material.can_fast_verify`）。
    两档实测：材料是**页面脚本的输出**而规则是选择器 → 解的是 URL 列表；材料是 **DOM**
    而规则读 `result` → `result` 不是脚本产出，跑的不是引擎里那条。
    快验**只有否决权**：忠实才给结论，通过也不升格为可用建议（验收仍看重调本步）。
    """
    from core.material import can_fast_verify

    shape = rule_shape(rule)
    if shape == "empty":
        return UNFAITHFUL_RULE_EMPTY
    if can_fast_verify(material_kind, shape):
        return ""
    return (UNFAITHFUL_RESULT_ABSENT if shape == "js_result"
            else UNFAITHFUL_MATERIAL_IS_SCRIPT)


def refusal_result(reason_code: str) -> Dict[str, Any]:
    """快验「答不了」的返回体：**没跑引擎**。消费侧按码取词（AGENTS #24④）。"""
    return {"ok": False, "unfaithful": True, "reason_code": reason_code,
            "count": 0, "values": [], "error": ""}


def refusal_code(step: str, field: str, material_kind: str, rule: str,
                 runtime: Optional[Dict[str, Any]] = None) -> str:
    """这条快验**跑不跑得了**：返回原因码，空串 = 可以跑。**不跑引擎、不发请求。**

    两半判据都收在这里（原先 HTTP 端点与 `agent_loop` 各写一遍，两处还会给出相反结论）：
      - 字段族：页面脚本（`webJs`）跑在页面环境，靠 `page_vars` 合成 `params` 才能验
        （合成在 :func:`webjs_check`）——没有那份事实就验不了；
      - 材料 × 规则形态：交给 :func:`unfaithful_reason`。
    """
    from core.autofill import field_kind

    if field and field_kind(step, field) == "script":
        return "" if (runtime or {}).get("page_vars") else UNFAITHFUL_PAGE_VARS_ABSENT
    return unfaithful_reason(material_kind, rule)


def rule_try(source: Dict[str, Any], step: str, body: str, base_url: str,
             rule: str, daemon_info: Optional[Dict[str, Any]] = None,
             ) -> Dict[str, Any]:
    """跑一条规则。返回引擎的 ``{ok, values, count, error}``（形状闸门后）。

    ``daemon_info`` 给了就直接用那个端口（调用方已经 ensure 过）；没给就自己
    ensure 一次。daemon 走不通时**回落 Gradle 一次性跑**（``--try`` 分派），
    结果同形。任何路径的失败都收敛到 ``{"ok": False, "error": …}``——不抛给
    HTTP 层（原因要走到用户眼前，但不许 500）。
    """
    body_path = _write_body(body)
    src_path = _write_source(source)
    if daemon_info is None:
        try:
            daemon_info = ensure()
        except DaemonError as e:
            return _gradle_fallback(src_path, body_path, step, rule, base_url,
                                    "常驻引擎没就绪（%s）" % e)
    try:
        payload = {
            "id": int(uuid.uuid4().int % 1_000_000_000),
            "op": "rule_try",
            "file": src_path,
            "body_path": body_path,
            "step": str(step or ""),
            "rule": str(rule or ""),
            "base_url": str(base_url or ""),
        }
        r = _exchange(payload, int(daemon_info["port"]), _DAEMON_TIMEOUT_SEC)
    except DaemonError as e:
        return _gradle_fallback(src_path, body_path, step, rule, base_url,
                                "常驻引擎没应答（%s）" % e)
    finally:
        _cleanup(body_path, src_path)
    result = _gate(r.get("result"))
    if result is None:
        # daemon 跑了但应答形状不对：不是「规则失败」，是我们自己的协议问题——
        # 如实说，不回落（回落重跑大概率同样形状，白付一次编译）
        return {"ok": False, "values": [], "count": 0,
                "error": "引擎应答形状不对，轻量验证没跑成", "via": "daemon"}
    result["via"] = "daemon"
    return result


def _gradle_fallback(src_path: str, body_path: str, step: str, rule: str,
                     base_url: str, note: str) -> Dict[str, Any]:
    """一次性 Gradle 路径（`--try` 分派）：先编译再跑，慢但一定跑得对。

    参数文件写独立路径（不碰批量校验共用的 args.properties——那会被正在跑的
    批量校验读到，两边的参数互相污染），launcher 显式指过去；文件格式由
    ``jvm_debug._write_args`` 统一写，这里只交 ``extra`` 键。
    """
    import subprocess

    from core import jvm_debug

    out_path = str(pathlib.Path(src_path).with_suffix(".try.json"))
    # ↑ 依赖 src_path 恰好是「单后缀 .json」的文件名约定（_write_source 造的）：
    # with_suffix 会把 .json 整个换掉。src_path 形状变了这里要跟着改（契约测试
    # 钉着 _write_source 的命名）。
    args_path = pathlib.Path(_try_dir()) / ("rule-try-%s.properties" % uuid.uuid4().hex[:12])
    jvm_debug._write_args(
        src_path, "try", out_path, timeout=60, args_path=args_path,
        extra=[("try_body", pathlib.Path(body_path).as_posix()),
               ("try_step", step),
               ("try_rule", rule),
               ("try_base", base_url)])
    try:
        env = {**os.environ, "LEGADO_APPSERVICE_ARGS": str(args_path)}
        p = subprocess.run(
            ["cmd", "/c", str(jvm_debug.LAUNCHER), ":app:testAppDebugUnitTest",
             "--tests", "io.legado.app.service.DebugServiceLauncher", "--rerun"],
            cwd=str(jvm_debug.AGSVC), env=env, capture_output=True,
            text=True, errors="replace", timeout=_GRADLE_TIMEOUT_MIN * 60)
        if p.returncode != 0:
            return {"ok": False, "values": [], "count": 0,
                    "error": "%s；慢速方式也没跑成（exit %s）" % (note, p.returncode),
                    "via": "gradle"}
        result = _gate(json.loads(pathlib.Path(out_path).read_text(encoding="utf-8")))
        if result is None:
            return {"ok": False, "values": [], "count": 0,
                    "error": note + "；引擎应答形状不对", "via": "gradle"}
        result["via"] = "gradle"
        return result
    except Exception as e:
        return {"ok": False, "values": [], "count": 0,
                "error": "%s；慢速方式失败：%s: %s" % (note, type(e).__name__, e),
                "via": "gradle"}
    finally:
        _cleanup(body_path, src_path)
        pathlib.Path(out_path).unlink(missing_ok=True)
        args_path.unlink(missing_ok=True)


#: 页面脚本快验里合成的数组最多塞几条。脚本对 `params.X` 的动作是「读、遍历、拼接」，
#: 条数不影响「键对不对 / 语法通不通」的结论；上限只为防页面给出的 `n` 离谱时撑爆内存。
_SYNTH_ITEMS_MAX = 10


def synth_params(page_vars: Dict[str, Any]) -> Dict[str, Any]:
    """`page_vars`（类型/条数/首项样例）→ 一个能注入 JS 的 `params` 对象。

    数组按 `min(n, 10)` 复制首项：**`length` 与内容自洽**（不虚报长度，否则脚本会崩在
    一个不存在的下标上，那种错误是假的）。字符串/数字/布尔用样例值。
    """
    out: Dict[str, Any] = {}
    for name, rec in (page_vars or {}).items():
        if not isinstance(rec, dict):
            continue
        t = str(rec.get("type") or "")
        first = rec.get("first")
        n = rec.get("n")
        if t == "[object Array]":
            count = n if isinstance(n, int) and not isinstance(n, bool) else 1
            out[name] = [str(first or "")] * max(0, min(count, _SYNTH_ITEMS_MAX))
        elif t == "[object Boolean]":
            out[name] = str(first).strip().lower() == "true"
        elif t == "[object Number]":
            text = str(first or "0")
            try:
                out[name] = float(text) if "." in text else int(text)
            except ValueError:
                out[name] = 0
        elif t == "[object String]":
            out[name] = str(first or "")
        elif t == "[object Object]":
            out[name] = {}
    return out


def webjs_check(source: Dict[str, Any], step: str, body: str, base_url: str, script: str,
                page_vars: Dict[str, Any]) -> Dict[str, Any]:
    """快验一段 webJs：把页面变量**合成**成 `params` 注入规则环境跑一遍。

    为什么能这样验：`webJs` 在引擎里跑在页面环境（读页面变量），而**页面变量的形状**
    在渲染那次已经采到了（`page_vars`）。把形状还原成 `params` 注入后，脚本照跑——
    实测 0.03–0.15s，而重调整步要 30–60s（2026-10-23）。

    包装成 `(function(){ var result; …; return result; })()` 的两个理由（都实测过）：
      - 顶层 `return`（`if (!x) return '';`）在裸规则里是语法错，在函数体里合法；
      - 裸规则里脚本没给 `result` 赋值时，`@js:` 会返回**整页 HTML**（假通过）；
        函数体里返回 `undefined`，落成 `fail` 才是事实。

    **它只回答「能不能跑通」，不回答「取到的内容对不对」**——那是重调整步的事（D1）。
    """
    # 注入分两段，都靠 JSON 字符串字面量语义，不做手工转义：
    #   - page_vars：先 dumps 成 JSON 文本，再 dumps 一次成它的 JS 字符串字面量，
    #     交给 JSON.parse 还原
    #   - 脚本本身：dumps 一次成 JS 字符串字面量，交给 eval
    # 关键：**各只转一次**。多套一层会得到双重转义，Rhino 报
    # `unterminated string literal`（2026-10-23；真实材料带转义字符时才暴露）。
    params_literal = json.dumps(
        json.dumps(synth_params(page_vars), ensure_ascii=False), ensure_ascii=False)
    # 脚本字面量要**多转一层反斜杠**：整条 `@js:` 文本在到达 Rhino 前会经过一次
    # JSON 传输（payload 的 rule 字段），那层会把两个反斜杠还原成一个。不补偿时
    # `join('\n')` 到引擎里变成真实换行，报 `unterminated string literal`
    # （2026-10-23 实测：`String.fromCharCode(10)` 能过、含反斜杠的写法不能）。
    script_literal = json.dumps(str(script or ""), ensure_ascii=False).replace("\\\\", "\\\\\\\\")
    inject = "var params=JSON.parse(%s);" % params_literal
    # 用 `new Function` 而不是 `eval`：`eval` 里顶层 `return` 是语法错（有些 webJs 写
    # `if (!x) return '';`），而 `new Function` 的 body 是函数体，`return` 合法，
    # 且 `result` 作为返回值拿出——与上游 `webJs` 的语义一致。
    rule = ("@js:" + inject + " (new Function('var result; ' + %s + '; return result;'))()"
            % script_literal)
    result = rule_try(source, step, body, base_url, rule)
    result["synthetic"] = True       # 产出基于合成快照：语法验证，不是内容验证
    return result


def fast_verify(source: Dict[str, Any], step: str, field: str, body: str, base_url: str,
                rule: str, material_kind: str,
                runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """快验的**唯一执行入口**：HTTP `/rule-try` 与 `agent_loop._try` 都调它。

    调用方应先问 :func:`refusal_code`（那里不跑引擎，因此不受试次预算约束），拿到空串
    再来这里。页面脚本那一路**在入口再判一次**同一道闸：少一个 `page_vars` 就退回
    「答不了」，不拿空 `params` 跑出一句假的「取不到」（AGENTS #22②：闸口放边界）。
    """
    from core.autofill import field_kind

    if field and field_kind(step, field) == "script":
        page_vars = (runtime or {}).get("page_vars") or {}
        if not page_vars:
            return refusal_result(UNFAITHFUL_PAGE_VARS_ABSENT)
        return webjs_check(source, step, body, base_url, rule, page_vars)
    why = unfaithful_reason(material_kind, rule)
    if why:
        return refusal_result(why)
    return rule_try(source, step, body, base_url, rule)


def _write_body(body: str) -> str:
    p = pathlib.Path(_try_dir())
    p.mkdir(parents=True, exist_ok=True)
    f = p / ("rule-try-body-%s.html" % uuid.uuid4().hex[:12])
    f.write_text(body or "", encoding="utf-8", newline="\n")
    return str(f)


def _write_source(source: Dict[str, Any]) -> str:
    p = pathlib.Path(_try_dir())
    p.mkdir(parents=True, exist_ok=True)
    f = p / ("rule-try-src-%s.json" % uuid.uuid4().hex[:12])
    f.write_text(json.dumps([source], ensure_ascii=False), encoding="utf-8",
                 newline="\n")
    return str(f)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            pathlib.Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def _try_dir() -> pathlib.Path:
    from core.paths import data_path
    return data_path(*_TRY_DIR)


def _gate(raw: Any) -> Optional[Dict[str, Any]]:
    """应答形状闸门（AGENTS #22②）：不合法返回 None，调用方如实上报，不静默。"""
    if not isinstance(raw, dict):
        return None
    values = raw.get("values")
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None
    ok = raw.get("ok")
    if not isinstance(ok, bool):
        return None
    return {"ok": ok,
            "values": [v[:200] for v in values[:5]],
            "count": int(raw.get("count") or 0) if isinstance(raw.get("count"), (int, float)) else 0,
            "error": str(raw.get("error") or "")[:200]}

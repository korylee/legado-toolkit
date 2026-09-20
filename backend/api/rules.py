# -*- coding: utf-8 -*-
"""规则试跑接口：按完整链路回放 source。"""

import asyncio

from fastapi import APIRouter, HTTPException

from backend.schemas import (
    AppDebugRequest,
    AppHostRequest,
    JvmDebugRequest,
    ReplayStepRequest,
    RuleChainTest,
    SuggestRuleRequest,
)

router = APIRouter()


@router.post("/chain")
async def chain_test(body: RuleChainTest):
    from core.verify import verify_chain

    source = dict(body.source or {})
    if not source.get("bookSourceUrl"):
        raise HTTPException(400, "缺少 bookSourceUrl")
    try:
        return await asyncio.to_thread(
            verify_chain, source, body.keyword or "我",
            body.detail_url or "", int(body.pick or 1),
        )
    except Exception as e:
        raise HTTPException(400, "试跑失败: %s: %s" % (type(e).__name__, e))


@router.post("/jvm-debug")
async def jvm_debug(body: JvmDebugRequest):
    """在本机引擎里跑一次调试（S5-A4）：**不填 IP、不推送**，直接出分段结果。

    返回体与 ``/app-debug`` **同形状**（source/steps/pages/all_ok/events/error），
    前端抽屉与卡片零改动——`source` 是 ``"jvm"``，抽屉据此标「本机引擎」而不是
    「App 实测」。

    不排队：跑批与调试共用 `appservice/args.properties`，同时跑会互相踩——锁与那句
    提示都在 `core.jvm_debug`（拿不到就返回「另一个任务在跑」，错误体而不是 500，
    用户侧等一下就能自己解决）。
    """
    from core.fetch import CACHE_MODES
    from core.jvm_debug import run_jvm_debug

    # 与 /app-debug 同一条纪律：枚举严格比，**不静默退回默认**——用户选了
    # 「忽略缓存重抓」却因为拼错而每次都联网，界面上分辨不出来
    cache = str(body.cache or "")
    if cache not in CACHE_MODES:
        raise HTTPException(400, "未知的缓存策略：%s（只能是 %s）"
                                 % (cache, " / ".join(CACHE_MODES)))
    if int(body.timeout or 0) <= 0:
        raise HTTPException(400, "timeout 必须是正数")
    # 同步阻塞（默认常驻 daemon，回落时才拉 Gradle），必须让出事件循环
    return await asyncio.to_thread(
        run_jvm_debug, dict(body.source or {}), body.key or "我",
        int(body.timeout or 60), body.cookie or "", cache,
    )


@router.post("/app-debug")
async def app_debug(body: AppDebugRequest):
    """连 App 跑一次调试：借阅读 App 的调试 WebSocket 走完整链路（含 JS 规则）。

    返回体与 ``/rules/chain`` **同形状**（steps / pages），前端抽屉与卡片零改动。

    `tag` **必须用 ``bookSourceUrl`` 的导入原文**：App 的
    ``getBookSource(tag)?.let{}`` 查不到源就什么都不做——表现为静默无响应，
    排查成本极高。详情接口返回的对象里这个字段就是原文，所以这里不做任何
    规范化（尤其不要 ``rstrip("/")`` / ``lower()``）。
    """
    from core.app_debug import run_app_debug
    from core.fetch import CACHE_MODES

    source = dict(body.source or {})
    tag = str(source.get("bookSourceUrl", "") or "").strip()
    if not tag:
        raise HTTPException(400, "缺少 bookSourceUrl")
    host = str(body.host or "").strip()
    if not host:
        raise HTTPException(400, "缺少 App 的 IP（App 通知栏里有）")
    # 严格按枚举比，**不做大小写/空白归一**：这个值来自我们自己的前端，
    # 对不上就是 bug，宽松一点只会让枚举多出第二份（更松的）定义。取值不合法
    # 一律 400，**不退回默认**——用户选了「只补解析不重抓」却因为拼错而每次都在
    # 联网，界面上分辨不出来
    cache = str(body.cache or "")
    if cache not in CACHE_MODES:
        raise HTTPException(400, "未知的缓存策略：%s（只能是 %s）"
                                 % (body.cache, " / ".join(CACHE_MODES)))
    if body.push:
        # 调试 WS 的 tag 是拿去 App 库里精确匹配的，库里没有这个源就静默无响应。
        # 先推一次（App 侧是 REPLACE，幂等），新源/改过还没保存的源就都能调试了。
        from core.app_debug import push_source

        ok, err = await asyncio.to_thread(push_source, host, source, body.port or None)
        if not ok:
            return {"error": "推送到 App 失败：%s" % (err or "未知原因")}
    try:
        # run_app_debug 是同步阻塞的（标准库 socket 收发），必须让出事件循环。
        # 连不上/超时它自己会返回带 error 的结果体（不抛），
        # 这里兜的是脏端口之类的入参异常——同样给 400，不给 500。
        return await asyncio.to_thread(
            run_app_debug, host, tag, body.key or "我",
            body.port or None, 60, source, cache=cache,
        )
    except Exception as e:
        raise HTTPException(400, "连 App 调试失败: %s: %s" % (type(e).__name__, e))


@router.post("/app-preflight")
async def app_preflight(body: AppHostRequest):
    """连 App 调试前的预检。

    把以前那个「点完等 60 秒、什么都不发生」拆成能对症的状态：
    连不上 / App 里没有这个源 / App 里是旧版本 / 可以直接调试。

    传**完整 source**（不是只传 URL）：要拿它和 App 里那份比对规则，
    才能发现「App 里有，但是旧版本」——那种情况下直接调试跑的是旧规则，
    结果看着正常、答的却不是你在改的东西。
    """
    from core.app_debug import preflight

    source = dict(body.source or {})
    if not str(source.get("bookSourceUrl", "") or "").strip():
        raise HTTPException(400, "缺少 bookSourceUrl")
    return await asyncio.to_thread(
        preflight, str(body.host or "").strip(), source, body.port or None)


@router.post("/app-push")
async def app_push(body: AppHostRequest):
    """把源推送到 App（幂等）。

    **会改动用户 App 里的书源数据**——新增或覆盖同 URL 的源。所以只由用户
    显式点击触发，任何流程都不得自动调用。失败不抛 400，把原因放在 body 里，
    前端好就地提示。
    """
    from core.app_debug import push_source

    source = dict(body.source or {})
    host = str(body.host or "").strip()
    if not host:
        return {"ok": False, "error": "没有填 App 的 IP"}
    ok, err = await asyncio.to_thread(push_source, host, source, body.port or None)
    return {"ok": ok, "error": err}


@router.post("/suggest-rule")
async def suggest_rule(body: SuggestRuleRequest):
    """让 AI 给某一步提候选规则。**只提议，每条都要过本地回放器**（AGENTS #3）。

    **这是个花钱的动作，只能由用户显式触发**（同 `/app-push` 那条边界）：
    前端只在一颗按钮的点击回调里调它，任何流程都不得自动调用。
    免费的那部分（`dry_run=true`：程序先挑一遍 + 登录墙判断）不在此列，
    它不发任何模型请求，可以在换步骤时自动跑。

    「没配模型」「模型输出不是 JSON」都**不是 HTTP 错误**：结果体里带 `llm`
    三态（ok / off / error / dry_run）与 `error` 文案，前端就地说明。做成 4xx/5xx
    只会让抽屉里冒出一个没有上下文的红 toast，而这几种情况用户都能自己处理。
    """
    from core.repair.suggest import suggest

    if not (body.html or "").strip():
        # 没页面就没有可分析的东西，这是调用方的错（抽屉该把按钮禁掉）
        raise HTTPException(400, "这一步没有页面 HTML，先抓到页面再让 AI 提议")
    try:
        return await suggest({
            "html": body.html, "step": body.step, "rule": body.rule,
            "step_label": body.step_label, "want_label": body.want_label,
            "field": body.field, "focus": body.focus, "source_type": body.source_type,
            "replay_note": body.replay_note, "app_values": body.app_values,
            "diagnosis": body.diagnosis, "candidates": body.candidates,
            "enabled_cookie_jar": body.enabled_cookie_jar,
        }, dry_run=body.dry_run)
    except Exception as e:
        raise HTTPException(400, "AI 提议失败: %s: %s" % (type(e).__name__, e))


@router.post("/replay-step")
async def replay_rule_step(body: ReplayStepRequest):
    """用已抓到的 HTML 重放一步规则，**不发网络请求**。

    改完规则想立刻看判定变化时用它：试跑结果里已经存了每页 HTML，
    不必重跑整条链（那要重新联网搜索）。
    """
    from core.verify import replay_step

    try:
        return await asyncio.to_thread(
            replay_step, body.html, body.rule, body.step, body.source_type)
    except Exception as e:
        raise HTTPException(400, "重放失败: %s: %s" % (type(e).__name__, e))

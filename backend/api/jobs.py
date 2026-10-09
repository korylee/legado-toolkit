# -*- coding: utf-8 -*-
import json
import time
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.deps import get_store
from backend.jobs import runner
from backend.schemas import JobCreate
from core.store import ACTIVE_JOB_STATUSES, JOBS_TTL_DAYS

router = APIRouter()

#: 任务详情只返回有界的块级摘要；完整执行记录仍保留在结果/运行目录中。
_JOB_DETAIL_CHUNK_LIMIT = 250
_JOB_DETAIL_LOG_LIMIT = 4000
_JOB_DETAIL_TEXT_LIMIT = 1200


def _bounded_text(value: Any, limit: int = _JOB_DETAIL_TEXT_LIMIT) -> str:
    """有界文本（**保头**）：原因/定性类字段的话要点在开头，超长截掉尾部。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _tail_text(value: Any, limit: int) -> str:
    """有界文本（**保尾**）：日志的报错在末尾，超长截掉头部。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _chunk_detail_reports(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """从 JVM 跑批结果里挑出详情面板实际消费的有限字段。"""
    reports = parsed.get("chunk_reports")
    if not isinstance(reports, list):
        return {"chunk_reports": [], "chunk_report_total": 0,
                "chunk_reports_truncated": False}

    projected = []
    for report in reports[:_JOB_DETAIL_CHUNK_LIMIT]:
        if not isinstance(report, dict):
            continue
        item: Dict[str, Any] = {
            "index": report.get("index") if isinstance(report.get("index"), int) else None,
            "ok": report.get("ok") if isinstance(report.get("ok"), bool) else None,
            "count": report.get("count") if isinstance(report.get("count"), (int, float)) else None,
            "resumed": report.get("resumed") if isinstance(report.get("resumed"), bool) else None,
            "execution_mode": _bounded_text(report.get("execution_mode"), 80),
            "cost_sec": report.get("cost_sec") if isinstance(report.get("cost_sec"), (int, float)) else None,
            "daemon_failure": _bounded_text(report.get("daemon_failure")),
            "reason": _bounded_text(report.get("reason")),
            "exit": report.get("exit") if isinstance(report.get("exit"), int) else None,
        }
        gradle = report.get("gradle")
        if isinstance(gradle, dict):
            item["gradle"] = {
                "exit": gradle.get("exit") if isinstance(gradle.get("exit"), int) else None,
                "stdout": _tail_text(gradle.get("stdout"), _JOB_DETAIL_LOG_LIMIT),
                "stderr": _tail_text(gradle.get("stderr"), _JOB_DETAIL_LOG_LIMIT),
            }
        projected.append(item)
    return {
        "chunk_reports": projected,
        "chunk_report_total": len(reports),
        "chunk_reports_truncated": len(reports) > _JOB_DETAIL_CHUNK_LIMIT,
    }


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _check_summary(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验类结果的摘要（**唯一一份**）：详情面板与任务列表共用。

    两处各写一遍时，列表里的「通过 / 未通过」和详情里的会对不上，而读者无从判断
    该信哪一份。**什么算校验结果也由这里回答**（标志字段 ``checked`` 是不是数字）——
    让调用方各判一次，就是同一件事长出两份判据。不是校验结果给 ``None``。
    """
    if not isinstance(parsed.get("checked"), (int, float)):
        return None
    transitions = parsed.get("transitions") or {}
    if not isinstance(transitions, dict):
        transitions = {}
    changed = transitions.get("changed") or {}
    if not isinstance(changed, dict):
        changed = {}
    changed_items = transitions.get("changed_items") or []
    if not isinstance(changed_items, list):
        changed_items = []
    dist = parsed.get("dist") if isinstance(parsed.get("dist"), dict) else {}
    checked = _num(parsed.get("checked"))
    ok = _num(dist.get("ok"))
    return {
        "checked": parsed.get("checked", 0),
        #: 通过 / 未通过在这里算好：详情与列表读同一份。让前端各减一次，
        #: 「未通过」就会有两个可能的算法（checked-ok 还是 dist 里其余桶之和）。
        "ok": int(ok),
        "fail": int(max(checked - ok, 0)),
        "cached": parsed.get("cached", 0),
        "fetched": parsed.get("fetched", 0),
        "first_checked": transitions.get("first_checked", 0),
        "dist": dist,
        "changed": changed,
        "changed_total": sum(v for v in changed.values() if isinstance(v, (int, float))),
        "changed_items": changed_items,
        "warnings": ([
            str(parsed["save_failures"]) + " 条结果没能写入管理库，列表状态不会更新"
        ] if parsed.get("save_failures") else []) + ([
            str(parsed["hit_downgrades"]) + " 个源无法核对命中状态，暂按「命中」计"
        ] if parsed.get("hit_downgrades") else []),
    }


def _load_result(job: Dict[str, Any]) -> Any:
    """把任务行里的 ``result_json`` 解成对象；解不出来给 ``None``。

    「解不出来」不是「没有结果」——详情与列表都要能说清是哪一种，所以坏 JSON
    单独走 ``_result_error``，不在这里静默成「没有结果」。
    """
    raw = job.get("result_json") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _error_text(error: Any, ok_flag: Any, reason: Any) -> str:
    """「为什么看不到结果」的文本（**唯一一份**：轻量行与详情共用）。

    ``error`` 是异常出口，``reason`` 是中止那条链——只读前者时，一次中止在界面上
    就是「已完成 + 空原因」。``reason`` 只在 ``ok`` 明确为假时才取：成功的结果里
    它可能只是过程说明（``ok_flag`` 来自 ``json_extract``，JSON 的 false 是 0）。
    """
    if error:
        return _bounded_text(error)
    if ok_flag in (0, False) and reason:
        return _bounded_text(reason)
    return ""


def _result_error(job: Dict[str, Any], parsed: Any) -> str:
    """任务失败的原因（详情侧）：坏 JSON 单独给一句固定说明——它同样是
    「为什么看不到结果」的答案。文本本身的口径在 ``_error_text``。"""
    if (job.get("result_json") or "") and parsed is None:
        return "任务结果不是有效 JSON"
    if isinstance(parsed, dict):
        return _error_text(parsed.get("error"), parsed.get("ok"), parsed.get("reason"))
    return ""


def _changed_total(raw: Any) -> int:
    """``transitions.changed`` 是 ``{档位: 条数}``，要求和。

    ``json_extract`` 交出来的是那个**小对象**的文本（不是整个结果），所以这里解析的
    代价与结果大小无关。
    """
    if not raw:
        return 0
    try:
        changed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return 0
    if not isinstance(changed, dict):
        return 0
    return int(sum(v for v in changed.values() if isinstance(v, (int, float))))


def _job_row(job: Dict[str, Any]) -> Dict[str, Any]:
    """任务**轻量行** → 界面行：结论摘要 + 失败原因，这里也不产出 ``result_json``。

    入参必须是 ``Store`` 的轻量行（``_JOB_LIGHT_COLUMNS``）：那几个数由 SQLite 的
    ``json_extract`` 抽好。**不要为「多拿一点信息」把它换回 ``get_job``**——全量校验
    一条结果上百 KB（``items[:500]``），列表 200 行就是几十 MB 的解析，而这样的列表
    打开一次、每 5 秒刷新一次都要付。
    """
    checked = job.get("result_checked")
    summary = None
    # 判据与 `_check_summary` 一致：`checked` 是**数字**才算校验结果
    # （json_extract 对字符串会原样返回，不能只判「不是 None」）
    if isinstance(checked, (int, float)):
        ok = _num(job.get("result_ok"))
        summary = {
            "checked": int(_num(checked)),
            "ok": int(ok),
            "fail": int(max(_num(checked) - ok, 0)),
            "changed_total": _changed_total(job.get("result_changed")),
        }
    return {
        "id": job.get("id", ""),
        "kind": job.get("kind", ""),
        "status": job.get("status", ""),
        "phase": job.get("phase", ""),
        "progress": job.get("progress", 0),
        "total": job.get("total", 0),
        "retry_of": job.get("retry_of", ""),
        "expires_at": job.get("expires_at", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        #: 失败/部分失败的原因。**要有界**：它可能是一整段 Gradle 报错
        "error": _error_text(job.get("result_error"), job.get("result_ok_flag"),
                             job.get("result_reason")),
        "summary": summary,
    }


def _job_detail(job: Dict[str, Any], raw: bool = False) -> Dict[str, Any]:
    """把数据库任务行转换成前端唯一使用的明细形状。

    **列表与流继续只返回轻量行**；只有用户主动查看（或终态收尾）时才解析结果。
    校验任务默认只下发摘要和变化明细，避免把 ``items[:500]`` 再复制进详情响应；
    其他任务保留结构化结果，方便查看生成/导入任务的实际返回值。

    ``raw=True`` 才把校验的**原始结果体**一并给出：列表页的「就地回填 + 结果条」
    要用 ``items``／``transitions``，而摘要是给界面看的投影、不够用。它是一条
    **一次性的拉取**（点开详情、跑完收尾），不是每拍都来的东西。
    """
    parsed = _load_result(job)

    summary = None
    execution_mode = ""
    execution_note = ""
    daemon_fallback_reason = ""
    error = _result_error(job, parsed)
    if isinstance(parsed, dict):
        execution_mode = str(parsed.get("execution_mode") or "")
        execution_note = str(parsed.get("execution_note") or "")
        daemon_fallback_reason = str(parsed.get("daemon_fallback_reason") or "")
        summary = _check_summary(parsed)

    detail = {
        "id": job.get("id", ""),
        "kind": job.get("kind", ""),
        "retry_of": job.get("retry_of", ""),
        "status": job.get("status", ""),
        "phase": job.get("phase", "queued"),
        "progress": job.get("progress", 0),
        "total": job.get("total", 0),
        "expires_at": job.get("expires_at", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "summary": summary,
        "execution_mode": execution_mode,
        "execution_note": execution_note,
        "daemon_fallback_reason": daemon_fallback_reason,
        "error": error,
        "result": parsed if (summary is None or raw) else None,
    }
    if job.get("kind") == "jvm_run" and isinstance(parsed, dict):
        detail.update(_chunk_detail_reports(parsed))
        prepared = parsed.get("daemon_prepare")
        if isinstance(prepared, dict):
            detail["daemon_prepare"] = {
                "outcome": _bounded_text(prepared.get("outcome"), 80),
                "reason": _bounded_text(prepared.get("reason")),
            }
    return detail


@router.post("", status_code=202)
async def create_job(body: JobCreate):
    try:
        job_id = runner.submit(body.kind, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": job_id, "kind": body.kind, "events": "/api/jobs/%s/stream" % job_id}


#: 状态档位 → 库内状态。**只认档位名**：让前端传 `running` 这类原始字面量，
#: 会让「进行中到底含不含 cancel_requested」在前后端各判一次，两边必然分叉。
#: 「进行中」直接复用 store 的常量——它与列表默认窗口的 OR 分支必须是同一组值。
_STATUS_SCOPES = {
    "active": ACTIVE_JOB_STATUSES,
    "done": ("done",),
    "failed": ("failed",),
    "cancelled": ("cancelled",),
}

#: 任务列表一次最多给多少条。**不暴露成查询参数**：列表要带结论摘要（服务端要解
#: result_json），让 URL 决定一次解多少个大 JSON 没有意义；任务中心本来也不翻页。
_LIST_LIMIT = 200

#: 默认窗口 = 保留期。**必须是同一个数**：`JOBS_TTL_DAYS` 决定「过期即清」，
#: 列表窗口决定「最近多少天算历史」；用两根轴会分叉成「列表里留着已经被清掉的」
#: 或「刚跑完就没了的」。
_RECENT_DAYS = JOBS_TTL_DAYS


@router.get("")
def list_jobs(
    scope: Literal["recent", "all"] = "recent",
    status: Literal["", "active", "done", "failed", "cancelled"] = "",
    kind: str = "",
    exclude_kind: str = "",
    st=Depends(get_store),
):
    """任务列表：筛选 + 每行结论摘要。

    ``scope=recent``（默认）给「进行中 + 最近 ``JOBS_TTL_DAYS`` 天」——任务多起来以后
    全部历史在首屏就是噪音；``scope=all`` 才是全量。筛选口径都在这里决定，前端只传档位名。
    ``exclude_kind`` 给任务中心用：**调试运行（``jvm_debug``）不进这份列表**——
    它由调试页自己观测，一次点击一条会把任务中心刷成流水账。

    每行带 ``summary``（通过 / 未通过 / 检查数 / 相对上次变化），**不带 result_json**：
    列表要能直接看出这次跑得好不好，而上百 KB 的结果不能进列表响应。
    """
    since = ""
    if scope == "recent":
        since = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - _RECENT_DAYS * 86400))
    rows = st.list_jobs(
        limit=_LIST_LIMIT,
        statuses=_STATUS_SCOPES.get(status) or (),
        kind=kind.strip(), recent_since=since, exclude_kind=exclude_kind.strip())
    return {"items": [_job_row(row) for row in rows], "limit": _LIST_LIMIT,
            # 界面把窗口天数写进档位名（「进行中 + 最近 N 天」）：数字从这里走，
            # 别在文案里再抄一份（JOBS_TTL_DAYS 改了标签跟着变）
            "recent_days": (_RECENT_DAYS if scope == "recent" else 0)}


@router.get("/lane")
def lane_status():
    """JVM lane 的排队现状：谁持着 JVM、排队的都有谁（kind/已等秒/有效优先级）。

    回答「我的调试/跑批为什么还没开始」；不塞进任务列表响应，是为了不动
    前端已消费的列表形状。必须注册在 /{job_id} 之前。
    """
    return {"jvm": runner.lane_snapshot("jvm")}


@router.get("/kinds")
def job_kinds():
    # 必须注册在 /{job_id} 之前，否则 kinds 会被当成 job_id 捕获
    return sorted(runner.HANDLERS.keys())


@router.get("/{job_id}/detail")
def get_job_detail(job_id: str, raw: bool = False, st=Depends(get_store)):
    """任务明细：``raw=1`` 时连校验的原始结果体一起给（见 ``_job_detail``）。

    默认不给：那是一份 ``items[:500]``（一条上百 KB），只有"就地回填 + 结果条"
    这种真要用 ``items`` 的调用方才该说这一声。
    """
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _job_detail(job, raw=raw)


@router.get("/{job_id}")
def get_job(job_id: str, st=Depends(get_store)):
    """单条任务的**轻量**状态行（进度/阶段/结论摘要），不含 ``result_json``。

    明细弹窗在任务运行期间每 2 秒拉一次这里——带上结果就是每 2 秒传上百 KB。
    要看结果去 ``/{job_id}/detail``（用户主动点开才算一次，而且只拉一次）。
    """
    row = st.get_job_summary(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return _job_row(row)


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") in ("pending", "running"):
        raise HTTPException(409, "任务仍在运行，不能重试")
    kind = str(job.get("kind") or "")
    if kind == "jvm_run":
        # jvm_run 可以重试：run_jvm_job 会扫描原运行目录里已完成块（DONE 标记）
        # 并跳过，只补失败/未跑的块；目录已被清理（成功或单条取消）时等价于重跑。
        # 注意「正在运行」的拦截在上面已经挡掉了 pending/running。
        pass
    if kind not in runner.HANDLERS:
        raise HTTPException(409, "任务类型当前不可重试：%s" % (kind or "未知"))
    try:
        payload = json.loads(job.get("payload") or "{}")
    except (TypeError, ValueError):
        raise HTTPException(409, "任务参数不是有效 JSON，无法重试")
    if not isinstance(payload, dict):
        raise HTTPException(409, "任务参数不是对象，无法重试")
    try:
        new_id = runner.submit(kind, payload, retry_of=job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"job_id": new_id, "kind": kind, "retry_of": job_id,
            "events": "/api/jobs/%s/stream" % new_id}


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str):
    return {"cancelled": runner.cancel(job_id)}


@router.delete("/{job_id}")
def delete_job(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") in ("pending", "running"):
        raise HTTPException(409, "任务正在运行，请先取消")
    if not st.delete_job(job_id):
        raise HTTPException(409, "任务状态已变化，请刷新后重试")
    return {"deleted": True, "job_id": job_id}


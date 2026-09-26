# -*- coding: utf-8 -*-
import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.deps import get_store
from backend.jobs import runner
from backend.schemas import JobCreate

router = APIRouter()


def _job_detail(job: Dict[str, Any]) -> Dict[str, Any]:
    """把数据库任务行转换成前端唯一使用的明细形状。

    列表和 SSE 继续只返回轻量任务行；只有用户主动查看时才解析结果。
    校验任务只下发摘要和变化明细，避免把 ``items[:500]`` 再复制进详情响应；
    其他任务保留结构化结果，方便查看生成/导入任务的实际返回值。
    """
    parsed = None
    parse_error = ""
    raw = job.get("result_json") or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parse_error = "任务结果不是有效 JSON"

    summary = None
    error = parse_error
    if isinstance(parsed, dict):
        error = str(parsed.get("error") or "")
        if isinstance(parsed.get("checked"), (int, float)):
            transitions = parsed.get("transitions") or {}
            if not isinstance(transitions, dict):
                transitions = {}
            changed = transitions.get("changed") or {}
            if not isinstance(changed, dict):
                changed = {}
            changed_items = transitions.get("changed_items") or []
            if not isinstance(changed_items, list):
                changed_items = []
            summary = {
                "checked": parsed.get("checked", 0),
                "cached": parsed.get("cached", 0),
                "fetched": parsed.get("fetched", 0),
                "first_checked": transitions.get("first_checked", 0),
                "changed": changed,
                "changed_total": sum(v for v in changed.values() if isinstance(v, (int, float))),
                "changed_items": changed_items,
                "warnings": ([
                    str(parsed["save_failures"]) + " 条结果没能写入管理库，列表状态不会更新"
                ] if parsed.get("save_failures") else []) + ([
                    str(parsed["hit_downgrades"]) + " 个源无法核对命中状态，暂按「命中」计"
                ] if parsed.get("hit_downgrades") else []),
            }

    return {
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
        "error": error,
        "result": parsed if summary is None else None,
    }


@router.post("", status_code=202)
async def create_job(body: JobCreate):
    try:
        job_id = runner.submit(body.kind, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": job_id, "kind": body.kind, "events": "/api/jobs/%s/events" % job_id}


@router.get("")
def list_jobs(st=Depends(get_store)):
    # 历史任务列表（store.list_jobs 默认最近 50 条，按创建时间倒序）。
    # 前端「任务」抽屉打开时拉一次，之后仍靠 SSE 订阅在跑的任务。
    return st.list_jobs()


@router.get("/kinds")
def job_kinds():
    # 必须注册在 /{job_id} 之前，否则 kinds 会被当成 job_id 捕获
    return sorted(runner.HANDLERS.keys())


@router.get("/{job_id}/detail")
def get_job_detail(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _job_detail(job)


@router.get("/{job_id}")
def get_job(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") in ("pending", "running"):
        raise HTTPException(409, "任务仍在运行，不能重试")
    kind = str(job.get("kind") or "")
    if kind == "jvm_run":
        # JVM 任务的 payload 指向一次性 run_dir，任务结束时目录已经清理，
        # 不能把旧路径原样交给下一次执行。
        raise HTTPException(409, "JVM 任务不能直接重试，请从校验入口重新提交")
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
            "events": "/api/jobs/%s/events" % new_id}


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


@router.get("/{job_id}/events")
async def job_events(job_id: str):
    # SSE：只推变化，任务结束即关闭
    def frame(data: dict) -> str:
        return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    async def gen():
        last = None
        # Store **建一次**就用整条流。放进循环里的话，每 0.5 秒要重跑一遍
        # `Store.__init__`——建连接 + PRAGMA + `_init_schema()`（CREATE TABLE
        # IF NOT EXISTS 外加逐列 ALTER 补列），为一个 `SELECT` 付一次建库的价。
        # 客户端断开时生成器被关，finally 里照常收连接
        st = runner.Store()
        try:
            while True:
                job = st.get_job(job_id)
                if not job:
                    yield frame({"error": "任务不存在"})
                    return
                cur = (job.get("status"), job.get("phase"), job.get("progress"))
                if cur != last:
                    yield frame(job)
                    last = cur
                if job.get("status") in ("done", "failed", "cancelled"):
                    return
                await asyncio.sleep(0.5)
        finally:
            st.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

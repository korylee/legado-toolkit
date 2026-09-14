# -*- coding: utf-8 -*-
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.deps import get_store
from backend.jobs import runner
from backend.schemas import JobCreate

router = APIRouter()


@router.post("", status_code=202)
async def create_job(body: JobCreate):
    try:
        job_id = runner.submit(body.kind, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": job_id, "kind": body.kind, "events": "/api/jobs/%s/events" % job_id}


@router.get("/kinds")
def job_kinds():
    # 必须注册在 /{job_id} 之前，否则 kinds 会被当成 job_id 捕获
    return sorted(runner.HANDLERS.keys())


@router.get("/{job_id}")
def get_job(job_id: str, st=Depends(get_store)):
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str):
    return {"cancelled": runner.cancel(job_id)}


@router.get("/{job_id}/events")
async def job_events(job_id: str):
    # SSE：只推变化，任务结束即关闭
    def frame(data: dict) -> str:
        return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    async def gen():
        last = None
        while True:
            st = runner.Store()
            try:
                job = st.get_job(job_id)
            finally:
                st.close()
            if not job:
                yield frame({"error": "任务不存在"})
                return
            cur = (job.get("status"), job.get("progress"))
            if cur != last:
                yield frame(job)
                last = cur
            if job.get("status") in ("done", "failed", "cancelled"):
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

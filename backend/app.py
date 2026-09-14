# -*- coding: utf-8 -*-
# Legado 书源管理后端。
#
# 启动：
#   .venv/Scripts/python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8787
#
# 约定：所有耗时操作都走 /api/jobs，不要新增同步的慢接口。
from fastapi import FastAPI

from backend.api import jobs, ops, sources
from core.store import Store

app = FastAPI(title="Legado 书源管理", version="0.1.0",
              description="候选主库 + 校验缓存 + 失效归因 + AI 修复")

app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])

# 导入即注册 ops 里的 job handler
_ = ops


@app.get("/api/health")
def health():
    with Store(readonly=True) as st:
        return st.stats()



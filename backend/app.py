# -*- coding: utf-8 -*-
# Legado 书源管理后端。
#
# 启动：
#   .venv/Scripts/python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8787
#
# 约定：所有耗时操作都走 /api/jobs，不要新增同步的慢接口。
import os
import socket

from fastapi import FastAPI

from backend.api import export, feed, imports, jobs, llm, ops, rules, sources
from core.store import Store

app = FastAPI(title="Legado 书源管理", version="0.1.0",
              description="候选主库 + 校验缓存 + 失效归因 + AI 修复")

app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(feed.router, prefix="/api/feed", tags=["feed"])
app.include_router(imports.router, prefix="/api/import", tags=["import"])
app.include_router(rules.router, prefix="/api/rules", tags=["rules"])
app.include_router(llm.router, prefix="/api/llm", tags=["llm"])

# 导入即注册 ops 里的 job handler
_ = ops


@app.get("/api/health")
def health():
    with Store(readonly=True) as st:
        return st.stats()


@app.get("/api/net")
def net_info():
    # 前端要拿它拼「手机能访问的地址」：localhost / 127.0.0.1 手机是访问不到的
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # 不发包，只让内核选出出口网卡
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return {"ips": ips, "hostname": socket.gethostname(),
            "port": int(os.getenv("LEGADO_PORT", "8787"))}

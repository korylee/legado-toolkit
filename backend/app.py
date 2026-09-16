# -*- coding: utf-8 -*-
# Legado 书源管理后端。
#
# 启动：
#   .venv/Scripts/python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8787
#
# 约定：所有耗时操作都走 /api/jobs，不要新增同步的慢接口。
import mimetypes
import pathlib
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import netinfo
from backend.api import (export, feed, imports, jobs, llm, ops, rules,
                         settings, sources)
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
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

# 导入即注册 ops 里的 job handler
_ = ops


@app.get("/api/health")
def health():
    with Store(readonly=True) as st:
        return st.stats()


@app.get("/api/net")
def net_info():
    # 前端要拿它拼「手机能访问的地址」：localhost / 127.0.0.1 手机是访问不到的。
    # 探测逻辑与启动日志共用一份，见 backend/netinfo.py
    return {"ips": netinfo.local_ips(), "hostname": netinfo.hostname(),
            "port": netinfo.port()}


# ---------------------------------------------------------------- 前端静态托管
# 构建一次之后，界面与 API 由同一个端口提供（`pnpm build` 完只起后端即可）。
#
# 这不是新增一条路：vite.config.js 写着「生产时前端由后端托管（同源）」，
# router/index.js 写着「用 hash 模式：后端静态托管时不用配 rewrite 规则」——
# 这两处前提本来就是为这里准备的。hash 路由下浏览器只会请求 `/` 与
# `/assets/*`，所以 `html=True` 足够，**不需要 SPA fallback**。
#
# 路径从 __file__ 推，不看 CWD：本文件允许从任意目录起
# （见顶上「启动」注释里的 `uvicorn backend.app:app` 形式）
WEB_DIST = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "dist"


def _cache_control_for(full_path: Any) -> str:
    """静态产物该带什么 Cache-Control。空串 = 不加这个头。

    `index.html` **必须** no-store：Vite 的产物名带内容哈希，重建之后旧
    index.html 引用的那些文件已经不存在了，浏览器拿缓存里的旧页面去要资源就是
    404 + 白屏。而「没写响应头」时浏览器对 HTML 的启发式缓存是**默认行为**，
    不显式禁止就迟早撞上。反过来 `/assets/*` 的名字随内容变，缓存越久越省事。

    分隔符要归一：starlette 的 `get_path` 是拿 `os.path.join` 拼的，Windows 上
    传进来的是 `assets\\index-xxx.js`，直接按 `"assets/"` 前缀判会整个漏掉。
    """
    name = str(full_path).replace("\\", "/")
    if name.endswith(".html"):
        return "no-store"
    if "/assets/" in name:
        return "public, max-age=31536000, immutable"
    return ""


class _UiFiles(StaticFiles):
    """前端产物。只做一件事：按文件类型补 Cache-Control（理由见上）。"""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        cc = _cache_control_for(full_path)
        if cc:
            resp.headers["Cache-Control"] = cc
        return resp


def _ui_stale_hint() -> str:
    """dist 落后于源码时的提醒；没有落后就返回空串。

    **别静默**：改了 .vue、重启后端、看到的还是旧界面——遇到的人只会怀疑
    「改动没生效」或「热重载坏了」，而真实原因是没重新构建。
    """
    web = WEB_DIST.parent
    try:
        newest = max((p.stat().st_mtime for p in (web / "src").rglob("*")
                      if p.is_file()), default=0.0)
        for extra in ("index.html", "vite.config.js", "package.json"):
            f = web / extra
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        built = (WEB_DIST / "index.html").stat().st_mtime
    except OSError:
        return ""
    if newest <= built:
        return ""
    return "前端 dist 落后于源码，界面可能不是最新的。重建：cd frontend && pnpm build"


# Windows 的注册表把 `.js` 关联成 `text/plain`，而 `mimetypes` 会读注册表，
# 于是 `StaticFiles` 会把打包好的 JS 当纯文本发出去。`<script type="module">`
# 对 MIME 是**严格**校验的，浏览器直接拒绝执行——表现是整页全白，且 Linux/macOS
# 上复现不出来。显式钉死这两种扩展名，别依赖机器上的注册表
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

# 挂在**最后**：/api/*、/docs、/openapi.json 都更早注册，先匹配先赢，
# 这一挂只兜住剩下的路径
if WEB_DIST.is_dir():
    # directory 是**关键字参数**（starlette 1.6 起不再接受位置传参）
    app.mount("/", _UiFiles(directory=WEB_DIST, html=True), name="ui")
    _stale = _ui_stale_hint()
    if _stale:
        # flush：重定向到文件时 stdout 是块缓冲的，提示不该等缓冲区满才出现
        print(_stale, flush=True)
else:
    # 目录不存在时**不挂载**而不是让它抛：`StaticFiles` 会直接 raise，那会让
    # 「我只想用 API」的人一头雾水。但也不能静默——静默的表现是打开根路径只有
    # 一个 404，分不清是没构建还是路径写错了
    print("未找到 %s——本次只提供 API。构建界面：cd frontend && pnpm build" % WEB_DIST,
          flush=True)

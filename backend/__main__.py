# -*- coding: utf-8 -*-
# 用 uv 起后端：
#     uv run python -m backend                 # 默认 0.0.0.0:8787
#     LEGADO_PORT=9000 uv run python -m backend
#     LEGADO_RELOAD=1 uv run python -m backend # 开发热重载
#
# 也可以绕过本文件直接：
#     uv run uvicorn backend.app:app --host 0.0.0.0 --port 8787
import os


def main() -> None:
    import uvicorn

    host = os.getenv("LEGADO_HOST", "0.0.0.0")
    port = int(os.getenv("LEGADO_PORT", "8787"))
    reload_on = os.getenv("LEGADO_RELOAD", "") in ("1", "true", "yes")
    print("Legado 后端启动中 -> http://%s:%d  (文档 /docs)" % (host, port))
    if reload_on:
        uvicorn.run("backend.app:app", host=host, port=port, reload=True)
    else:
        uvicorn.run("backend.app:app", host=host, port=port)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# 用 uv 起后端：
#     uv run python -m backend                    # 默认 0.0.0.0:8787
#     uv run python -m backend --reload           # 开发热重载
#     uv run python -m backend --host 127.0.0.1 --port 9000
#
# 环境变量（LEGADO_HOST / LEGADO_PORT / LEGADO_RELOAD）仍然生效，命令行参数优先。
# 之所以要有命令行的形式：PowerShell 里 `$env:LEGADO_RELOAD="1"` 只对当前会话有效，
# 新开一个终端就没了。
#
# 也可以绕过本文件直接：
#     uv run uvicorn backend.app:app --host 0.0.0.0 --port 8787
import argparse
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 热重载只监视代码目录。
#:
#: **不传 reload_dirs 的话 uvicorn 会盯着整个 CWD**（仓库根），于是 .venv 里
#: 400+ 个 .py、frontend/node_modules 的 1371 个目录全在监视范围内。代价实测：
#: 退回 StatReload 轮询时是 313ms/次（125% 单核，忙循环），限定后 10ms/次；
#: 即便用 WatchFilesReload，少注册一千多个目录句柄也更省。
RELOAD_DIRS = ("backend", "core", "services", "cli", "tools")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _watchfiles_installed() -> bool:
    """uvicorn 装了 watchfiles 才用 WatchFilesReload（OS 文件事件）。"""
    import importlib.util

    return importlib.util.find_spec("watchfiles") is not None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend", description="Legado 书源管理后端")
    parser.add_argument(
        "--host", default=os.getenv("LEGADO_HOST", "0.0.0.0"),
        help="监听地址（默认 0.0.0.0，或环境变量 LEGADO_HOST）")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("LEGADO_PORT", "8787")),
        help="监听端口（默认 8787，或环境变量 LEGADO_PORT）")
    # BooleanOptionalAction 一次给出 --reload / --no-reload：
    # 环境变量设了 LEGADO_RELOAD 时，还得能用 --no-reload 关掉它
    parser.add_argument(
        "--reload", action=argparse.BooleanOptionalAction,
        default=_env_flag("LEGADO_RELOAD"),
        help="开发热重载，只监视代码目录（或环境变量 LEGADO_RELOAD=1）")
    return parser


def main(argv=None) -> None:
    import uvicorn

    args = build_parser().parse_args(argv)
    print("Legado 后端启动中 -> http://%s:%d  (文档 /docs)" % (args.host, args.port))
    if args.reload:
        # 热重载会杀掉进行中的后台任务（校验/修复），别在做全量校验时开着
        print("热重载已开启，监视: %s" % "、".join(RELOAD_DIRS))
        if not _watchfiles_installed():
            # 漏装 watchfiles 时 uvicorn 会**静默**退回 StatReload 轮询：
            # 每 0.25 秒递归扫一遍 *.py 并逐个 stat，实测持续占约 4% 单核。
            # 这种"没报错但明显更费"的降级必须自己说出来
            print("警告: 未安装 watchfiles，热重载将退回 StatReload 轮询"
                  "（持续约占 4% 单核）。安装: uv sync")
        uvicorn.run("backend.app:app", host=args.host, port=args.port,
                    reload=True,
                    reload_dirs=[str(_ROOT / name) for name in RELOAD_DIRS])
    else:
        uvicorn.run("backend.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

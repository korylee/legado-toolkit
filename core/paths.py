# -*- coding: utf-8 -*-
"""数据目录解析（迁移期兼容 legado-tools/）。"""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_DIRS = (ROOT / "data", ROOT / "legado-tools")


def data_dir() -> pathlib.Path:
    env = os.getenv("LEGADO_DATA_DIR")
    if env:
        return pathlib.Path(env)
    for d in _DIRS:
        if d.is_dir():
            return d
    return ROOT / "data"


def data_path(*parts) -> str:
    return str(data_dir().joinpath(*parts))


#: 启动器的参数文件（**相对 data/**）：跑批与调试都写它，Gradle 那条链靠环境变量
#: `LEGADO_APPSERVICE_ARGS` 找到它（见 `core/jvm_debug.ARGS` 与 `backend/api/jvm.py`）。
#: 零件写在这一处：两侧都要拼同一个路径，而其中一侧必须在**调用时**算
#: （测试会换 data 目录，import 时算死的常量会把它钉在真目录上）。
ARGS_PARTS = ("app_probe", "args.properties")

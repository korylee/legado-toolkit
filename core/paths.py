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

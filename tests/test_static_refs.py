# -*- coding: utf-8 -*-
"""静态闸门：`X.attr` 里的 X 既没 import、也没被 `from … import *` 带进来 → 报错。

由来（2026-09-20）：`backend/api/jvm.py` 的 `_run_gradle` 用了 `os.environ` 却忘了
`import os`，于是 `POST /api/jvm/run` **每次调用都 500**——产品里「本机引擎」跑批走的
就是它（lessons §六十九 的第二次实例：同样因为那一行在打桩的下游之外，测试拦不住）。

不跑代码、只做 AST 静态检查（几毫秒），管的是「名字没定义」这一**类**错。名字来自
哪里都算：模块级 import / 函数内 import / `from X import *`（去那个文件静态取它导出的
名字）/ 形参与同名的局部变量（那是合法的，不该误报）。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 只查「看起来像模块」的限定名。**白名单是刻意的**：`data.get(...)` 里的 `data`
#: 是局部变量，不在名单里，永远不会误报。
MODULES = {
    "os", "sys", "re", "json", "time", "shutil", "subprocess", "pathlib", "datetime",
    "threading", "tempfile", "random", "sqlite3", "collections", "asyncio", "hashlib",
    "base64", "csv", "zipfile", "functools", "itertools", "typing", "warnings", "glob",
    "math", "uuid", "socket", "ssl", "logging", "traceback", "unicodedata", "textwrap",
}

#: 扫源码目录（不含 tests：测试里同名局部变量更多，噪声大于收益）
ROOTS = ("backend", "core", "cli", "scripts")


def _wildcard_exports(modname: str) -> "set[str]":
    """`from X import *` 带进来的名字：静态读 X 的源码，取顶层定义 / 赋值 / import。"""
    candidate = _ROOT.joinpath(*modname.split(".")).with_suffix(".py")
    if not candidate.exists():
        return set()
    names: "set[str]" = set()
    for node in ast.parse(candidate.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _missing(path: pathlib.Path) -> "list[str]":
    provided: "set[str]" = set()
    stars: "set[str]" = set()
    used: "set[str]" = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            provided.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                stars.add(node.module or "")
            else:
                provided.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.arg):
            provided.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            provided.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    for star in stars:
        provided |= _wildcard_exports(star)
    return sorted((used & MODULES) - provided)


class StaticRefTests(unittest.TestCase):
    def test_no_undefined_module_names(self):
        bad = []
        for root in ROOTS:
            for path in sorted(_ROOT.joinpath(root).rglob("*.py")):
                miss = _missing(path)
                if miss:
                    bad.append("%s：用了 %s，但文件里没 import（也没有 import *）"
                               % (path.relative_to(_ROOT).as_posix(), ", ".join(miss)))
        self.assertEqual(bad, [], "静态检查发现未定义的名字：\n  " + "\n  ".join(bad))


if __name__ == "__main__":
    unittest.main()

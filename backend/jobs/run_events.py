# -*- coding: utf-8 -*-
"""一次运行留在运行目录里的两本账：**事件**与**失败清单**（唯一一份生产者）。

为什么单独成模块：跑批（``backend/jobs/jvm_exec``）与调试 job
（``backend/jobs/jvm_debug_job``）都往同一本账里追加，而**行号即游标**是消费端
（``backend/api/job_timeline``）的契约——两个生产者各写一份格式，游标就对不上，
表现是「时间线缺行」而不是报错。

清场口径也在这里：**成功只留这两本账**。
``sources.json`` / ``manifest.json`` / ``results.jsonl`` / ``DONE`` 是执行产物
（重试恢复要用它们；留着却会让「再次运行」把已完成的批误判成"已恢复"而空转），
事件与失败是**界面事实**——清了，跑成功的批就永远看不到时间线（上一版正是这样：
只剩 ``result_json`` 里 500 条尾巴，开头那几行永远丢了）。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import threading
import time
from typing import Any, Iterable, Optional

#: 追加的模块锁：块线程与任务协程会交错写同一个文件。
_LOCK = threading.Lock()

EVENTS_NAME = "events.jsonl"
FAILURES_NAME = "failures.jsonl"

#: 清场时保留的文件（**唯一一份**：跑批与调试共用）。
KEEP_ON_TRIM = (EVENTS_NAME, FAILURES_NAME)


def append_event(run_dir: Optional[pathlib.Path], kind: str, **fields: Any) -> None:
    """骨架事件追加到运行目录的 ``events.jsonl``。

    一行一条、**坏行也占号**（消费端按行号增量读，见 ``backend/api/job_timeline``）。
    事件是辅助证据：写不进去（OSError）不拦运行本身。
    """
    if run_dir is None:
        return
    record = {"ts": round(time.time(), 3), "kind": kind, **fields}
    with _LOCK:
        try:
            with open(pathlib.Path(run_dir) / EVENTS_NAME, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def append_failure(run_dir: Optional[pathlib.Path], *, url: Any, name: Any, state: Any,
                   reason: Any, stage: Any = "", chunk: Any = "") -> None:
    """把一条**已经判定的失败**记进 ``failures.jsonl``。

    「在产生处写一次」：跑批读那一块结果时手里就有 rows，写在这里；消费端因此
    不必每拍重扫各块 ``results.jsonl``（那是把已知的事重算）。行形状与消费端一致
    ``{url,name,state,reason,stage,chunk}``；reason 的截断在消费端做，文件留原文。
    """
    if run_dir is None:
        return
    record = {"url": str(url or ""), "name": str(name or ""),
              "state": str(state or ""), "reason": str(reason or ""),
              "stage": str(stage or ""), "chunk": str(chunk or "")}
    with _LOCK:
        try:
            with open(pathlib.Path(run_dir) / FAILURES_NAME, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def record_failures(run_dir: Optional[pathlib.Path], rows: Iterable[Any],
                    chunk: str = "") -> None:
    """一批结论里没通过的那些记进失败清单。

    通过判据与 ``core.jvm_health`` 的映射同一条：**只有 ``state == "ok"`` 算通过**，
    其余原值（no_result / timeout / error / login_wall / …）都算非通过。
    """
    for row in rows or []:
        if not isinstance(row, dict) or row.get("state") == "ok":
            continue
        append_failure(run_dir, url=row.get("url"), name=row.get("name"),
                       state=row.get("state"), reason=row.get("reason"),
                       stage=row.get("stage"), chunk=chunk)


def _under(run_dir: Optional[pathlib.Path], root) -> Optional[pathlib.Path]:
    """``run_dir`` 必须**严格位于** ``root`` 之下，否则返回 None。

    防的是手滑：删目录这条路上，一个算错的路径就是工作目录/仓库根。
    """
    if run_dir is None:
        return None
    try:
        target = pathlib.Path(run_dir).resolve()
        base = pathlib.Path(root).resolve()
    except OSError:
        return None
    if target.parent != base or target == base:
        return None
    return target


def trim_run_dir(run_dir: Optional[pathlib.Path], root) -> None:
    """成功清场：只留事件与失败两本账，其余执行产物删掉。"""
    target = _under(run_dir, root)
    if target is None or not target.is_dir():
        return
    for child in target.iterdir():
        if child.name in KEEP_ON_TRIM:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


def remove_run_dir(run_dir: Optional[pathlib.Path], root) -> None:
    """整目录删除（跑之前就早退的路径用它：那时没有任何事件可留）。"""
    target = _under(run_dir, root)
    if target is not None:
        shutil.rmtree(target, ignore_errors=True)

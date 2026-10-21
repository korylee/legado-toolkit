# -*- coding: utf-8 -*-
"""一次运行留在运行目录里的两本账：**事件**与**失败清单**（唯一一份生产者）。

为什么单独成模块：批量校验（``backend/jobs/jvm_exec``）与调试 job
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
#: 模型调用的账本（AI 补全）：**输入输出原文**。与事件账本同一套读法（行号即游标），
#: 单独一本的理由是形状与体量都不同：提示词几十 KB，按需单取，不进事件流。
LLM_NAME = "llm.jsonl"

#: 清场时保留事件、失败、模型调用三本账及其 per-job attempts 容器
#: （唯一一份，共批量校验、调试与 AI 补全使用）。
KEEP_ON_TRIM = (EVENTS_NAME, FAILURES_NAME, LLM_NAME, "attempts")


def append_event(run_dir: Optional[pathlib.Path], event: str, /, **payload: Any) -> None:
    """骨架事件追加到运行目录的 ``events.jsonl``。

    一行一条、**坏行也占号**（消费端按行号增量读，见 ``backend/api/job_timeline``）。
    事件是辅助证据：写不进去（OSError）不拦运行本身。

    事件名是**位置专用参数**（``/``）：事件数据里本来就有叫 ``kind`` / ``fields``
    的字段（提议族的取样类型、这一族要问哪些字段），形参若可被关键字命中，撞名会抛
    「got multiple values for argument」——真踩过：兜底 except 把它吞了，界面上只
    看到进度缺一段、账本里少一类行。
    """
    if run_dir is None:
        return
    # ``kind`` / ``ts`` 是账本的**保留键**：放在最后展开，载荷里的同名键覆盖不掉它们
    # （真踩过：AI 补全把取样类型也叫 kind，事件名当场被换成 "text"/"media"）
    record = {**payload, "ts": round(time.time(), 3), "kind": event}
    with _LOCK:
        try:
            with open(pathlib.Path(run_dir) / EVENTS_NAME, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def append_failure(run_dir: Optional[pathlib.Path], *, url: Any, name: Any, state: Any,
                   reason: Any, stage: Any = "", chunk: Any = "") -> None:
    """把一条**已经判定的失败**记进 ``failures.jsonl``。

    「在产生处写一次」：批量校验读那一块结果时手里就有 rows，写在这里；消费端因此
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


def append_llm_call(run_dir: Optional[pathlib.Path], record: Dict[str, Any]) -> None:
    """把**一次模型调用**记进 ``llm.jsonl``（输入输出原文）。

    与事件账本同一套约定：一行一条、坏行也占号、写不进去（OSError）不拦运行本身
    ——账本是排障材料，不是运行的前置条件。锁也共用一把：块线程与任务协程会交错写。
    """
    if run_dir is None:
        return
    with _LOCK:
        try:
            with open(pathlib.Path(run_dir) / LLM_NAME, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def record_failures(run_dir: Optional[pathlib.Path], rows: Iterable[Any],                    chunk: str = "") -> None:
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

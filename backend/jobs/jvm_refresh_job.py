# -*- coding: utf-8 -*-
"""通过后台任务刷新 JVM runtime snapshot。"""
import asyncio
import pathlib
import subprocess
import sys
from typing import Any, Dict

from backend.jobs import runner


def _run_refresh_cli(script: pathlib.Path, root: pathlib.Path):
    process = subprocess.run(
        [sys.executable, str(script), "--refresh"], cwd=str(root),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    output = "\n".join((process.stdout or "").splitlines()[-60:]).strip()
    return process.returncode, output


@runner.register("jvm_refresh")
async def run_jvm_refresh_job(job_id: str, _store: Any,
                              _payload: Dict[str, Any]) -> Dict[str, Any]:
    runner.update_phase(job_id, "refreshing_snapshot")
    root = pathlib.Path(__file__).resolve().parents[2]
    script = root / "scripts" / "jvm_debug_direct.py"
    code, output = await asyncio.to_thread(_run_refresh_cli, script, root)
    if code:
        raise RuntimeError("runtime snapshot 刷新失败" + ("：\n" + output if output else ""))
    return {"ok": True}

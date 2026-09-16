# -*- coding: utf-8 -*-
"""上次进程留下的任务：服务启动时收尾（`Store.fail_orphan_jobs` + app 的 lifespan）。

崩溃、强杀、热重载都会把任务行留在 `running`/`pending`——任务活在进程内的 asyncio
task 里，进程一死它们就没了，而库里那行再也没人推进。`sweep_jobs` 的 TTL 最终能
收掉它们，但要等 7 天；这 7 天里任务抽屉一直显示它在跑、徽标也一直挂着（实测崩溃
那次：一条 check 永远停在 500/3861）。

两个方向都要守，缺一个都等于没修：

  - **判据**：谁该动、谁不该动
  - **调用点**：函数写对了但没人在启动时调，症状和没写一模一样。这里用 app 的
    lifespan 验，不起 TestClient（本仓库没有那个依赖，见 test_backend_ui.py 开头）

不起真实服务：`lifespan_context` 是 starlette 的公开属性，手动进出一次就够。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
import uuid

from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)

#: 四种状态各一条：两个非终态（该动）、两个终态（不该动）
_SEED = (("running", "running"), ("pending", "pending"),
         ("done", "done"), ("failed", "failed"))


class _DataDirCase(unittest.TestCase):
    """每个用例一个独立的 data 目录（Store 认环境变量 LEGADO_DATA_DIR）。"""

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_job_recovery_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> None:
        with Store() as st:
            for job_id, status in _SEED:
                st.create_job(job_id, "check")
                st.update_job(job_id, status=status, progress=1, total=2,
                              result={"checked": 1})

    def _statuses(self) -> dict:
        with Store() as st:
            rows = st.conn.execute("SELECT id, status FROM jobs").fetchall()
        return {r["id"]: r["status"] for r in rows}

    def _result(self, job_id: str) -> dict:
        with Store() as st:
            row = st.conn.execute("SELECT result_json FROM jobs WHERE id = ?",
                                  (job_id,)).fetchone()
        return json.loads(row["result_json"] or "{}")


class FailOrphanJobsTests(_DataDirCase):
    def test_only_non_terminal_jobs_are_touched(self) -> None:
        """终态的两条不能动——它们的结论是用户要看的东西。"""
        self._seed()
        with Store() as st:
            self.assertEqual(st.fail_orphan_jobs(), 2)
        self.assertEqual(self._statuses(),
                         {"running": "failed", "pending": "failed",
                          "done": "done", "failed": "failed"})

    def test_reason_lands_in_result_json(self) -> None:
        """**不能只改状态不写原因**：任务抽屉里那行详情读的就是它。空着的话用户
        只看到一条变红的任务，不知道为什么——而「进程重启」恰恰是他没做过的事。"""
        self._seed()
        with Store() as st:
            st.fail_orphan_jobs()
        self.assertTrue(self._result("running").get("error"),
                        "改完状态要写清原因，否则详情栏是空的")

    def test_it_is_idempotent(self) -> None:
        """第二次调用必须是 0：它非幂等的话，每次启动都会把「进程重启」这句
        写回**已经收尾过的**行上，而那行本来可能已经有真实结论了。"""
        self._seed()
        with Store() as st:
            self.assertEqual(st.fail_orphan_jobs(), 2)
            self.assertEqual(st.fail_orphan_jobs(), 0)


class StartupWiringTests(_DataDirCase):
    def test_lifespan_sweeps_orphans(self) -> None:
        """服务进程启动时要真的调它。

        **这条守的是调用点**：把 ``runner.recover_orphans()`` 从 lifespan 里拿掉、
        或者整个 lifespan 忘了传给 FastAPI，判据那几条用例照样全绿——而事故现场
        一点没变。
        """
        self._seed()
        from backend.app import app

        async def _start():
            ctx = app.router.lifespan_context(app)
            await ctx.__aenter__()
            await ctx.__aexit__(None, None, None)

        asyncio.run(_start())
        statuses = self._statuses()
        self.assertEqual(statuses["running"], "failed")
        self.assertEqual(statuses["pending"], "failed")
        self.assertEqual(statuses["done"], "done", "终态任务不能被启动流程改写")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_job_recovery` → 确认变红 → 还原）。
#
#  M35  把 `runner.recover_orphans()` 从 app 的 lifespan 里拿掉（改成只读一下它的
#        docstring，保持「函数还在、只是没人调」的形状）
#         → test_lifespan_sweeps_orphans 红（'running' != 'failed'）
#         判据那三条**照样全绿**——判据写对了但没人在启动时调，症状和没写一样。
#         这就是调用点要单独守一遍的原因。

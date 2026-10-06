# -*- coding: utf-8 -*-
"""任务列表（``GET /api/jobs``）的筛选口径与列表行形状。

两件事必须同时成立，而它们方向相反：

1. 「一屏看全部任务」要能直接看出这次跑得好不好 → 列表行得带结论摘要；
2. 结果里有 ``items[:500]``，全量校验一条上百 KB → 不能进列表响应。

同时满足只能靠**服务端投影**，所以这里钉住投影后的字段集合（不许出现 ``result_json``）
和那几个数。另外钉住「默认窗口 = 任务保留期」：``JOBS_TTL_DAYS`` 决定过期即清，
列表窗口决定最近多少天算历史——用两根轴会分叉成「列表里留着已经该清的」。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import unittest
import uuid
from typing import Any, Dict

from backend.api.jobs import _RECENT_DAYS, _job_row, get_job, list_jobs
from core.store import JOBS_TTL_DAYS, Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def _row(result: Any = None, **over: Any) -> Dict[str, Any]:
    """列表行的任务行夹具：形状 = ``Store`` 的**轻量行**（``_JOB_LIGHT_COLUMNS``）。

    它**不含 result_json**——那几个标量是 SQL 里 ``json_extract`` 抽好的。夹具按
    「一份校验结果」写，再折成轻量列，这样断言读起来还是结果的样子。
    """
    job: Dict[str, Any] = {
        "id": "j1", "kind": "check", "status": "done", "phase": "finished",
        "progress": 5, "total": 5, "retry_of": "", "expires_at": "",
        "created_at": "2026-09-23 10:00:00", "updated_at": "2026-09-23 10:00:02",
        "result_checked": None, "result_ok": None,
        "result_changed": None, "result_error": None,
    }
    if isinstance(result, dict):
        job["result_checked"] = result.get("checked")
        job["result_ok"] = (result.get("dist") or {}).get("ok")
        changed = (result.get("transitions") or {}).get("changed")
        job["result_changed"] = json.dumps(changed) if changed is not None else None
        job["result_error"] = result.get("error")
    job.update(over)
    return job


class JobRowProjectionTests(unittest.TestCase):
    def test_check_result_is_reduced_to_four_numbers(self):
        row = _job_row(_row({
            "checked": 5, "dist": {"ok": 4},
            "transitions": {"changed": {"dead": 1}},
            "items": [{"url": "https://a.example"}],
        }))
        self.assertEqual(
            row["summary"], {"checked": 5, "ok": 4, "fail": 1, "changed_total": 1})

    def test_result_json_never_reaches_the_list_row(self):
        """投影的判据是「响应里没有它」，不是「我们记得没加它」。"""
        row = _job_row(_row({"checked": 1, "items": [{"url": "https://a.example"}]}))
        self.assertNotIn("result_json", row)
        self.assertNotIn("payload", row)

    def test_missing_dist_counts_everything_as_failed(self):
        """没有 dist 时不能把 ok 当 0 而 fail 也算 0——两个数不能都变小。"""
        row = _job_row(_row({"checked": 3}))
        self.assertEqual(row["summary"]["ok"], 0)
        self.assertEqual(row["summary"]["fail"], 3)

    def test_non_check_result_has_no_summary(self):
        """生成类任务的结果里没有 checked——不编一个「通过 0」出来。"""
        row = _job_row(_row({"urls": ["https://a.example"]}))
        self.assertIsNone(row["summary"])

    def test_broken_result_json_is_not_an_error(self):
        """坏结果不产生摘要，但也不让这一行消失（守卫在 SQL 层，见下面的端到端测试）。"""
        row = _job_row(_row(**{"result_checked": None, "result_error": None}))
        self.assertIsNone(row["summary"])
        self.assertEqual(row["error"], "")
        self.assertEqual(row["id"], "j1")

    def test_running_job_has_no_result_yet(self):
        row = _job_row({"id": "j1", "kind": "jvm_run", "status": "running",
                        "progress": 3, "total": 10})
        self.assertIsNone(row["summary"])
        self.assertEqual(row["progress"], 3)

    def test_failure_reason_reaches_the_list_row(self):
        """列表里必须看得到「为什么失败」——否则要逐条点开弹窗才知道。"""
        row = _job_row(_row({"error": "环境就绪检查未通过：App 源码目录不存在"},
                            status="failed"))
        self.assertEqual(row["error"], "环境就绪检查未通过：App 源码目录不存在")

    def test_checked_must_be_a_number(self):
        """判据与详情一致：`checked` 是字符串时不算校验结果。

        `json_extract` 对 JSON 字符串会**原样返回**，所以列表侧不能只判「不是 None」。
        """
        row = _job_row(_row(**{"result_checked": "5", "result_ok": 4}))
        self.assertIsNone(row["summary"])

    def test_failure_reason_is_bounded(self):
        """原因可能是一整段 Gradle 报错——进列表必须截断。"""
        row = _job_row(_row({"error": "x" * 5000}, status="failed"))
        self.assertLess(len(row["error"]), 5000)

    def test_successful_job_has_no_reason(self):
        row = _job_row(_row({"checked": 1, "dist": {"ok": 1}}))
        self.assertEqual(row["error"], "")

    def test_recent_window_is_the_retention_period(self):
        """两根轴必须是同一个数（见模块 docstring）。"""
        self.assertEqual(_RECENT_DAYS, JOBS_TTL_DAYS)


class JobListFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_jobs_list_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _age(st: Store, job_id: str, days: float) -> None:
        """把任务的创建时间拨到 N 天前（不想为测试等 7 天）。"""
        with st.conn:
            st.conn.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - days * 86400)), job_id))

    def _seed(self, st: Store) -> None:
        st.create_job("running", "jvm_run")
        st.update_job("running", status="running", progress=3, total=10)
        st.create_job("cancelling", "jvm_run")
        st.update_job("cancelling", status="cancel_requested")
        st.create_job("finished", "jvm_run")
        st.update_job("finished", status="done", result={
            "checked": 5, "dist": {"ok": 4}, "transitions": {"changed": {"dead": 1}}})
        st.create_job("generated", "add")
        st.update_job("generated", status="done", result={"urls": ["https://a.example"]})
        st.create_job("ancient", "jvm_run")
        st.update_job("ancient", status="done")
        self._age(st, "ancient", JOBS_TTL_DAYS + 30)
        # 建得很早但**还在跑**：它是这个列表最该显示的东西，不能被窗口切掉
        st.create_job("long_run", "jvm_run")
        st.update_job("long_run", status="running", progress=1, total=99)
        self._age(st, "long_run", JOBS_TTL_DAYS + 30)

    def _ids(self, **kw: Any) -> set:
        with Store(self.db) as st:
            self._seed(st) if kw.pop("seed", True) else None
            return {row["id"] for row in list_jobs(st=st, **kw)["items"]}

    def test_malformed_result_does_not_break_the_whole_list(self):
        """一条写坏的结果不能带走整张列表。

        `json_extract` 遇到无效 JSON 会直接抛 `malformed JSON`——没有 `json_valid`
        守卫的话这个 SELECT 就失败，任务中心整个打不开（Python 侧解析时是 try/except
        兜住的，搬到 SQL 就必须显式补上）。
        """
        with Store(self.db) as st:
            st.create_job("bad", "check")
            st.update_job("bad", status="done")
            with st.conn:
                st.conn.execute("UPDATE jobs SET result_json = ? WHERE id = 'bad'",
                                ("{不是 JSON",))
            st.create_job("good", "check")
            st.update_job("good", status="done", result={
                "checked": 2, "dist": {"ok": 2}})
            rows = st.list_jobs()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {"bad", "good"})
        self.assertIsNone(by_id["bad"]["result_checked"])
        self.assertEqual(by_id["good"]["result_checked"], 2)

    def test_default_scope_is_active_plus_recent_window(self):
        """默认不开全量：过期历史不进首屏，在跑的必须进（不论多久以前建的）。"""
        self.assertEqual(self._ids(),
                         {"running", "cancelling", "finished", "generated", "long_run"})

    def test_window_only_filters_terminal_rows(self):
        """窗口只筛终态行：`created_at >= since` 一刀切会让在跑的长任务凭空消失。"""
        ids = self._ids()
        self.assertIn("long_run", ids)     # 建得早、还在跑
        self.assertNotIn("ancient", ids)   # 建得早、已结束

    def test_all_scope_includes_expired_history(self):
        self.assertIn("ancient", self._ids(scope="all"))

    def test_active_scope_covers_every_in_flight_status(self):
        """档位由后端定：前端传 active 就该拿到 pending/running/cancel_requested 全部。"""
        self.assertEqual(self._ids(status="active"),
                         {"running", "cancelling", "long_run"})

    def test_status_and_kind_filters_combine(self):
        self.assertEqual(self._ids(status="done", kind="add"), {"generated"})

    def test_rows_are_ordered_newest_first(self):
        with Store(self.db) as st:
            self._seed(st)
            ids = [row["id"] for row in list_jobs(scope="all", st=st)["items"]]
        self.assertLess(ids.index("finished"), ids.index("ancient"))

    def test_list_response_declares_its_cap(self):
        """上限由后端下发：界面要说明「只显示最近 N 条」，不该在前端再写一个 200。"""
        with Store(self.db) as st:
            page = list_jobs(st=st)
        self.assertIn("items", page)
        self.assertIsInstance(page["limit"], int)
        self.assertGreater(page["limit"], 0)

    def test_status_endpoint_is_lightweight(self):
        """单任务轮询端点不带 result_json。

        明细弹窗在任务运行期间每 2 秒拉一次它——带上结果就是每 2 秒传上百 KB。
        """
        with Store(self.db) as st:
            st.create_job("j1", "jvm_run")
            st.update_job("j1", status="done", result={
                "checked": 2, "dist": {"ok": 1},
                "items": [{"url": "https://a.example"}]})
            row = get_job("j1", st=st)
        self.assertNotIn("result_json", row)
        self.assertEqual(row["summary"], {"checked": 2, "ok": 1, "fail": 1,
                                          "changed_total": 0})

    def test_list_rows_carry_summary_but_not_result_json(self):
        """端到端：跑完的校验在列表里能直接读出结论。"""
        with Store(self.db) as st:
            self._seed(st)
            rows = list_jobs(status="done", st=st)["items"]
        row = next(r for r in rows if r["id"] == "finished")
        self.assertEqual(row["summary"], {"checked": 5, "ok": 4, "fail": 1,
                                          "changed_total": 1})
        self.assertNotIn("result_json", row)
        self.assertIsNone(next(r for r in rows if r["id"] == "generated")["summary"])


if __name__ == "__main__":
    unittest.main()

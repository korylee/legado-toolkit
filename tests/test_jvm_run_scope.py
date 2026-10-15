# -*- coding: utf-8 -*-
"""批量校验的范围：**只跑选中的那几条**（S5-A 第二期）。

守三件事，都是「不报错但结果不对」的那一类：

1. **两侧都要归一 URL**（AGENTS #5）：前端给的是库里归一化过的 `source_url`，而导出的是
   源 JSON 里 `bookSourceUrl` 的**原文**——不归一就一条都匹配不上，而表现是「跑完了但
   JVM 列没变」，谁也不报错。
2. **一条都没匹配上要明说**：那种情况下开跑一次空批，界面上会说「完成：0 条结论已入库」，
   读起来像「这些源没问题」。
3. **范围给几条就导出几条**：勾选/筛选是范围的全部依据，没有另一个会静默截断它的上限。

不跑 Gradle、不联网：`_export_sources_file` 用真的（它只读库 + 写一个临时 JSON），
`_run_gradle` / `readiness` / `Store` 打桩。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from backend.api import jvm as jvm_api
from backend.jobs import jvm_exec
from backend.jobs import runner as jobs_runner
from backend.schemas import JvmRunRequest
from core import jvm_direct
from core.checker import CACHE_VERSION
from core.loader import _normalize_url
from core.store import Store


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_scope_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.db = str(self.tmp / "sources.sqlite3")
        self.probe = self.tmp / "probe"
        self.probe.mkdir()
        (self.probe / "appservice").mkdir()
        (self.probe / "appservice" / "legado-gradle.bat").write_text("@echo off\n",
                                                                    encoding="utf-8")
        self.gradle_calls = 0
        self.daemon_calls = 0
        self.gradle_kwargs = {}
        self.runtime_seen = None
        self.gradle_result = 0
        self.no_output = False
        test_limits = dict(jvm_api.settings_store.LIMITS)
        test_limits["jvm_chunk_size"] = (1, 200)
        for p in (
            mock.patch.object(jvm_exec, "_AGSVC", self.probe / "appservice"),
            mock.patch.object(jvm_api, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jvm_exec, "data_dir", lambda: self.probe / "data"),
            # 运行目录巡检（runner.sweep_run_dirs）按**任务行**决定目录生死：
            # 不隔离它就会拿真库当真依据，还会去动真 data/ 目录
            mock.patch.object(jobs_runner, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jobs_runner, "Store", lambda *a, **kw: Store(self.db)),
            mock.patch.object(jvm_api, "readiness", lambda repo, sdk="": {"ok": True, "checks": [], "runtime": {"app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk", "gradle_user_home": "X:/.gradle"}}),
            mock.patch.object(jvm_api, "execution_readiness", lambda dump=None: {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}),
            mock.patch.object(jvm_exec, "execution_readiness", lambda dump=None: {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}),
            # 隔离真机状态：机器上可能有真实 runtime snapshot（准备态的产物），
            # 不隔离的话单条任务会走上真 daemon——这批测试原本依赖
            # 「data/ 里没有 dump」这个巧合，snapshot 一存在就整批变红。
            mock.patch.object(jvm_direct, "dump_path",
                              lambda: self.probe / "data" / "app_probe" / "test_jvm_env.json"),
            mock.patch.object(jvm_direct, "load_dump",
                              lambda warn_stale=True: {"source": "test"}),
            mock.patch.object(jvm_exec, "_write_meta", lambda rows: "testbatch"),
            mock.patch.object(jvm_api.settings_store, "LIMITS", test_limits),
            mock.patch.object(jvm_exec, "_run_gradle", self._fake_gradle),
            mock.patch("core.jvm_validate_daemon.prepare",
                        lambda *a, **kw: {"outcome": "started", "reason": ""}),
            mock.patch("core.jvm_validate_daemon.probe",
                        lambda *a, **kw: {"sig": "sig"}),
            mock.patch("core.jvm_validate_daemon.run", self._fake_daemon),
            mock.patch.object(jvm_api.settings_store, "load",
                              lambda: {"network": {"proxy": ""}, "jvm": {"app_repo": "X:/repo", "keyword": "我",
                                               "timeout": 25, "concurrency": 8,
                                               "depth": "search", "chunk_size": 25}}),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.batch_seen = []
        self.args_seen = ""
        self._put_source("https://A.com/", "大写 + 尾斜杠的原文")
        self._put_source("https://b.com", "普通源")
        self._put_source("https://c.com/", "另一条")

    def _fake_gradle(self, args_path=None, runtime=None, **kwargs) -> int:
        self.runtime_seen = runtime
        self.gradle_kwargs = dict(kwargs)
        self.gradle_calls += 1
        self.args_seen = pathlib.Path(args_path).read_text(encoding="utf-8")
        source_path = next(line.split("=", 1)[1] for line in self.args_seen.splitlines()
                           if line.startswith("file="))
        self.batch_seen = json.loads(pathlib.Path(source_path).read_text(encoding="utf-8"))
        if self.no_output:
            return self.gradle_result
        out = pathlib.Path(args_path).parent / "results.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                       encoding="utf-8")
        return self.gradle_result

    def _fake_daemon(self, dump, args_path, socket_timeout=None, **kwargs) -> dict:
        self.daemon_calls += 1
        self.args_seen = pathlib.Path(args_path).read_text(encoding="utf-8")
        source_path = next(line.split("=", 1)[1] for line in self.args_seen.splitlines()
                           if line.startswith("file="))
        self.batch_seen = json.loads(pathlib.Path(source_path).read_text(encoding="utf-8"))
        if self.no_output:
            return {"code": 0, "cost_ms": 1}
        out = pathlib.Path(args_path).parent / "results.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                       encoding="utf-8")
        return {"code": 0, "cost_ms": 1}

    def _put_source(self, url: str, name: str, **overrides) -> None:
        # 用真 API 写源（`upsert_sources` 会建表+算 fingerprint）：比手写 INSERT 更接近
        # 生产路径，也不会因为漏 NOT NULL 列而炸
        source = {
            "bookSourceName": name, "bookSourceUrl": url,
            "bookSourceType": 0, "enabled": True,
            "searchUrl": "/search?q={{key}}",
            "ruleSearch": {"bookList": ".book", "name": ".name",
                           "bookUrl": ".item@href"},
            "ruleToc": {"chapterList": ".chapter"},
            "ruleContent": {"content": ".content"},
        }
        source.update(overrides)
        with Store(self.db) as st:
            st.upsert_sources([source])

    def _call(self, urls=None, filt=None, params=None, run_job=True) -> dict:
        """请求（预检：范围 / 空范围原因 / 建任务）**+ 把任务跑完**。

        「跑完」这一步不能省：`gradle_calls` 这类断言看的是任务体做了什么，
        不驱动它就只是"没报错"（实测踩过：一条断言因此恒真）。`store_checks`
        打桩的理由见 test_jvm_run_lock 的同名注释（它会开真管理库）。
        """
        body = JvmRunRequest(urls=urls or [], filter=filt or {}, params=params or {})

        async def go():
            submitted = {}

            def capture_submit(kind, payload, lane=None):
                submitted["payload"] = payload
                return "testjob"

            with mock.patch.object(jvm_api.runner, "submit", side_effect=capture_submit):
                r = await jvm_api.jvm_run(body)
            if run_job and r.get("job_id"):
                # **传真的 store**：任务体要读一次「跑之前的 checks 快照」算变化，
                # 传 None 会当场 AttributeError（生产里 runner 一定会给 store）
                got = await jvm_exec.run_jvm_job("testjob", Store(self.db), submitted["payload"])
                return dict(r, **got)
            return r

        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)),              mock.patch.object(jvm_exec, "Store", lambda *a, **kw: Store(self.db)),             mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0):
            return asyncio.run(go())

    def _batch(self) -> list:
        return self.batch_seen


class ScopeTests(_Base):
    def test_incomplete_sources_are_skipped_and_reported(self) -> None:
        self._put_source("https://incomplete.example", "缺规则源", ruleContent={})

        result = self._call(urls=["https://a.com", "https://incomplete.example"])

        self.assertTrue(result["started"])
        self.assertEqual(result["skipped_rule_incomplete"], 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(self._batch()), 1)
        self.assertEqual(self._batch()[0]["bookSourceUrl"], "https://A.com/")

    def test_all_incomplete_sources_do_not_start_a_job(self) -> None:
        self._put_source("https://incomplete.example", "缺规则源", ruleToc={})

        result = self._call(urls=["https://incomplete.example"], run_job=False)

        self.assertFalse(result["started"])
        self.assertEqual(result["skipped_rule_incomplete"], 1)
        self.assertIn("规则不完整", result["reason"])
        self.assertEqual(self.gradle_calls, 0)
        self.assertEqual(self.daemon_calls, 0)

    def test_payload_without_manifest_is_rejected_with_reason(self) -> None:
        """没有清单就拒绝：连带显式路径的旧 payload 也不放行（生产只走清单）。

        放它继续走只会有两种结局——拿 payload 里的旧路径兜底（兼容层，已删），
        或者在块线程里 KeyError（不可读的失败）。这里钉的是第三种：一条能读的原因。
        """
        run_dir = self.probe / "data" / "app_probe" / "runs" / "legacy"
        run_dir.mkdir(parents=True)
        payload = {
            "prep": {"started": True},
            "run_dir": str(run_dir),
            "single": True,
            "allow_gradle_fallback": False,
        }
        with mock.patch.object(jvm_exec, "_run_gradle") as gradle:
            result = asyncio.run(jvm_exec.run_jvm_job(
                "legacy-job", Store(self.db), payload))
        self.assertFalse(result["ok"])
        self.assertIn("manifest", result["reason"])
        gradle.assert_not_called()

    async def _cancelled_job(self, payload):
        task = asyncio.create_task(jvm_exec.run_jvm_job(
            "cancel-job", Store(self.db), payload))
        started = payload["started"]
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(started.is_set(), "daemon worker 未启动")
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "取消不应绕过仍在运行的 worker")
        payload["release"].set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_batch_cancel_keeps_run_dir_for_resume(self) -> None:
        """批量取消**保留**运行目录：已完成块的 DONE/results 是重试恢复的依据。"""
        run_dir = self.probe / "data" / "app_probe" / "runs" / "cancelled"
        run_dir.mkdir(parents=True)
        source_file = run_dir / "sources.json"
        args_file = run_dir / "args.properties"
        out_path = run_dir / "results.jsonl"
        source_file.write_text("[]", encoding="utf-8")
        args_file.write_text("file=%s\nout=%s\n" % (source_file, out_path), encoding="utf-8")
        manifest = jvm_exec._build_jvm_manifest(
            run_dir=run_dir, source_file=source_file, args_file=args_file,
            out_path=out_path, single=False, execution_plan="validate_daemon",
            allow_gradle_fallback=False, runtime={"app_repo": "X:/repo"},
            readiness={"ok": True}, execution_readiness={},
            readiness_fingerprint="", readiness_checked_at="", source_count=2,
            urls=["https://a.com", "https://b.com"], params={"keyword": "我", "timeout": 25, "concurrency": 8, "depth": "search"}, chunks=[2])
        control = {"started": threading.Event(), "release": threading.Event()}
        payload = {"prep": {"started": True}, "manifest": manifest,
                   **control}

        def blocking_daemon(_dump, args_path, socket_timeout=None, owner=None):
            control["started"].set()
            control["release"].wait(2)
            return {"code": 1, "error": "cancelled"}

        async def go():
            with mock.patch("core.jvm_validate_daemon.run", side_effect=blocking_daemon):
                await self._cancelled_job(payload)

        asyncio.run(go())
        # 取消不伪装完成，但批量现场保留（重试恢复的依据）；锁必须已归还
        self.assertTrue(run_dir.exists())
        self.assertTrue((run_dir / "args.properties").exists())
        from core.jvm_debug import RUN_LOCK
        self.assertTrue(RUN_LOCK.acquire(blocking=False))
        RUN_LOCK.release()

    def test_submission_captures_hashed_manifest(self) -> None:
        submitted = {}

        def capture_submit(kind, payload, lane=None):
            submitted["payload"] = payload
            return "manifest-job"

        async def go():
            with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)), \
                 mock.patch.object(jvm_api.runner, "submit", side_effect=capture_submit):
                return await jvm_api.jvm_run(JvmRunRequest(urls=["https://a.com"]))

        result = asyncio.run(go())
        manifest = submitted["payload"]["manifest"]
        self.assertEqual(result["job_id"], "manifest-job")
        self.assertEqual(manifest["schema"], 3)
        self.assertEqual(manifest["source_count"], 1)
        self.assertEqual(manifest["execution_plan"], "validate_daemon")
        # 单条不分块，但大小序列同样冻结在 manifest 里
        self.assertEqual(manifest["chunks"], [1])
        # 归一化的范围与生效参数也是「提交即冻结」的输入（AGENTS #5：库里归一化
        # 过的 URL 与导出原文必须在这里统一）
        self.assertEqual(manifest["urls"], ["https://a.com"])
        self.assertIn("keyword", manifest["params"])
        self.assertEqual(jvm_exec._manifest_error(manifest), "")

        tampered = dict(manifest)
        tampered["out_path"] = tampered["out_path"] + ".changed"
        self.assertIn("校验失败", jvm_exec._manifest_error(tampered))

        malformed = dict(manifest)
        malformed.pop("args_file")
        body = dict(malformed)
        body.pop("sha256", None)
        malformed["sha256"] = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertIn("结构不受支持", jvm_exec._manifest_error(malformed))

        malformed_chunks = dict(manifest)
        malformed_chunks["chunks"] = ["1"]
        body = dict(malformed_chunks)
        body.pop("sha256", None)
        malformed_chunks["sha256"] = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertIn("chunks", jvm_exec._manifest_error(malformed_chunks))

    def test_queued_job_uses_runtime_snapshot_captured_at_submission(self) -> None:
        submitted = {}
        original = {
            "app_repo": "X:/repo", "java_home": "X:/jdk",
            "android_sdk": "X:/sdk", "gradle_user_home": "X:/.gradle",
        }
        readiness_snapshot = {
            "ok": True,
            "checks": [],
            "runtime": dict(original),
            "fingerprint": "sha256:submission-snapshot",
            "checked_at": "2026-09-24T12:00:00Z",
        }

        async def submit_job():
            def capture_submit(kind, payload, lane=None):
                submitted["payload"] = payload
                return "queued-job"

            with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)), \
                mock.patch.object(jvm_api, "readiness", lambda repo, sdk="": dict(readiness_snapshot)), \
                 mock.patch.object(jvm_api.runner, "submit", side_effect=capture_submit):
                return await jvm_api.jvm_run(JvmRunRequest(urls=["https://a.com"]))

        response = asyncio.run(submit_job())
        self.assertTrue(response["started"])
        self.assertEqual(submitted["payload"]["runtime"], original)
        self.assertEqual(submitted["payload"]["readiness_fingerprint"],
                         readiness_snapshot["fingerprint"])
        self.assertEqual(submitted["payload"]["readiness_checked_at"],
                         readiness_snapshot["checked_at"])

        changed = dict(original, java_home="Y:/new-jdk")
        with mock.patch.object(jvm_api, "readiness", side_effect=AssertionError(
                "执行阶段不应重新检查环境")), \
            mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0):
            result = asyncio.run(jvm_exec.run_jvm_job(
                "queued-job", Store(self.db), submitted["payload"]))

        self.assertTrue(result["ok"])
        self.assertEqual(submitted["payload"]["runtime"], original)
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(self.gradle_calls, 0)

    def test_execution_readiness_drift_does_not_store_result(self) -> None:
        reason = "实际启动环境 JAVA_HOME 与 readiness 不一致"
        with mock.patch.object(jvm_exec, "execution_readiness",
                               return_value={"ok": False, "reason": reason}), \
             mock.patch("core.jvm_validate_daemon.prepare") as prepare:
            result = self._call()
        self.assertFalse(result["ok"])
        self.assertIn("JAVA_HOME", result["reason"])
        prepare.assert_not_called()
        self.assertEqual(self.gradle_calls, 0)
        with Store(self.db) as st:
            self.assertNotIn("https://a.com", st.checks_map())

    def test_daemon_only_result_has_no_gradle_snapshot_diagnostic(self) -> None:
        result = self._call(urls=["https://a.com"])
        self.assertTrue(result["ok"])
        self.assertNotIn("runtime_snapshot", result)

    def test_selector_matches_despite_case_and_trailing_slash(self) -> None:
        """**归一后才比**：库里那条的原文是 `https://A.com/`（大写 + 尾斜杠），
        而列表页给的是归一化过的 `https://a.com`。不归一的话这里会一条都不跑。"""
        self._call(urls=["https://a.com"])
        got = [s["bookSourceUrl"] for s in self._batch()]
        self.assertEqual(got, ["https://A.com/"], "选中的那条要按原文导出（给 JVM 用的是原文）")

    def test_selector_does_not_merge_distinct_fragment_sources(self) -> None:
        """勾 `https://A.com/#分享` 不能把库里那条 `https://A.com/` 一起带出来：
        范围匹配按源身份，忽略 fragment 会一次导出整组。"""
        r = self._call(urls=["https://A.com/#分享"], run_job=False)
        self.assertFalse(r["started"])
        self.assertEqual(r["missing_urls"], ["https://A.com/#分享"])
        self.assertEqual(r["matched_urls"], [])
        self.assertEqual(self.batch_seen, [], "一条也不许导出")

    def test_only_selected_sources_are_exported(self) -> None:
        self._call(urls=["https://b.com", "https://c.com"])
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://b.com", "https://c.com/"])

    def test_selection_is_exported_in_full(self) -> None:
        """选中 3 条就导出 3 条：范围是唯一依据，没有任何上限参与（历史上这里被
        「条数上限」静默截断过——界面上只会看到「选了 3 条」而实际跑了 2 条）。"""
        self._call(urls=["https://a.com", "https://b.com", "https://c.com"])
        self.assertEqual(len(self._batch()), 3)
        self.assertEqual(self.gradle_calls, 0)
        self.assertEqual(self.daemon_calls, 1)

    def test_partial_match_still_runs_and_reports_the_gap(self) -> None:
        """勾 2 条只匹配到 1 条：照跑，但少掉的那条要能看见——否则「已选 2 条」与实际
        只跑 1 条之间的差额是静默的（与「选中全部 N 条」那条静默截断同一类）。"""
        r = self._call(urls=["https://a.com", "https://nope.com"])
        self.assertTrue(r["started"])
        self.assertEqual(r["matched_urls"], ["https://a.com"])
        self.assertEqual(r["missing_urls"], ["https://nope.com"])
        self.assertEqual([s["bookSourceUrl"] for s in self._batch()], ["https://A.com/"])

    def test_unmatched_selection_is_refused_with_a_reason(self) -> None:
        r = self._call(urls=["https://nope.com"])
        self.assertFalse(r["started"])
        self.assertIn("一条都没匹配上", r["reason"])
        self.assertEqual(r["requested_urls"], ["https://nope.com"])
        self.assertEqual(r["matched_urls"], [])
        # 具体是哪条没匹配上要出现在 reason 里：否则用户只能猜
        self.assertIn("https://nope.com", r["reason"])
        self.assertEqual(self.gradle_calls, 0, "匹配不上就不该开跑（空批会被读成「都没问题」）")

    def test_all_sources_are_exported_in_full(self) -> None:
        """全量导出的是库里全部在用源：没有「跑前 N 条」这个隐藏入口。"""
        r = self._call()
        self.assertTrue(r["started"])
        self.assertEqual(len(self._batch()), 3)

    def test_normalized_selector_is_idempotent(self) -> None:
        """再跑一次归一（前端偶尔给已经归一的、也偶尔给原文）结果一样。"""
        self._call(urls=["https://a.com/"])
        first = [s["bookSourceUrl"] for s in self._batch()]
        self.assertEqual(first, ["https://A.com/"])


class FilterScopeTests(_Base):
    """范围也可以给「当前筛选」：一台引擎之后这是最省时间的杠杆。

    全量一次十几分钟，而**站点按 IP 认人**（频繁跑全量会被封）——所以「只重跑待验证
    那一批」不是锦上添花，是主要用法。形状与 `POST /api/export` 的 `filter` 一致，
    前端把列表页那个查询对象原样递过来。
    """

    def _mark_all(self, health: str) -> None:
        with Store(self.db) as st:
            st.save_checks([{"url": u, "v": CACHE_VERSION, "health": health,
                             "checked_at": "2026-09-20 10:00:00"}
                            for u in ("https://a.com", "https://b.com", "https://c.com")])

    def test_filter_selects_the_matching_sources(self) -> None:
        self._mark_all("gfw")
        self._call(filt={"health": "gfw"})
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://A.com/", "https://b.com", "https://c.com/"])

    def test_filter_exports_every_match(self) -> None:
        """筛选范围就是命中多少跑多少（不受分页、也没有别的上限）。"""
        self._mark_all("pending")
        self._call(filt={"health": "pending"})
        self.assertEqual(len(self._batch()), 3)

    def test_filter_by_keyword(self) -> None:
        self._call(filt={"q": "普通"})
        self.assertEqual([s["bookSourceUrl"] for s in self._batch()], ["https://b.com"])

    def test_empty_filter_is_refused_with_a_reason(self) -> None:
        r = self._call(filt={"health": "dead"})
        self.assertFalse(r["started"])
        self.assertIn("筛选", r["reason"])
        self.assertEqual(self.gradle_calls, 0, "筛选没命中就不该开跑")

    def test_run_params_override_the_settings(self) -> None:
        """**本次参数覆盖设置**，而且只影响这一次。

        设置里 `depth = search`（`_Base` 的桩），这次传 `depth = content` → 这次跑深档。
        这条正是"参数该长在动作旁边"的理由：留在设置页时，这一次想验深一层就得先去
        改全局，跑完还得记得改回来。
        """
        self._call(params={"depth": "content"})
        self.assertIn("depth=content", self.args_seen)

    def test_depth_param_reaches_the_args_file(self) -> None:
        """参数要真的落到给 JVM 的那份 `args.properties` 上——不落就是"填了没用"。"""
        self._call(params={"depth": "content"})
        self.assertIn("depth=content", self.args_seen)

    def test_unknown_param_key_is_dropped(self) -> None:
        """未知键丢掉、不报错（`settings_store.coerce` 的契约）；合法键照常生效。"""
        self._call(params={"nope": 1, "depth": "toc"})
        self.assertIn("depth=toc", self.args_seen)
        self.assertNotIn("nope", self.args_seen)

    def test_selection_wins_over_filter(self) -> None:
        """两者都给时**勾选优先**：勾是明确意图，筛选是「这一屏里的」。"""
        self._call(urls=["https://b.com"], filt={"q": "普通"})
        self.assertEqual([s["bookSourceUrl"] for s in self._batch()], ["https://b.com"])


class GradleDiagnosticTests(_Base):
    def test_missing_output_keeps_gradle_tail(self) -> None:
        """启动 JVM 前失败时，任务结果必须带真实 Gradle 输出。"""
        self.no_output = True
        self.gradle_result = {
            "exit": 1,
            "stdout": "",
            "stderr": "Failed to load native library 'native-platform.dll'",
        }
        out = self._call(urls=["https://a.com"])
        self.assertFalse(out["ok"])
        self.assertIn("native-platform.dll", out["reason"])
        self.assertEqual(out["gradle"]["exit"], 1)


class ResultShapeTests(_Base):
    """批量校验结果体与**本地校验那条同形状**（十-2：单条校验切引擎）。

    前端读 items / transitions 的是同一段代码，所以两边少一个键就等于「某一格
    永远不更新」——不报错，只是那一列看着像没校验过。这里走**真的** `store_checks`
    （`_call` 里把它打桩了，那条是给范围测试省事的），让 items 真的从 checks 表里来。
    """

    def _run_single(self):
        body = JvmRunRequest(urls=["https://a.com"], filter={}, params={})

        async def go():
            submitted = {}

            def capture_submit(kind, payload, lane=None):
                submitted["payload"] = payload
                return "testjob"

            with mock.patch.object(jvm_api.runner, "submit", side_effect=capture_submit):
                r = await jvm_api.jvm_run(body)
            return r, await jvm_exec.run_jvm_job("testjob", Store(self.db), submitted["payload"])

        # DNS 交叉验证要打桩：`store_checks` 默认会真去探测（测试不许联网）
        async def _fake_probe(host):
            return "answer", "1.2.3.4"

        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)),                 mock.patch.object(jvm_exec, "Store", lambda *a, **kw: Store(self.db)),                 mock.patch("core.dns_check.probe", _fake_probe):
            return asyncio.run(go())

    def test_result_carries_items_and_transitions(self) -> None:
        r, out = self._run_single()
        self.assertTrue(r.get("started"), r)
        # 与本地那条对得上的四个键（前端 `parseCheckResult` 读 `checked`）
        self.assertEqual(out["checked"], 1)
        self.assertEqual(out["cached"], 0)
        self.assertEqual(out["fetched"], 1)
        # **首次有结论**：库里本来没有这条源的结论。快照若在 `store_checks` **之后**
        # 读，这里会变成 0（新旧一样）——那是「这次变了什么」永远答「没变」的同一个错
        self.assertEqual(out["transitions"]["first_checked"], 1)
        self.assertEqual(out["transitions"]["changed"], {})

    def test_items_carry_what_the_list_needs_to_backfill(self) -> None:
        _r, out = self._run_single()
        it = out["items"][0]
        self.assertEqual(it["url"], "https://a.com")      # 归一化后的键
        for key in ("name", "health", "error",
                    "toc_complete", "content_ok", "search_hit", "checked_at"):
            self.assertIn(key, it, "回填缺字段: %s" % key)
        # 结论是 checks 口径来的（engine=jvm），并进了那张表
        with Store(self.db) as st:
            row = st.checks_map().get("https://a.com")
        self.assertIsNotNone(row, "结论没落进 checks")
        self.assertEqual(row.get("engine"), "jvm")
        self.assertEqual(row.get("health"), it["health"])

    def test_second_run_reports_no_change(self) -> None:
        """第二次跑同一条源：健康度没变 → 不进「变成 X」（快照读的是跑之前那份）。"""
        self._run_single()
        _r, out = self._run_single()
        self.assertEqual(out["transitions"]["changed"], {})
        self.assertEqual(out["transitions"]["first_checked"], 0)

    def test_single_prefers_validate_daemon(self) -> None:
        def fake_daemon(_dump, args_file, owner=None):
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        with mock.patch("core.jvm_direct.load_dump", return_value={
                "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                "environment": {}, "jvmArgs": [], "systemProperties": {},
                "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run", side_effect=fake_daemon) as daemon:
            _request, result = self._run_single()
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertIn("常驻 Validate JVM", result["execution_note"])
        daemon.assert_called_once()
        self.assertEqual(self.gradle_calls, 0)

    def test_single_daemon_failure_falls_back_with_reason(self) -> None:
        with mock.patch("core.jvm_direct.load_dump", return_value={
                "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                "environment": {}, "jvmArgs": [], "systemProperties": {},
                "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run",
                        side_effect=RuntimeError("端口不可用")):
            _request, result = self._run_single()
        self.assertEqual(result["execution_mode"], "gradle_fallback")
        self.assertIn("端口不可用", result["daemon_fallback_reason"])
        self.assertIn("Gradle", result["execution_note"])
        self.assertEqual(self.gradle_calls, 1)

    def test_single_checks_execution_readiness_before_submit_and_daemon(self) -> None:
        events = []
        executable = {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}

        def check_execution(dump=None):
            events.append(("execution_readiness", dump))
            return executable

        def fake_daemon(_dump, args_file, owner=None):
            events.append(("daemon", None))
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        check = mock.Mock(side_effect=check_execution)
        with mock.patch.object(jvm_api, "execution_readiness", new=check), \
             mock.patch.object(jvm_exec, "execution_readiness", new=check), \
            mock.patch("core.jvm_direct.load_dump", return_value={
                 "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                 "environment": {}, "jvmArgs": [], "systemProperties": {},
                 "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run", side_effect=fake_daemon) as daemon:
            _request, result = self._run_single()

        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(check.call_count, 2)
        # 提交点手里还没有 dump，免 dump 是有意的；daemon 前才带真实 dump 复查
        self.assertIsNone(events[0][1])
        self.assertEqual(events[1][1]["classpath"], "x")
        self.assertEqual([event[0] for event in events],
                         ["execution_readiness", "execution_readiness", "daemon"])
        daemon.assert_called_once()
        self.assertEqual(self.gradle_calls, 0)

    def test_queued_snapshot_expiry_skips_daemon_and_falls_back(self) -> None:
        executable = {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}
        expired = {"ok": False, "checks": [{"id": "runtime_classpath", "ok": False}],
                   "reason": "runtime classpath 已失效", "source_sig": "sig"}

        def check_execution(dump=None):
            return executable if dump is None else expired

        check = mock.Mock(side_effect=check_execution)
        with mock.patch.object(jvm_api, "execution_readiness", new=check), \
             mock.patch.object(jvm_exec, "execution_readiness", new=check), \
            mock.patch("core.jvm_direct.load_dump", return_value={
                 "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                 "environment": {}, "jvmArgs": [], "systemProperties": {},
                 "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run") as daemon:
            _request, result = self._run_single()

        self.assertEqual(result["execution_mode"], "gradle_fallback")
        self.assertIn("runtime classpath 已失效", result["daemon_fallback_reason"])
        daemon.assert_not_called()
        self.assertEqual(self.gradle_calls, 1)

    def test_single_uses_daemon_when_gradle_readiness_is_incomplete(self) -> None:
        def fake_daemon(_dump, args_file, owner=None):
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        incomplete = {"ok": False, "checks": [{"id": "android_sdk", "ok": False,
                                                 "hint": "缺少 platforms;android-37"}]}
        executable = {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}
        with mock.patch.object(jvm_api, "readiness", return_value=incomplete), \
             mock.patch.object(jvm_api, "execution_readiness", return_value=executable), \
             mock.patch.object(jvm_exec, "execution_readiness", return_value=executable), \
            mock.patch("core.jvm_direct.load_dump", return_value={
                 "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                 "environment": {}, "jvmArgs": [], "systemProperties": {},
                 "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run", side_effect=fake_daemon) as daemon:
            _request, result = self._run_single()

        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(result["execution_plan"], "validate_daemon")
        self.assertEqual(self.gradle_calls, 0)
        daemon.assert_called_once()

    def test_single_does_not_fallback_when_preparation_is_incomplete(self) -> None:
        incomplete = {"ok": False, "checks": [{"id": "android_sdk", "ok": False,
                                                 "hint": "缺少 platforms;android-37"}]}
        executable = {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}
        with mock.patch.object(jvm_api, "readiness", return_value=incomplete), \
             mock.patch.object(jvm_api, "execution_readiness", return_value=executable), \
             mock.patch.object(jvm_exec, "execution_readiness", return_value=executable), \
            mock.patch("core.jvm_direct.load_dump", return_value={
                 "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                 "environment": {}, "jvmArgs": [], "systemProperties": {},
                 "javaHomeEnv": "C:/jdk"}), \
            mock.patch("core.jvm_validate_daemon.run", side_effect=RuntimeError("端口不可用")):
            _request, result = self._run_single()

        self.assertFalse(result["ok"])
        self.assertIn("不回退 Gradle", result["reason"])
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(self.gradle_calls, 0)

    def test_batch_still_requires_complete_gradle_readiness(self) -> None:
        incomplete = {"ok": False, "checks": [{"id": "android_sdk", "ok": False,
                                                 "hint": "缺少 platforms;android-37"}]}
        with mock.patch.object(jvm_api, "readiness", return_value=incomplete):
            result = self._call()

        self.assertFalse(result["started"])
        self.assertIn("readiness", result)
        self.assertNotIn("execution_readiness", result)
        self.assertEqual(self.gradle_calls, 0)


class ExportTests(_Base):
    """导出这一步本身。**这里曾经是个真缺陷**：`export_sources()` 给的是解析好的书源
    对象，而导出还在按「带 raw_json 的包装」读 → `d = None` → 任何有在用源的库都抛异常，
    也就是说 `/api/jvm/run` 从来没在真库上跑通过（测试把这个函数整个打桩了）。"""

    def test_export_reads_the_view_directly(self) -> None:
        with Store(self.db) as st:
            path = jvm_api._export_sources_file(st)
        rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 3)
        # 只导出 JVM 侧要看的那几个键，且**不炸**（原先是 `d = None` → TypeError）
        self.assertTrue({"bookSourceName", "bookSourceUrl", "enabled"} <= set(rows[0]), rows[0])

    def test_export_skips_disabled_sources(self) -> None:
        """`enabled` 的权威在**源 JSON 里**（列是 `upsert_sources` 从它写下来的，
        界面的开关也走 `/save` 改 JSON）——所以这里按真实路径改，不是改列。"""
        with Store(self.db) as st:
            st.upsert_sources([{"bookSourceName": "普通源", "bookSourceUrl": "https://b.com",
                                "enabled": False}], allow_new_tags=True)
        with Store(self.db) as st:
            rows = json.loads(pathlib.Path(jvm_api._export_sources_file(st))
                              .read_text(encoding="utf-8"))
        self.assertEqual(sorted(r["bookSourceUrl"] for r in rows),
                         ["https://A.com/", "https://c.com/"])


class RunDirRetentionTests(_Base):
    """运行目录的保留策略：失败保留现场（上限修剪），成功/取消清理。

    manifest.json / runtime-snapshot.json / results.jsonl 是「重启或异常退出后
    逐项对出这次用了什么、写到哪」的复查交付物；SQLite 仍是任务事实源。
    """

    def _latest_batch_dir(self) -> pathlib.Path:
        runs = self.probe / "data" / "app_probe" / "runs"
        dirs = sorted(runs.glob("batch-*"), key=lambda p: p.stat().st_mtime)
        self.assertTrue(dirs, "没有运行目录")
        return dirs[-1]

    def test_failed_run_keeps_manifest_snapshot_and_results(self) -> None:
        dump_file = self.probe / "data" / "app_probe" / "test_jvm_env.json"
        dump_file.parent.mkdir(parents=True, exist_ok=True)
        dump_file.write_text(json.dumps({"workingDir": "X:/repo"}), encoding="utf-8")
        self.no_output = True
        self.gradle_result = 1
        result = self._call()
        self.assertFalse(result["ok"])
        run_dir = self._latest_batch_dir()
        envelope = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(envelope["job_id"], "testjob")
        self.assertEqual(envelope["chunk"], "")
        self.assertEqual(envelope["retry_of"], "")
        self.assertIn("owner_pid", envelope)
        inputs = envelope["inputs"]
        self.assertEqual(len(inputs["urls"]), inputs["source_count"])
        self.assertTrue(all("://" in u for u in inputs["urls"]))
        self.assertIn("keyword", inputs["params"])
        body = dict(inputs)
        expected = body.pop("sha256")
        self.assertEqual(hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest(), expected)
        self.assertTrue((run_dir / "runtime-snapshot.json").exists())
        # 这个失败场景是「Gradle 没产出结果文件」——参数文件必须在现场，
        # results.jsonl 反倒不该有
        self.assertTrue((run_dir / "args.properties").exists())
        self.assertFalse((run_dir / "results.jsonl").exists())

    def test_retry_of_is_recorded_in_the_manifest(self) -> None:
        """重试天然生成新运行目录（uuid 命名），旧产物不会被覆盖；retry_of 记录它替代谁。"""
        self.no_output = True
        self.gradle_result = 1
        with Store(self.db) as st:
            st.create_job("testjob", "jvm_run", total=3, retry_of="orig-job")
        self._call()
        envelope = json.loads(
            (self._latest_batch_dir() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(envelope["retry_of"], "orig-job")

    def test_run_dirs_follow_their_job_row(self) -> None:
        """运行目录的寿命跟着**任务行**走：行没了才删，行还在（含在跑的）不许删。

        时间线读的就是目录里的两本账：按"目录个数上限"删，会让任务列表里那条的
        时间线凭空空掉；而没有任务行的目录在界面上根本没有入口，按 mtime 超期删。
        """
        runs = self.probe / "data" / "app_probe" / "runs"
        dead = runs / "batch-dead"
        alive = runs / "batch-alive"
        orphan = runs / "batch-orphan"
        for d, job_id in ((dead, "gone"), (alive, "alive"), (orphan, "")):
            d.mkdir(parents=True, exist_ok=True)
            if job_id:
                (d / "manifest.json").write_text(
                    json.dumps({"job_id": job_id}), encoding="utf-8")
        with Store(self.db) as st:
            st.create_job("alive", "jvm_run")

        self.assertEqual(jobs_runner.sweep_run_dirs(), 1)

        self.assertFalse(dead.exists(), "任务行没了的目录要删")
        self.assertTrue(alive.exists(), "任务行还在：时间线还要读它")
        self.assertTrue(orphan.exists(), "没有任务行、又是刚建的：留出余量")


class ChunkExecutionTests(_Base):
    """批量分块：一块一次 JVM 占用，块间交还调度权，失败中止余下块，重试跳过已完成块。"""

    _CHUNK_SETTINGS = {"network": {"proxy": ""},
                       "jvm": {"app_repo": "X:/repo", "keyword": "我", "timeout": 25,
                               "concurrency": 8, "depth": "search",
                               "chunk_size": 1}}

    def test_batch_runs_one_daemon_call_per_chunk(self) -> None:
        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNK_SETTINGS)):
            result = self._call()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.gradle_calls, 0)
        self.assertEqual(self.daemon_calls, 3)
        self.assertEqual(len(result["chunk_reports"]), 3)
        self.assertEqual(result["count"], 3)

    def test_chunk_failure_aborts_remaining_chunks(self) -> None:
        calls = {"n": 0}

        def flaky_daemon(dump, args_path, socket_timeout=None, owner=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"code": 1, "error": "boom"}
            return self._fake_daemon(dump, args_path, socket_timeout=socket_timeout)

        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNK_SETTINGS)), \
             mock.patch("core.jvm_validate_daemon.run", side_effect=flaky_daemon):
            result = self._call()
        self.assertFalse(result["ok"])
        self.assertIn("第 2/3 块失败", result["reason"])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.gradle_calls, 0)
        self.assertFalse(result["chunk_reports"][1]["ok"])

    def test_retry_skips_completed_chunks(self) -> None:
        """重试恢复：DONE 标记在的块不重跑，只补失败块——不重复请求站点。"""
        submitted = {}

        def capture_submit(kind, payload, lane=None):
            submitted["payload"] = payload
            return "resume-job"

        calls = {"n": 0}

        def flaky_daemon(dump, args_path, socket_timeout=None, owner=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"code": 1, "error": "boom"}
            return self._fake_daemon(dump, args_path, socket_timeout=socket_timeout)

        async def go():
            with mock.patch.object(jvm_api, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_exec, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_api.runner, "submit",
                                   side_effect=capture_submit),                  mock.patch.object(jvm_api.settings_store, "load",
                                   lambda: dict(self._CHUNK_SETTINGS)),                 mock.patch("core.jvm_health.store_checks",
                            lambda *a, **kw: 0),                  mock.patch("core.jvm_validate_daemon.run",
                                   side_effect=flaky_daemon):
                await jvm_api.jvm_run(
                    JvmRunRequest(urls=["https://a.com", "https://b.com"]))
                return await jvm_exec.run_jvm_job(
                    "resume-job", Store(self.db), submitted["payload"])

        asyncio.run(go())                     # 第一轮：第 2 块失败，目录保留
        saved_payload = submitted["payload"]
        calls["n"] = 0

        async def retry():
            with mock.patch.object(jvm_api, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_exec, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_api.settings_store, "load",
                                   lambda: dict(self._CHUNK_SETTINGS)),                 mock.patch("core.jvm_health.store_checks",
                            lambda *a, **kw: 0),                  mock.patch("core.jvm_validate_daemon.run",
                                   side_effect=flaky_daemon):
                return await jvm_exec.run_jvm_job(
                    "resume-job-2", Store(self.db), saved_payload)

        result = asyncio.run(retry())
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls["n"], 1)       # 只有第 2 块真跑了
        resumed = [c for c in result["chunk_reports"] if c.get("resumed")]
        self.assertEqual(len(resumed), 1)



class GradleLogTests(unittest.TestCase):
    """真 `_run_gradle`（不走 _Base 的打桩）：全量 stdout/stderr 要落进运行目录。"""

    def test_stdout_stderr_logs_are_written_into_run_dir(self) -> None:
        root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_gradle_log_"))
        self.addCleanup(shutil.rmtree, str(root), ignore_errors=True)
        run_dir = root / "app_probe" / "runs" / "logged"
        run_dir.mkdir(parents=True)
        args_path = run_dir / "args.properties"
        args_path.write_text("file=x\n", encoding="utf-8")
        proc = mock.Mock(returncode=1)
        proc.communicate.return_value = ("gradle 全量输出", "boom")
        with mock.patch.object(jvm_exec, "_launcher",
                               return_value=root / "appservice" / "legado-gradle.bat"), \
             mock.patch.object(jvm_exec, "_AGSVC", root / "appservice"), \
             mock.patch.object(jvm_exec, "data_dir", lambda: root), \
             mock.patch.object(jvm_exec.subprocess, "Popen", return_value=proc):
            result = jvm_exec._run_gradle(args_path=args_path, runtime={
                "app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk",
                "gradle_user_home": "X:/.gradle"})
        self.assertEqual(result["exit"], 1)
        self.assertEqual((run_dir / "stdout.log").read_text(encoding="utf-8"),
                         "gradle 全量输出")
        self.assertEqual((run_dir / "stderr.log").read_text(encoding="utf-8"), "boom")


class BatchDaemonTests(_Base):
    """块级 daemon 路径与批前准备。

    钉住批量层的编排：每批至多 prepare 一次、失败/忙带原因回落、
    恢复批按待跑块决定是否准备。
    """

    _DUMP = {"workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
             "environment": {}, "jvmArgs": [], "systemProperties": {},
             "javaHomeEnv": "C:/jdk"}

    _CHUNKED = {"network": {"proxy": ""},
                "jvm": {"app_repo": "X:/repo", "keyword": "我", "timeout": 25,
                        "concurrency": 8, "depth": "search",
                        "chunk_size": 1}}

    def _fake_run_writing_results(self, calls: list):
        def fake_run(_dump, args_file, socket_timeout=None, owner=None):
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            src = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("file=")))
            calls["socket_timeout"] = socket_timeout
            calls["sources"] = len(json.loads(src.read_text(encoding="utf-8")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}
        return fake_run

    def test_daemon_prepare_failures_fail_batch_without_gradle(self) -> None:
        cases = (
            ("busy", {"outcome": "busy", "reason": "daemon busy"}),
            ("stale", {"outcome": "failed", "reason": "runtime snapshot stale"}),
            ("exception", RuntimeError("prepare crashed")),
        )
        for name, outcome in cases:
            with self.subTest(name=name):
                if isinstance(outcome, Exception):
                    prepare = mock.patch("core.jvm_validate_daemon.prepare",
                                         side_effect=outcome)
                    expected = str(outcome)
                else:
                    prepare = mock.patch("core.jvm_validate_daemon.prepare",
                                         return_value=outcome)
                    expected = outcome["reason"]
                with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
                     prepare, \
                     mock.patch.object(jvm_exec, "_run_gradle") as gradle:
                    result = self._call()
                self.assertFalse(result["ok"], result)
                gradle.assert_not_called()
                self.assertEqual(result["gradle_chunks"], 0)
                self.assertIn(expected, result["reason"])



    def test_batch_uses_daemon(self) -> None:
        calls: dict = {}
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "ready",
                                      "info": {"pid": 1, "port": 9999, "sig": "s"}}) as prepare, \
            mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 1, "port": 9999, "sig": "s"}), \
            mock.patch("core.jvm_validate_daemon.run",
                        side_effect=self._fake_run_writing_results(calls)):
            result = self._call()
        prepare.assert_called_once()
        self.assertEqual(self.gradle_calls, 0, "daemon 成功时本块不得再碰 Gradle")
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(result["daemon_chunks"], 1)
        self.assertEqual(result["gradle_chunks"], 0)
        self.assertEqual(result["daemon_prepare"],
                         {"outcome": "ready", "reason": ""})
        report = result["chunk_reports"][0]
        self.assertEqual(report["execution_mode"], "validate_daemon")
        self.assertEqual(report.get("daemon_failure") or "", "")
        # socket 等待按块规模缩放：每源预算(25) × 块源数 + 60
        self.assertEqual(calls["socket_timeout"], 25 * calls["sources"] + 60)
        # 块墙钟落进报告；数值随环境漂，只钉存在与非负
        self.assertGreaterEqual(report.get("cost_sec", -1), 0)

    def test_batch_daemon_error_is_reported_without_gradle(self) -> None:
        calls: dict = {}
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
             mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "ready",
                                      "info": {"pid": 1, "port": 9999, "sig": "s"}}), \
             mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 1, "port": 9999, "sig": "s"}), \
             mock.patch("core.jvm_validate_daemon.run",
                        return_value={"code": 2, "cost_ms": 3, "error": "daemon 内部错误"}), \
             mock.patch.object(jvm_exec, "_run_gradle") as gradle:
            result = self._call()
        gradle.assert_not_called()
        report = result["chunk_reports"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(report["execution_mode"], "validate_daemon")
        self.assertIn("code=2", report["daemon_failure"])
        self.assertIn("daemon 内部错误", report["daemon_failure"])
        self.assertEqual(result["gradle_chunks"], 0)
        self.assertEqual(calls, {})

    def test_batch_cold_start_prepares_once_and_reuses_across_chunks(self) -> None:
        """冷启动多块：prepare 恰一次（每批至多一次），后续块全部复用 daemon，
        一块都不落 Gradle。"""
        calls: dict = {}
        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNKED)), \
            mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "started",
                                      "info": {"pid": 7, "port": 7777, "sig": "s"}}) as prepare, \
            mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 7, "port": 7777, "sig": "s"}), \
            mock.patch("core.jvm_validate_daemon.run",
                        side_effect=self._fake_run_writing_results(calls)):
            result = self._call()
        prepare.assert_called_once()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.gradle_calls, 0)
        self.assertEqual(len(result["chunk_reports"]), 3)
        self.assertEqual(result["daemon_chunks"], 3)
        self.assertEqual(result["gradle_chunks"], 0)
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertEqual(result["daemon_prepare"], {"outcome": "started", "reason": ""})

    def _resume_harness(self):
        """重试恢复的共享桩具：daemon 第 2 次调用失败；第 1 块已完成并保留。"""
        import contextlib

        submitted = {}
        state = {"daemon": 0}

        def capture_submit(kind, payload, lane=None):
            submitted["payload"] = payload
            return "resume-job"

        def fake_run(_dump, args_file, socket_timeout=None, owner=None):
            state["daemon"] += 1
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            if state["daemon"] == 2:
                return {"code": 2, "cost_ms": 3, "error": "daemon 内部错误"}
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        def _started():
            return {"outcome": "started",
                    "info": {"pid": 7, "port": 7777, "sig": "s"}}

        def patches(prepare_mock):
            return (mock.patch.object(jvm_api, "Store",
                                      lambda *a, **kw: Store(self.db)),
                    mock.patch.object(jvm_api.runner, "submit",
                                      side_effect=capture_submit),
                    mock.patch.object(jvm_api.settings_store, "load",
                                      lambda: dict(self._CHUNKED)),
                   mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0),
                  mock.patch("core.jvm_direct.load_dump",
                               return_value=self._DUMP),
                   mock.patch("core.jvm_validate_daemon.prepare", prepare_mock),
                   mock.patch("core.jvm_validate_daemon.probe",
                               return_value={"pid": 7, "port": 7777, "sig": "s"}),
                   mock.patch("core.jvm_validate_daemon.run",
                               side_effect=fake_run))



        def _run(prepare_mock, body):
            with contextlib.ExitStack() as stk:
                for p in patches(prepare_mock):
                    stk.enter_context(p)
                return asyncio.run(body())

        def submit_and_run(prepare_mock):
            async def go():
                await jvm_api.jvm_run(
                    JvmRunRequest(urls=["https://a.com", "https://b.com"]))
                return await jvm_exec.run_jvm_job("resume-job", Store(self.db),
                                                  submitted["payload"])
            return _run(prepare_mock, go)

        def run_only(prepare_mock, job_id, payload):
            async def go():
                return await jvm_exec.run_jvm_job(job_id, Store(self.db), payload)
            return _run(prepare_mock, go)

        return submitted, state, _started, submit_and_run, run_only

    def test_batch_resume_prepares_once_per_run(self) -> None:
        """第 1 轮块 2 失败中止；第 2 轮只补块 2——每轮至多准备一次。"""
        submitted, state, _started, submit_and_run, run_only = self._resume_harness()
        prepare1 = mock.MagicMock(return_value=_started())
        r1 = submit_and_run(prepare1)
        self.assertFalse(r1["ok"])
        self.assertIn("第 2/2 块失败", r1["reason"])
        self.assertEqual(prepare1.call_count, 1)
        self.assertEqual(state["daemon"], 2)

        # 第 2 轮（重试）：块 1 DONE 跳过；块 2 待跑 → 本轮准备一次、跑成
        prepare2 = mock.MagicMock(return_value=_started())
        r2 = run_only(prepare2, "resume-job-2", submitted["payload"])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(prepare2.call_count, 1, "恢复批对唯一待跑块仍只准备一次")
        self.assertEqual(len([c for c in r2["chunk_reports"] if c.get("resumed")]), 1)
        self.assertEqual(state["daemon"], 3)

    def test_batch_all_chunks_done_skips_prepare(self) -> None:
        """全部块 DONE 的恢复批（成功清场后手工补齐的形态）：一次都不准备，
        结果里也没有 daemon_prepare 键。"""
        submitted, _state, _started, submit_and_run, run_only = self._resume_harness()
        prepare1 = mock.MagicMock(return_value=_started())
        r1 = submit_and_run(prepare1)
        self.assertFalse(r1["ok"], r1)
        # 成功会清运行目录，这里靠第 1 轮的失败保留它，再把块 2 补成已完成
        run_dir = pathlib.Path(submitted["payload"]["manifest"]["run_dir"])
        (run_dir / "chunk-02" / "DONE").write_text("", encoding="utf-8")
        (run_dir / "chunk-02" / "results.jsonl").write_text(
            json.dumps({"url": "https://b.com", "state": "ok"}), encoding="utf-8")
        prepare2 = mock.MagicMock()
        r2 = run_only(prepare2, "resume-job-2", submitted["payload"])
        self.assertTrue(r2["ok"], r2)
        prepare2.assert_not_called()
        self.assertNotIn("daemon_prepare", r2)
        self.assertEqual(len([c for c in r2["chunk_reports"] if c.get("resumed")]), 2)


class EventTimelineTests(_Base):
    """骨架事件流（jvm-batch-timeline 生产者）：events.jsonl 的行序与终态合并。

    行号即游标，**消费契约**钉在 tests.test_job_timeline；这里钉生产侧：
    什么节点必须出现什么事件、恢复/中止/忙重试批的形状、result["events"]
    有界合并。事件是辅助证据，所以全部走 daemon 关/桩的快路径。
    """

    _QUIET = {"network": {"proxy": ""},
              "jvm": {"app_repo": "X:/repo", "keyword": "我", "timeout": 25,
                      "concurrency": 8, "depth": "search",
                      "chunk_size": 1}}

    _BUSY3 = {"network": {"proxy": ""},
              "jvm": {"app_repo": "X:/repo", "keyword": "我", "timeout": 25,
                      "concurrency": 8, "depth": "search",
                      "chunk_size": 1}}

    _DUMP = {"workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
             "environment": {}, "jvmArgs": [], "systemProperties": {},
             "javaHomeEnv": "C:/jdk"}

    def _run_batch(self, settings=None, urls=None):
        import contextlib

        submitted = {}

        def capture(kind, payload, lane=None):
            submitted["payload"] = payload
            return "ev-job"

        async def go():
            with contextlib.ExitStack() as stk:
                for p in (mock.patch.object(jvm_api, "Store",
                                            lambda *a, **kw: Store(self.db)),
                          mock.patch.object(jvm_api.runner, "submit",
                                            side_effect=capture),
                         mock.patch("core.jvm_health.store_checks",
                                     lambda *a, **kw: 0)):
                    stk.enter_context(p)
                if settings is not None:
                    stk.enter_context(mock.patch.object(
                        jvm_api.settings_store, "load",
                        lambda: dict(settings)))
                await jvm_api.jvm_run(
                    JvmRunRequest(urls=urls if urls is not None else []))
                result = await jvm_exec.run_jvm_job("ev-job", Store(self.db),
                                                    submitted["payload"])
            return submitted["payload"], result

        return asyncio.run(go())

    @staticmethod
    def _events(run_dir) -> list:
        """运行目录里的事件。**事实在文件里**（不再合并进 ``result_json``）：
        成功清场只清执行产物，两本账留着。"""
        text = (pathlib.Path(run_dir) / "events.jsonl").read_text(
            encoding="utf-8")
        return [json.loads(l) for l in text.splitlines() if l.strip()]

    @staticmethod
    def _kinds(run_dir) -> list:
        return [e["kind"] for e in EventTimelineTests._events(run_dir)]

    @staticmethod
    def _run_dir(payload) -> str:
        return payload["manifest"]["run_dir"]

    def test_queue_wait_is_not_reported_when_nothing_queued(self) -> None:
        """没排队就不发那行：每次跑都来一条「任务排队完成（0 秒）」是噪声。"""
        payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        events = self._events(self._run_dir(payload))
        self.assertEqual([], [e for e in events if e.get("stage") == "queue_wait"],
                         events)

    def test_queue_wait_is_reported_when_the_lane_makes_it_wait(self) -> None:
        """真排队时必须留下那一行，且秒数来自 lane 的实际等待。"""

        class _SlowFirstLane:
            def __init__(self) -> None:
                self.calls = 0

            async def acquire(self, kind, job_id=None):
                if self.calls == 0:
                    await asyncio.sleep(jvm_exec._QUEUE_NOTICE_MIN_SEC + 0.2)
                self.calls += 1

            def release(self) -> None:
                return None

        lane_stub = _SlowFirstLane()
        with mock.patch.object(jvm_exec.runner, "lane", lambda name: lane_stub):
            payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        events = self._events(self._run_dir(payload))
        queue = [e for e in events if e.get("stage") == "queue_wait"]
        self.assertEqual(1, len(queue), events)
        self.assertGreaterEqual(queue[0]["cost_sec"], jvm_exec._QUEUE_NOTICE_MIN_SEC)

    def test_batch_reports_engine_wait_while_daemon_busy(self) -> None:
        """等待引擎空闲要写进时间线，窗口用实测上界（2s 已证明必然白等）。"""
        seen = {}

        def fake_prepare(dump, **kwargs):
            seen["busy_wait_sec"] = kwargs.get("busy_wait_sec")
            if kwargs.get("on_wait"):
                kwargs["on_wait"](10.0)
            return {"outcome": "busy", "reason": "daemon 忙"}

        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        side_effect=fake_prepare), \
            mock.patch("core.jvm_validate_daemon.probe", return_value=None):
            payload, result = self._run_batch(self._QUIET)
        self.assertEqual(jvm_exec._DAEMON_BUSY_WAIT_SEC, seen["busy_wait_sec"])
        events = self._events(self._run_dir(payload))
        waits = [e for e in events if e.get("kind") == "waiting_engine"]
        self.assertEqual(1, len(waits), events)
        self.assertEqual(10.0, waits[0]["elapsed_sec"])

    def test_stalled_chunk_survives_successful_isolation(self) -> None:
        """停滞块的隔离重跑成功后，这一块必须算成功、余下块继续跑。

        2026-10-09 实测（相隔一小时的**两批**都死在这里）：隔离已把结论并回块文件，
        块却被判「没有产出结果文件」——因为块级退出码还是隔离之前那次 Gradle 尝试的。
        后果不是少几条结论：那一块的全部结论被丢，整批按「环境级失败」中止（3279 条只跑
        了 825 条、2454 条只跑了 25 条）。
        """
        from core import jvm_validate_daemon

        prepare_calls = {"n": 0}

        def fake_prepare(dump, **kwargs):
            prepare_calls["n"] += 1
            return {"outcome": "started", "info": {"pid": 1, "port": 1, "sig": "s"}}

        stalled = threading.Event()

        def force_stall(watch_path, stall_sec, stop, abort, interval,
                        is_cancelled, why):
            why["stalled"] = True
            stalled.set()
            stop.set()

        run_calls = {"n": 0}

        def fake_run(_dump, args_file, socket_timeout=None, owner=None):
            run_calls["n"] += 1
            if run_calls["n"] == 1:
                stalled.wait(1)
                raise jvm_validate_daemon.ValidateDaemonError("daemon op stalled")
            if run_calls["n"] == 2:
                raise jvm_validate_daemon.ValidateDaemonError("引擎对该源无响应")
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            src = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("file=")))
            row = json.loads(src.read_text(encoding="utf-8"))[0]
            out.write_text(json.dumps({"url": row["bookSourceUrl"], "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3}

        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
             mock.patch("core.jvm_validate_daemon.prepare", side_effect=fake_prepare), \
             mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 1, "port": 1, "sig": "s"}), \
             mock.patch("core.jvm_validate_daemon.stop", return_value=True), \
             mock.patch("core.jvm_validate_daemon.run", side_effect=fake_run), \
             mock.patch.object(jvm_exec, "_watch_output_stall", side_effect=force_stall):
            payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        self.assertEqual(3, len(result["chunk_reports"]), result["chunk_reports"])
        self.assertTrue(all(r.get("ok") for r in result["chunk_reports"]),
                        result["chunk_reports"])
        # 每块 1 条源：判死的那条也要作为结论留在文件里被读回，不能被整块丢掉
        self.assertEqual([1, 1, 1],
                         [r.get("count") for r in result["chunk_reports"]])
        self.assertEqual("done", self._kinds(self._run_dir(payload))[-1])

    def test_batch_stale_snapshot_fails_without_daemon_or_gradle(self) -> None:
        gate = {"ok": False,
                "reason": "snapshot 早于 appservice Kotlin 源码，请先刷新"}
        with mock.patch.object(jvm_exec, "execution_readiness", lambda dump=None: gate), \
             mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
             mock.patch("core.jvm_validate_daemon.prepare") as prepare, \
             mock.patch.object(jvm_exec, "_run_gradle") as gradle:
            _payload, result = self._run_batch(self._QUIET)
        self.assertFalse(result["ok"], result)
        prepare.assert_not_called()
        gradle.assert_not_called()
        self.assertIn("刷新", result["reason"])
        self.assertEqual(result["gradle_chunks"], 0)

    def test_batch_without_manifest_is_rejected_with_reason(self) -> None:
        """无 manifest 的批量调用要给可读失败，而不是块线程里的 KeyError。"""
        payload = {"prep": {"started": True}, "single": False,
                   "run_dir": str(self.probe / "data" / "app_probe" / "runs" / "nomanifest")}
        with mock.patch.object(jvm_exec, "_run_gradle") as gradle:
            result = asyncio.run(jvm_exec.run_jvm_job(
                "nomanifest-job", Store(self.db), payload))
        self.assertFalse(result["ok"])
        self.assertIn("manifest", result["reason"])
        gradle.assert_not_called()

    def test_batch_event_sequence_and_terminal_merge(self) -> None:
        """成功批只清执行产物：事件留在运行目录里，跑完也读得到（时间线的事实源）。"""
        payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._kinds(self._run_dir(payload)),
                         ["batch_started", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_done", "chunk_started", "chunk_done",
                          "done"])

    def test_busy_daemon_failure_events(self) -> None:
        """daemon busy：保留 prepare 原因，当前块失败并结束批次。"""

        def fake_run(_dump, args_file, socket_timeout=None, owner=None):
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "busy", "reason": "daemon 忙"}), \
            mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 7, "port": 7777, "sig": "s"}), \
            mock.patch("core.jvm_validate_daemon.run", side_effect=fake_run):
            _payload, result = self._run_batch(self._BUSY3)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self._kinds(_payload["manifest"]["run_dir"]),
                         ["batch_started", "prepare", "chunk_started",
                          "chunk_failed", "failed"])
        self.assertEqual(result["gradle_chunks"], 0)

    def test_resume_and_abort_events(self) -> None:
        """中止批以 failed 收尾；恢复批以 resumed 开场、done 收尾。"""
        import contextlib

        gradle_calls = {"n": 0}

        def flaky_daemon(_dump, args_file, socket_timeout=None, owner=None):
            gradle_calls["n"] += 1
            if gradle_calls["n"] == 2:
                return {"code": 1, "error": "boom"}
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            src = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("file=")))
            url = json.loads(src.read_text(encoding="utf-8"))[0]["bookSourceUrl"]
            out.write_text(json.dumps({"url": url, "state": "ok"}), encoding="utf-8")
            return {"code": 0, "cost_ms": 1}

        submitted = {}

        def capture(kind, payload, lane=None):
            submitted["payload"] = payload
            return "ev-job"

        def patches():
            return [mock.patch.object(jvm_api, "Store",
                                      lambda *a, **kw: Store(self.db)),
                    mock.patch.object(jvm_api.runner, "submit",
                                      side_effect=capture),
                    mock.patch.object(jvm_api.settings_store, "load",
                                      lambda: dict(self._QUIET)),
                   mock.patch("core.jvm_health.store_checks",
                               lambda *a, **kw: 0),
                    mock.patch("core.jvm_validate_daemon.run",
                               side_effect=flaky_daemon)]

        with contextlib.ExitStack() as stk:
            for p in patches():
                stk.enter_context(p)
            asyncio.run(jvm_api.jvm_run(
                JvmRunRequest(urls=["https://a.com", "https://b.com"])))
            r1 = asyncio.run(jvm_exec.run_jvm_job(
                "ev-job", Store(self.db), submitted["payload"]))
        run_dir = submitted["payload"]["manifest"]["run_dir"]
        self.assertEqual(self._kinds(run_dir),
                         ["batch_started", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_failed", "failed"])
        self.assertFalse(r1["ok"])

        retry_id = "ev-job-2"
        retry_timeline = pathlib.Path(run_dir) / "attempts" / retry_id
        retry_payload = dict(submitted["payload"], timeline_dir=str(retry_timeline))
        with contextlib.ExitStack() as stk:
            for p in patches():
                stk.enter_context(p)
            r2 = asyncio.run(jvm_exec.run_jvm_job(
                retry_id, Store(self.db), retry_payload))
        self.assertTrue(r2["ok"], r2)
        # 执行块从共享目录恢复，观测账本按 job 隔离：新时间线用 resumed 表示跳过旧 DONE 块。
        self.assertEqual(self._kinds(run_dir),
                         ["batch_started", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_failed", "failed"])
        self.assertEqual(self._kinds(retry_timeline),
                         ["batch_started", "resumed", "prepare", "chunk_started",
                          "chunk_done", "done"])

    def test_success_trim_keeps_the_two_accounts(self) -> None:
        """成功清场只清**执行产物**：DONE/results 留着会让「再次运行」跳过所有块。"""
        run_dir = self.probe / "data" / "app_probe" / "runs" / "batch-trim"
        (run_dir / "chunk-01").mkdir(parents=True)
        (run_dir / "chunk-01" / "DONE").write_text("", encoding="utf-8")
        (run_dir / "chunk-01" / "results.jsonl").write_text("{}\n", encoding="utf-8")
        (run_dir / "sources.json").write_text("[]", encoding="utf-8")
        jvm_exec._append_event(run_dir, "batch_started", chunks=1)
        jvm_exec._record_failures(run_dir, [{"url": "https://b.com", "state": "error"}],
                                  "chunk-01")

        jvm_exec._trim_run_dir(run_dir)

        self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                         ["events.jsonl", "failures.jsonl"])


if __name__ == "__main__":
    unittest.main()

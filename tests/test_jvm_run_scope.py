# -*- coding: utf-8 -*-
"""跑批的范围：**只跑选中的那几条**（S5-A 第二期）。

守三件事，都是「不报错但结果不对」的那一类：

1. **两侧都要归一 URL**（AGENTS #5）：前端给的是库里归一化过的 `source_url`，而导出的是
   源 JSON 里 `bookSourceUrl` 的**原文**——不归一就一条都匹配不上，而表现是「跑完了但
   JVM 列没变」，谁也不报错。
2. **一条都没匹配上要明说**：那种情况下开跑一次空批，界面上会说「完成：0 条结论已入库」，
   读起来像「这些源没问题」。
3. **选了具体几条就不看条数上限**：否则「选了 20 条只跑了 3 条」是一次看不出来的截断。

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
import unittest
import uuid
from unittest import mock

from backend.api import jvm as jvm_api
from backend.schemas import JvmRunRequest
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
        self.runtime_seen = None
        self.gradle_result = 0
        self.no_output = False
        for p in (
            mock.patch.object(jvm_api, "_AGSVC", self.probe / "appservice"),
            mock.patch.object(jvm_api, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jvm_api, "readiness", lambda repo, sdk="": {"ok": True, "checks": [], "runtime": {"app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk", "gradle_user_home": "X:/.gradle"}}),
            mock.patch.object(jvm_api, "execution_readiness", lambda dump=None: {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}),
            mock.patch.object(jvm_api, "_write_meta", lambda rows: "testbatch"),
            mock.patch.object(jvm_api, "_run_gradle", self._fake_gradle),
            mock.patch.object(jvm_api.settings_store, "load",
                              lambda: {"network": {"proxy": ""}, "jvm": {"app_repo": "X:/repo", "keyword": "我",
                                               "timeout": 25, "concurrency": 8,
                                               "limit": 2, "depth": "search"}}),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.batch_seen = []
        self.args_seen = ""
        self._put_source("https://A.com/", "大写 + 尾斜杠的原文")
        self._put_source("https://b.com", "普通源")
        self._put_source("https://c.com/", "另一条")

    def _fake_gradle(self, args_path=None, runtime=None) -> int:
        self.runtime_seen = runtime
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

    def _put_source(self, url: str, name: str) -> None:
        # 用真 API 写源（`upsert_sources` 会建表+算 fingerprint）：比手写 INSERT 更接近
        # 生产路径，也不会因为漏 NOT NULL 列而炸
        with Store(self.db) as st:
            st.upsert_sources([{"bookSourceName": name, "bookSourceUrl": url,
                                "enabled": True}])

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
                got = await jvm_api.run_jvm_job("testjob", Store(self.db), submitted["payload"])
                return dict(r, **got)
            return r

        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)),              mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0):
            return asyncio.run(go())

    def _batch(self) -> list:
        return self.batch_seen


class ScopeTests(_Base):
    def test_legacy_payload_without_manifest_keeps_its_run_paths(self) -> None:
        run_dir = self.probe / "data" / "app_probe" / "runs" / "legacy"
        run_dir.mkdir(parents=True)
        source_file = run_dir / "sources.json"
        args_file = run_dir / "args.properties"
        out_path = run_dir / "results.jsonl"
        source_file.write_text("[]", encoding="utf-8")
        args_file.write_text("file=%s\nout=%s\n" % (source_file, out_path), encoding="utf-8")

        payload = {
            "prep": {"started": True},
            "run_dir": str(run_dir),
            "source_file": str(source_file),
            "args_file": str(args_file),
            "out_path": str(out_path),
            "single": True,
            "allow_gradle_fallback": False,
            "execution_readiness": {"ok": True},
            "readiness": {"ok": False},
        }

        async def go():
            with mock.patch("core.jvm_direct.load_dump", return_value={
                    "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                    "environment": {}, "jvmArgs": [], "systemProperties": {},
                    "javaHomeEnv": "C:/jdk"}), \
                 mock.patch("core.jvm_validate_daemon.run", side_effect=lambda _dump, _args: {
                     "code": 0, "cost_ms": 1, "error": ""}), \
                 mock.patch.object(jvm_api, "execution_readiness", return_value={"ok": True}), \
                 mock.patch.object(jvm_api, "_read_results", return_value=[]), \
                 mock.patch.object(jvm_api, "_write_meta", return_value="legacy-batch"), \
                 mock.patch("core.jvm_health.store_checks", return_value=0):
                return await jvm_api.run_jvm_job("legacy-job", Store(self.db), payload)

        result = asyncio.run(go())
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertFalse(run_dir.exists())

    async def _cancelled_job(self, payload):
        task = asyncio.create_task(jvm_api.run_jvm_job(
            "cancel-job", Store(self.db), payload))
        started = payload["started"]
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(started.is_set(), "Gradle worker 未启动")
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "取消不应绕过仍在运行的 worker")
        payload["release"].set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_cancel_waits_for_worker_and_cleans_run_dir(self) -> None:
        run_dir = self.probe / "data" / "app_probe" / "runs" / "cancelled"
        run_dir.mkdir(parents=True)
        source_file = run_dir / "sources.json"
        args_file = run_dir / "args.properties"
        out_path = run_dir / "results.jsonl"
        source_file.write_text("[]", encoding="utf-8")
        args_file.write_text("file=%s\nout=%s\n" % (source_file, out_path), encoding="utf-8")
        manifest = jvm_api._build_jvm_manifest(
            run_dir=run_dir, source_file=source_file, args_file=args_file,
            out_path=out_path, single=False, execution_plan="gradle",
            allow_gradle_fallback=True, runtime={"app_repo": "X:/repo"},
            readiness={"ok": True}, execution_readiness={},
            readiness_fingerprint="", readiness_checked_at="", source_count=1)
        control = {"started": threading.Event(), "release": threading.Event()}
        payload = {"prep": {"started": True}, "manifest": manifest,
                   **control}

        def blocking_gradle(*, args_path=None, runtime=None):
            control["started"].set()
            control["release"].wait(2)
            return {"exit": 1, "stdout": "", "stderr": "cancelled"}

        async def go():
            with mock.patch.object(jvm_api, "_run_gradle", side_effect=blocking_gradle):
                await self._cancelled_job(payload)

        asyncio.run(go())
        self.assertFalse(run_dir.exists())
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
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["source_count"], 1)
        self.assertEqual(manifest["execution_plan"], "validate_daemon")
        self.assertEqual(jvm_api._manifest_error(manifest), "")

        tampered = dict(manifest)
        tampered["out_path"] = tampered["out_path"] + ".changed"
        self.assertIn("校验失败", jvm_api._manifest_error(tampered))

        malformed = dict(manifest)
        malformed.pop("args_file")
        body = dict(malformed)
        body.pop("sha256", None)
        malformed["sha256"] = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertIn("结构不受支持", jvm_api._manifest_error(malformed))

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
            result = asyncio.run(jvm_api.run_jvm_job(
                "queued-job", Store(self.db), submitted["payload"]))

        self.assertTrue(result["ok"])
        self.assertEqual(self.runtime_seen, original)

    def test_readiness_environment_drift_does_not_store_result(self) -> None:
        self.gradle_result = {
            "exit": 0,
            "stdout": "",
            "stderr": "",
            "runtime_snapshot": {
                "differences": {
                    "readinessEnvironment": {
                        "JAVA_HOME": {
                            "declared": "X:/jdk",
                            "actual": "Y:/jdk",
                        },
                    },
                },
            },
        }
        result = self._call(urls=["https://a.com"])
        self.assertFalse(result["ok"])
        self.assertIn("实际启动环境", result["reason"])
        self.assertIn("JAVA_HOME", result["reason"])
        self.assertEqual(result["gradle"], self.gradle_result)
        with Store(self.db) as st:
            self.assertNotIn("https://a.com", st.checks_map())

    def test_missing_runtime_snapshot_is_diagnostic_only(self) -> None:
        self.gradle_result = {
            "exit": 0,
            "stdout": "",
            "stderr": "",
            "runtime_snapshot": {"error": "actual snapshot missing"},
        }
        result = self._call(urls=["https://a.com"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["runtime_snapshot"]["error"],
                         "actual snapshot missing")

    def test_selector_matches_despite_case_and_trailing_slash(self) -> None:
        """**归一后才比**：库里那条的原文是 `https://A.com/`（大写 + 尾斜杠），
        而列表页给的是归一化过的 `https://a.com`。不归一的话这里会一条都不跑。"""
        self._call(urls=["https://a.com"])
        got = [s["bookSourceUrl"] for s in self._batch()]
        self.assertEqual(got, ["https://A.com/"], "选中的那条要按原文导出（给 JVM 用的是原文）")

    def test_only_selected_sources_are_exported(self) -> None:
        self._call(urls=["https://b.com", "https://c.com"])
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://b.com", "https://c.com/"])

    def test_selection_ignores_the_limit(self) -> None:
        """设置里条数上限 = 2，但**选中 3 条就得跑 3 条**——
        截断会静默：界面上只会看到「选了 3 条」而实际跑了 2 条。"""
        self._call(urls=["https://a.com", "https://b.com", "https://c.com"])
        self.assertEqual(len(self._batch()), 3)
        self.assertEqual(self.gradle_calls, 1)

    def test_unmatched_selection_is_refused_with_a_reason(self) -> None:
        r = self._call(urls=["https://nope.com"])
        self.assertFalse(r["started"])
        self.assertIn("一条都没匹配上", r["reason"])
        self.assertEqual(self.gradle_calls, 0, "匹配不上就不该开跑（空批会被读成「都没问题」）")

    def test_all_sources_still_honours_the_limit(self) -> None:
        """不选（全量）时条数上限照旧生效——那是给「先跑几条看看」用的安全阀。"""
        r = self._call()
        self.assertTrue(r["started"])
        self.assertEqual(len(self._batch()), 2, "limit=2")

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
            st.save_checks([{"url": u, "health": health,
                             "checked_at": "2026-09-20 10:00:00"}
                            for u in ("https://a.com", "https://b.com", "https://c.com")])

    def test_filter_selects_the_matching_sources(self) -> None:
        self._mark_all("gfw")
        self._call(filt={"health": "gfw"})
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://A.com/", "https://b.com", "https://c.com/"])

    def test_filter_ignores_the_limit(self) -> None:
        """与「选中」同一条：范围既然明确给了，再按条数上限截断就是一次看不出来的截断。"""
        self._mark_all("pending")
        self._call(filt={"health": "pending"})
        self.assertEqual(len(self._batch()), 3, "limit=2 但筛选命中 3 条")

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

        设置里 `limit = 2`（`_Base` 的桩），这次传 `limit = 0` → 不该再截断。
        这条正是"参数该长在动作旁边"的理由：留在设置页时，「全部在用源」会被一个
        看不见的上限悄悄截成 2 条，界面上只显示「全部在用源」。
        """
        self._call(params={"limit": 0})
        self.assertEqual(len(self._batch()), 3, "本次给了 0 就该跑全部")

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
    """跑批结果体与**本地校验那条同形状**（十-2：单条校验切引擎）。

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
            return r, await jvm_api.run_jvm_job("testjob", Store(self.db), submitted["payload"])

        # DNS 交叉验证要打桩：`store_checks` 默认会真去探测（测试不许联网）
        async def _fake_probe(host):
            return "answer", "1.2.3.4"

        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)),                 mock.patch("core.dns_check.probe", _fake_probe):
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
        for key in ("name", "health", "stars", "star_basis",
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
        def fake_daemon(_dump, args_file):
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

        def fake_daemon(_dump, args_file):
            events.append(("daemon", None))
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        with mock.patch.object(jvm_api, "execution_readiness",
                               side_effect=check_execution) as check, \
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

        with mock.patch.object(jvm_api, "execution_readiness",
                               side_effect=check_execution), \
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
        def fake_daemon(_dump, args_file):
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
    也就是说 `/api/jvm/run` 从来没在真库上跑通过（测试把这个函数整个打桩了，而历史上的
    批量是走 CLI + 导出文件那条路）。"""

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


if __name__ == "__main__":
    unittest.main()

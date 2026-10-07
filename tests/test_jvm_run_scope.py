# -*- coding: utf-8 -*-
"""跑批的范围：**只跑选中的那几条**（S5-A 第二期）。

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
        self.runtime_seen = None
        self.gradle_result = 0
        self.no_output = False
        test_limits = dict(jvm_api.settings_store.LIMITS)
        test_limits["jvm_chunk_size"] = (1, 200)
        for p in (
            mock.patch.object(jvm_exec, "_AGSVC", self.probe / "appservice"),
            mock.patch.object(jvm_api, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jvm_exec, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jvm_api, "readiness", lambda repo, sdk="": {"ok": True, "checks": [], "runtime": {"app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk", "gradle_user_home": "X:/.gradle"}}),
            mock.patch.object(jvm_api, "execution_readiness", lambda dump=None: {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}),
            mock.patch.object(jvm_exec, "execution_readiness", lambda dump=None: {"ok": True, "checks": [], "reason": "", "source_sig": "sig"}),
            # 隔离真机状态：机器上可能有真实 runtime snapshot（准备态的产物），
            # 不隔离的话单条任务会走上真 daemon——这批测试原本依赖
            # 「data/ 里没有 dump」这个巧合，snapshot 一存在就整批变红。
            mock.patch.object(jvm_direct, "dump_path",
                              lambda: self.probe / "data" / "app_probe" / "test_jvm_env.json"),
            mock.patch.object(jvm_exec, "_write_meta", lambda rows: "testbatch"),
            mock.patch.object(jvm_api.settings_store, "LIMITS", test_limits),
            mock.patch.object(jvm_exec, "_run_gradle", self._fake_gradle),
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
                got = await jvm_exec.run_jvm_job("testjob", Store(self.db), submitted["payload"])
                return dict(r, **got)
            return r

        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)),              mock.patch.object(jvm_exec, "Store", lambda *a, **kw: Store(self.db)),             mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0):
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
            def fake_daemon(_dump, _args):
                out_path.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                                    encoding="utf-8")
                return {"code": 0, "cost_ms": 1, "error": ""}

            with mock.patch("core.jvm_direct.load_dump", return_value={
                    "workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
                    "environment": {}, "jvmArgs": [], "systemProperties": {},
                    "javaHomeEnv": "C:/jdk"}), \
                 mock.patch("core.jvm_validate_daemon.run", side_effect=fake_daemon), \
                 mock.patch.object(jvm_exec, "execution_readiness", return_value={"ok": True}), \
                 mock.patch.object(jvm_exec, "_read_results", return_value=[]), \
                 mock.patch.object(jvm_exec, "_write_meta", return_value="legacy-batch"), \
                 mock.patch("core.jvm_health.store_checks", return_value=0):
                return await jvm_exec.run_jvm_job("legacy-job", Store(self.db), payload)

        result = asyncio.run(go())
        self.assertEqual(result["execution_mode"], "validate_daemon")
        self.assertFalse(run_dir.exists())

    async def _cancelled_job(self, payload):
        task = asyncio.create_task(jvm_exec.run_jvm_job(
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
            out_path=out_path, single=False, execution_plan="gradle",
            allow_gradle_fallback=True, runtime={"app_repo": "X:/repo"},
            readiness={"ok": True}, execution_readiness={},
            readiness_fingerprint="", readiness_checked_at="", source_count=2,
            urls=["https://a.com", "https://b.com"], params={"keyword": "我", "timeout": 25, "concurrency": 8, "depth": "search"}, chunks=[2])
        control = {"started": threading.Event(), "release": threading.Event()}
        payload = {"prep": {"started": True}, "manifest": manifest,
                   **control}

        def blocking_gradle(*, args_path=None, runtime=None, **kwargs):
            control["started"].set()
            control["release"].wait(2)
            return {"exit": 1, "stdout": "", "stderr": "cancelled"}

        async def go():
            with mock.patch.object(jvm_exec, "_run_gradle", side_effect=blocking_gradle):
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

    def test_selection_is_exported_in_full(self) -> None:
        """选中 3 条就导出 3 条：范围是唯一依据，没有任何上限参与（历史上这里被
        「条数上限」静默截断过——界面上只会看到「选了 3 条」而实际跑了 2 条）。"""
        self._call(urls=["https://a.com", "https://b.com", "https://c.com"])
        self.assertEqual(len(self._batch()), 3)
        self.assertEqual(self.gradle_calls, 1)

    def test_unmatched_selection_is_refused_with_a_reason(self) -> None:
        r = self._call(urls=["https://nope.com"])
        self.assertFalse(r["started"])
        self.assertIn("一条都没匹配上", r["reason"])
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

    def test_failed_run_dirs_are_pruned_to_the_cap(self) -> None:
        from core.jvm_debug import FAILED_RUN_DIR_CAP
        runs = self.probe / "data" / "app_probe" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        for i in range(FAILED_RUN_DIR_CAP + 3):
            (runs / ("batch-%06d" % i)).mkdir()
        self.no_output = True
        self.gradle_result = 1
        self._call()
        self.assertLessEqual(len(list(runs.glob("batch-*"))), FAILED_RUN_DIR_CAP)
        # 保留的必须是最新那个（本次失败现场），而不是任意 cap 个
        self.assertTrue(self._latest_batch_dir().exists())


class ChunkExecutionTests(_Base):
    """批量分块：一块一次 JVM 占用，块间交还调度权，失败中止余下块，重试跳过已完成块。"""

    _CHUNK_SETTINGS = {"network": {"proxy": ""},
                       "jvm": {"app_repo": "X:/repo", "keyword": "我", "timeout": 25,
                               "concurrency": 8, "depth": "search",
                               "chunk_size": 1}}

    def test_batch_runs_one_gradle_call_per_chunk(self) -> None:
        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNK_SETTINGS)):
            result = self._call()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.gradle_calls, 3)
        self.assertEqual(len(result["chunk_reports"]), 3)
        self.assertEqual(result["count"], 3)

    def test_chunk_failure_aborts_remaining_chunks(self) -> None:
        calls = {"n": 0}

        def flaky_gradle(args_path=None, runtime=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"exit": 1, "stdout": "", "stderr": "boom"}
            return self._fake_gradle(args_path=args_path, runtime=runtime)

        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNK_SETTINGS)),              mock.patch.object(jvm_exec, "_run_gradle", side_effect=flaky_gradle):
            result = self._call()
        self.assertFalse(result["ok"])
        self.assertIn("第 2/3 块失败", result["reason"])
        self.assertEqual(calls["n"], 2)
        self.assertFalse(result["chunk_reports"][1]["ok"])

    def test_retry_skips_completed_chunks(self) -> None:
        """重试恢复：DONE 标记在的块不重跑，只补失败块——不重复请求站点。"""
        submitted = {}

        def capture_submit(kind, payload, lane=None):
            submitted["payload"] = payload
            return "resume-job"

        calls = {"n": 0}

        def flaky_gradle(args_path=None, runtime=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"exit": 1, "stdout": "", "stderr": "boom"}
            return self._fake_gradle(args_path=args_path, runtime=runtime)

        async def go():
            with mock.patch.object(jvm_api, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_exec, "Store",
                                   lambda *a, **kw: Store(self.db)),                  mock.patch.object(jvm_api.runner, "submit",
                                   side_effect=capture_submit),                  mock.patch.object(jvm_api.settings_store, "load",
                                   lambda: dict(self._CHUNK_SETTINGS)),                 mock.patch("core.jvm_health.store_checks",
                            lambda *a, **kw: 0),                  mock.patch.object(jvm_exec, "_run_gradle",
                                   side_effect=flaky_gradle):
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
                            lambda *a, **kw: 0),                  mock.patch.object(jvm_exec, "_run_gradle",
                                   side_effect=flaky_gradle):
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
        def fake_run(_dump, args_file, socket_timeout=None):
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

    def test_batch_daemon_busy_falls_back_without_killing_it(self) -> None:
        """prepare 判忙（不杀不启）→ 首块直接回落，下一块最多重试一次；
        批量层全程不触碰 ensure / start / _kill_proc。"""
        busy = {"outcome": "busy", "reason": "daemon 进程还在但 ping 没应答，本批不准备也不杀"}
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare", return_value=busy) as prepare, \
            mock.patch("core.jvm_validate_daemon.probe", return_value=None) as probe, \
            mock.patch("core.jvm_validate_daemon.ensure") as ensure, \
            mock.patch("core.jvm_validate_daemon.start") as start, \
            mock.patch("core.jvm_validate_daemon._kill_proc") as kill:
            result = self._call()
        prepare.assert_called_once()
        probe.assert_not_called()
        ensure.assert_not_called()
        start.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(self.gradle_calls, 1, "回落必须真用 Gradle 跑完本块")
        report = result["chunk_reports"][0]
        self.assertEqual(report["execution_mode"], "gradle")
        self.assertIn("daemon", report.get("daemon_failure") or "")
        self.assertEqual(result["execution_mode"], "gradle_fallback")
        self.assertEqual(result["daemon_prepare"],
                         {"outcome": "busy", "reason": busy["reason"]})

    def test_batch_busy_retries_once_then_reuses_recovered_daemon(self) -> None:
        """批次初始忙：首块直接回落，下一块只重试一次；恢复后余块复用。"""
        calls: dict = {}
        busy = {"outcome": "busy", "reason": "daemon 忙，本批暂不准备"}
        with mock.patch.object(jvm_api.settings_store, "load",
                               lambda: dict(self._CHUNKED)), \
            mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare", return_value=busy), \
            mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 7, "port": 7777, "sig": "s"}) as probe, \
            mock.patch("core.jvm_validate_daemon.run",
                        side_effect=self._fake_run_writing_results(calls)):
            result = self._call()

        self.assertTrue(result["ok"], result)
        probe.assert_has_calls([mock.call(self._DUMP), mock.call(self._DUMP)])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.gradle_calls, 1)
        self.assertEqual(result["daemon_chunks"], 2)
        self.assertEqual(result["gradle_chunks"], 1)
        self.assertEqual(result["chunk_reports"][0]["execution_mode"], "gradle")
        self.assertEqual(result["chunk_reports"][1]["execution_mode"], "validate_daemon")
        self.assertEqual(result["chunk_reports"][2]["execution_mode"], "validate_daemon")
    def test_batch_daemon_error_code_falls_back_with_reason(self) -> None:
        """daemon 应答 code!=0 → 回落 Gradle，原因逐字带到块报告；
        daemon 的半成品结果不得冒充结论。"""
        calls: dict = {}
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "ready",
                                      "info": {"pid": 1, "port": 9999, "sig": "s"}}), \
            mock.patch("core.jvm_validate_daemon.probe",
                        return_value={"pid": 1, "port": 9999, "sig": "s"}), \
            mock.patch("core.jvm_validate_daemon.run",
                        side_effect=lambda d, a, socket_timeout=None:
                            {"code": 2, "cost_ms": 3, "error": "daemon 内部错误"}):
            result = self._call()
        self.assertEqual(self.gradle_calls, 1)
        report = result["chunk_reports"][0]
        self.assertEqual(report["execution_mode"], "gradle")
        self.assertIn("code=2", report.get("daemon_failure") or "")
        self.assertIn("daemon 内部错误", report.get("daemon_failure") or "")
        self.assertEqual(result["daemon_chunks"], 0)
        self.assertEqual(result["gradle_chunks"], 1)
        # 回落路径的结果行来自 Gradle fake（一行），daemon 没写过
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

    def test_batch_prepare_failure_falls_back_with_reason(self) -> None:
        """准备失败（如启动超时）→ 原因逐字进结果，各块照旧回落 Gradle，
        批不炸。"""
        reason = "Validate daemon 180s 内没起来（看 validate_daemon.log）"
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "failed", "reason": reason}), \
            mock.patch("core.jvm_validate_daemon.probe", return_value=None):
            result = self._call()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.gradle_calls, 1)
        self.assertEqual(result["execution_mode"], "gradle_fallback")
        self.assertEqual(result["daemon_prepare"],
                         {"outcome": "failed", "reason": reason})

    def test_batch_prepare_exception_is_contained(self) -> None:
        """prepare 意外抛异常也不能带走整批：兜底成 failed 带原因，批继续。"""
        with mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
            mock.patch("core.jvm_validate_daemon.prepare",
                        side_effect=RuntimeError("boom")), \
            mock.patch("core.jvm_validate_daemon.probe", return_value=None):
            result = self._call()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.gradle_calls, 1)
        self.assertEqual(result["daemon_prepare"],
                         {"outcome": "failed", "reason": "boom"})

    def _resume_harness(self):
        """重试恢复的共享桩具：daemon 第 2 次调用返回 code=2、Gradle 第 1 次
        调用 exit=1——第 1 轮固定打成「块 1 daemon 成、块 2 双败中止、目录保留」。"""
        import contextlib

        submitted = {}
        state = {"daemon": 0, "gradle": 0}

        def capture_submit(kind, payload, lane=None):
            submitted["payload"] = payload
            return "resume-job"

        def fake_run(_dump, args_file, socket_timeout=None):
            state["daemon"] += 1
            args = pathlib.Path(args_file).read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            if state["daemon"] == 2:
                return {"code": 2, "cost_ms": 3, "error": "daemon 内部错误"}
            out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                           encoding="utf-8")
            return {"code": 0, "cost_ms": 3, "error": ""}

        def flaky_gradle(args_path=None, runtime=None, **kwargs):
            state["gradle"] += 1
            if state["gradle"] == 1:
                return {"exit": 1, "stdout": "", "stderr": "boom"}
            return self._fake_gradle(args_path=args_path, runtime=runtime)

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
                               side_effect=fake_run),
                    mock.patch.object(jvm_exec, "_run_gradle",
                                      side_effect=flaky_gradle))

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
        self.assertEqual(state["gradle"], 1)

        # 第 2 轮（重试）：块 1 DONE 跳过；块 2 待跑 → 本轮准备一次、跑成
        prepare2 = mock.MagicMock(return_value=_started())
        r2 = run_only(prepare2, "resume-job-2", submitted["payload"])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(prepare2.call_count, 1, "恢复批对唯一待跑块仍只准备一次")
        self.assertEqual(len([c for c in r2["chunk_reports"] if c.get("resumed")]), 1)
        self.assertEqual(state["daemon"], 3)
        self.assertEqual(state["gradle"], 1, "重试成功后不应有新的 Gradle 调用")

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
    def _kinds(run_dir) -> list:
        text = (pathlib.Path(run_dir) / "events.jsonl").read_text(
            encoding="utf-8")
        return [json.loads(l)["kind"] for l in text.splitlines() if l.strip()]

    def test_queue_wait_event_carries_lane_wait(self) -> None:
        """排队事件只记录 lane 的实际等待时间。"""
        _payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        queue = [e for e in result["events"] if e.get("stage") == "queue_wait"]
        self.assertEqual(1, len(queue), result["events"] )
        self.assertGreaterEqual(queue[0]["cost_sec"], 0)
        self.assertNotIn("since_submit_sec", queue[0])

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
            _payload, result = self._run_batch(self._QUIET)
        self.assertEqual(jvm_exec._DAEMON_BUSY_WAIT_SEC, seen["busy_wait_sec"])
        waits = [e for e in result["events"] if e.get("kind") == "waiting_engine"]
        self.assertEqual(1, len(waits), result["events"])
        self.assertEqual(10.0, waits[0]["elapsed_sec"])

    def test_batch_stale_snapshot_skips_daemon_and_keeps_reason(self) -> None:
        """改了 Kotlin 未刷新 snapshot：批量不碰 daemon、全部走 Gradle，原因可见。

        daemon 的 sig 只反映源码 mtime，不等于类已重编——少了这道闸门就会整批跑在
        旧字节码上，而结论和真跑的一样。
        """
        gate = {"ok": False,
                "reason": "snapshot 早于 appservice Kotlin 源码，请先刷新"}
        with mock.patch.object(jvm_exec, "execution_readiness",
                               lambda dump=None: gate), \
             mock.patch("core.jvm_direct.load_dump", return_value=self._DUMP), \
             mock.patch("core.jvm_validate_daemon.prepare") as prepare:
            _payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        prepare.assert_not_called()
        self.assertEqual({r.get("execution_mode") for r in result["chunk_reports"]},
                         {"gradle"})
        self.assertIn("刷新", result["daemon_prepare"]["reason"])

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
        """成功批清运行目录，时间线靠 result["events"] 长存（合并的意义）。"""
        _payload, result = self._run_batch(self._QUIET)
        self.assertTrue(result["ok"], result)
        self.assertEqual([e["kind"] for e in result["events"]],
                         ["batch_started", "startup_stage", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_done", "chunk_started", "chunk_done",
                          "done"])

    def test_prepare_and_recovered_events_on_busy_retry(self) -> None:
        """忙批：prepare(busy) → 首块 Gradle → 重试块 daemon + recovered → 余块 daemon。"""

        def fake_run(_dump, args_file, socket_timeout=None):
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
        self.assertTrue(result["ok"], result)
        self.assertEqual([e["kind"] for e in result["events"]],
                         ["batch_started", "startup_stage", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_done", "recovered",
                          "chunk_started", "chunk_done", "done"])
        self.assertEqual(result["daemon_chunks"], 2)
        self.assertEqual(result["gradle_chunks"], 1)

    def test_resume_and_abort_events(self) -> None:
        """中止批以 failed 收尾；恢复批以 resumed 开场、done 收尾。"""
        import contextlib

        gradle_calls = {"n": 0}

        def flaky_gradle(args_path=None, runtime=None, **kwargs):
            gradle_calls["n"] += 1
            if gradle_calls["n"] == 2:
                return {"exit": 1, "stdout": "", "stderr": "boom"}
            return self._fake_gradle(args_path=args_path, runtime=runtime)

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
                    mock.patch.object(jvm_exec, "_run_gradle",
                                      side_effect=flaky_gradle)]

        with contextlib.ExitStack() as stk:
            for p in patches():
                stk.enter_context(p)
            asyncio.run(jvm_api.jvm_run(
                JvmRunRequest(urls=["https://a.com", "https://b.com"])))
            r1 = asyncio.run(jvm_exec.run_jvm_job(
                "ev-job", Store(self.db), submitted["payload"]))
        run_dir = submitted["payload"]["manifest"]["run_dir"]
        self.assertEqual(self._kinds(run_dir),
                         ["batch_started", "startup_stage", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_failed", "failed"])
        self.assertFalse(r1["ok"])

        with contextlib.ExitStack() as stk:
            for p in patches():
                stk.enter_context(p)
            r2 = asyncio.run(jvm_exec.run_jvm_job(
                "ev-job-2", Store(self.db), submitted["payload"]))
        self.assertTrue(r2["ok"], r2)
        # 事件文件跨轮**追加**：恢复批的时间线包含上一轮的完整历史，
        # resumed 行标记了两次运行的边界（成功后目录清场，断言走合并结果）
        self.assertEqual([e["kind"] for e in r2["events"]],
                         ["batch_started", "startup_stage", "prepare", "chunk_started", "chunk_done",
                          "chunk_started", "chunk_failed", "failed",
                          "batch_started", "startup_stage", "resumed", "prepare", "chunk_started",
                          "chunk_done", "done"])

    def test_events_tail_merge_is_bounded(self) -> None:
        run_dir = self.probe / "data" / "app_probe" / "runs" / "ev-cap"
        run_dir.mkdir(parents=True)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({"kind": "e%d" % i}) + "\n")
        with mock.patch.object(jvm_exec, "_EVENT_TAIL_CAP", 3):
            got = jvm_exec._read_events_tail(run_dir)
        self.assertEqual([e["kind"] for e in got], ["e2", "e3", "e4"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Gradle 路径的相位推进：引擎真的开始跑了，头就不能还写着「启动 Gradle」。

历史 bug（2026-10-09 实测）：`starting_gradle` 只在调 Gradle **之前**设一次，而
`_run_gradle` 是一次阻塞调用，把任务图、编译与全部校验都干完（60～130 秒/块）——
期间没有任何人推进相位。于是 400 条的批（snapshot 过期 → daemon 准备失败 →
16 块全走 Gradle）进度已经 150/400，任务行的 `phase` 还是 `starting_gradle`，
弹窗头上写着「启动 Gradle」。用户看到的是**自相矛盾**：明明在校验源。

判据是**真观测**而不是计时器：`ValidateService.main` 的第一句就写下
`$dump.actual.gradle.validate.json`（`ServiceJson.writeRuntimeSnapshot("validate")`），
那是**校验 JVM 自己**落盘的报告——比 init 脚本在测试任务 `doFirst` 里写的 dump 晚，
但那是编译结束、JVM 刚起来那一刻，正好是「启动 Gradle」与「执行校验」的分界。
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from backend.jobs import jvm_exec


class EngineStartWatchTests(unittest.TestCase):
    """相位观察线程本身：看校验 JVM 的报告文件，不看时钟。"""

    def _watch(self, publish_at=None, run_sec=0.3) -> list:
        """起观察线程；`publish_at` 秒后写下报告（None = 永不写）。返回相位写入。"""
        seen: list = []
        root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_phase_watch_"))
        self.addCleanup(shutil.rmtree, str(root), ignore_errors=True)
        report = root / "test_jvm_env.json.actual.gradle.validate.json"
        stop = threading.Event()
        with mock.patch("backend.jobs.runner.update_phase",
                        side_effect=lambda jid, phase: seen.append((jid, phase))):
            thread = threading.Thread(
                target=jvm_exec._watch_validate_start,
                args=("job", report, stop), kwargs={"interval": 0.01}, daemon=True)
            thread.start()
            if publish_at is not None:
                time.sleep(publish_at)
                report.write_text("{}", encoding="utf-8")
            time.sleep(run_sec)
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "观察线程没退出")
        return seen

    def test_report_landing_advances_phase_once(self):
        # 落盘后线程即收工：多跑 0.3 秒也不许重复写相位
        self.assertEqual(self._watch(publish_at=0.0), [("job", "running_validate")])

    def test_no_report_keeps_launch_phase(self):
        # Gradle 起不来（编译失败等）时不能谎报「在执行校验」
        self.assertEqual(self._watch(publish_at=None), [])


class RunGradlePhaseTests(unittest.TestCase):
    """真 `_run_gradle`：不给任务号就不观察，给了就必须把相位推过去。"""

    def _run(self, job_id: str) -> list:
        root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_phase_run_"))
        self.addCleanup(shutil.rmtree, str(root), ignore_errors=True)
        run_dir = root / "app_probe" / "runs" / "phased"
        run_dir.mkdir(parents=True)
        args_path = run_dir / "args.properties"
        args_path.write_text("file=x\n", encoding="utf-8")
        # 校验 JVM 写的那份报告：`ServiceJson.writeRuntimeSnapshot("validate")`
        report = root / "app_probe" / "test_jvm_env.json.actual.gradle.validate.json"

        def fake_communicate(timeout=None):
            # 0.2 秒 = 假 Gradle 的存活时间；采样 0.01 秒，留够余量免得负载高时假红
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("{}", encoding="utf-8")
            time.sleep(0.2)
            return ("", "")

        proc = mock.Mock(returncode=0)
        proc.communicate.side_effect = fake_communicate
        seen: list = []
        with mock.patch.object(jvm_exec, "_launcher",
                               return_value=root / "appservice" / "legado-gradle.bat"), \
             mock.patch.object(jvm_exec, "_AGSVC", root / "appservice"), \
             mock.patch.object(jvm_exec, "data_dir", lambda: root), \
             mock.patch.object(jvm_exec, "_VALIDATE_START_POLL_SEC", 0.01), \
             mock.patch("backend.jobs.runner.update_phase",
                        side_effect=lambda jid, phase: seen.append((jid, phase))), \
             mock.patch.object(jvm_exec.subprocess, "Popen", return_value=proc):
            jvm_exec._run_gradle(args_path=args_path, job_id=job_id, runtime={
                "app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk",
                "gradle_user_home": "X:/.gradle"})
        return seen

    def test_report_landing_advances_phase(self):
        self.assertEqual(self._run("job"), [("job", "running_validate")])

    def test_dump_alone_does_not_advance_phase(self):
        """只有 dump（编译期写的）落盘时**不许**说「在校验」：那份早于测试 JVM 约 10 秒。
        守的是「判据换回去」这类回归——dump 是同一个目录里的诱饵文件。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_phase_dump_"))
        self.addCleanup(shutil.rmtree, str(root), ignore_errors=True)
        run_dir = root / "app_probe" / "runs" / "phased"
        run_dir.mkdir(parents=True)
        args_path = run_dir / "args.properties"
        args_path.write_text("file=x\n", encoding="utf-8")
        dump = root / "app_probe" / "test_jvm_env.json"

        def fake_communicate(timeout=None):
            dump.write_text("{}", encoding="utf-8")
            time.sleep(0.2)
            return ("", "")

        proc = mock.Mock(returncode=0)
        proc.communicate.side_effect = fake_communicate
        seen: list = []
        with mock.patch.object(jvm_exec, "_launcher",
                               return_value=root / "appservice" / "legado-gradle.bat"), \
             mock.patch.object(jvm_exec, "_AGSVC", root / "appservice"), \
             mock.patch.object(jvm_exec, "data_dir", lambda: root), \
             mock.patch.object(jvm_exec, "_VALIDATE_START_POLL_SEC", 0.01), \
             mock.patch("backend.jobs.runner.update_phase",
                        side_effect=lambda jid, phase: seen.append((jid, phase))), \
             mock.patch.object(jvm_exec.subprocess, "Popen", return_value=proc):
            jvm_exec._run_gradle(args_path=args_path, job_id="job", runtime={
                "app_repo": "X:/repo", "java_home": "X:/jdk", "android_sdk": "X:/sdk",
                "gradle_user_home": "X:/.gradle"})
        self.assertEqual(seen, [])

    def test_without_job_id_nothing_is_written(self):
        # 没有任务号就无处可写：老调用方（不带相位需求）行为不变
        self.assertEqual(self._run(""), [])


if __name__ == "__main__":
    unittest.main()

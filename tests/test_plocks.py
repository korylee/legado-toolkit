# -*- coding: utf-8 -*-
"""跨进程互斥锁：判据 + 调用点。

判据（`core.plocks.ProcLock` 本体）：争用只有一个赢家、释放后立即可再持有、
**持有者进程死亡后内核立刻放锁**——最后这条是整个选型的根基（「持有者已死」
是进程边界事实，不是超时猜测，lessons §二十八），所以用子进程 `os._exit`
模拟真实死亡，不用任何假时钟。

调用点（`backend.app` 的 lifespan）：哨兵函数写对了但没人在启动时调，第二
实例照样双开——事故现场一点没变。同 test_job_recovery 的理由，用
`lifespan_context` 手动进出守一遍。

测试全程不碰真实 `data/`（lessons §七十四）：锁路径显式传临时目录，lifespan
那组用 `LEGADO_DATA_DIR` 重定向。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from core.plocks import ProcLock, read_owner

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProcLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="plocks_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = os.path.join(self.root, "locks", "nested", "probe.lock")

    def test_second_acquire_while_held_loses(self) -> None:
        """争用只有一个赢家；输家拿到的是 False，不是异常也不是等待。"""
        first = ProcLock(self.path)
        second = ProcLock(self.path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()

    def test_release_frees_the_lock_immediately(self) -> None:
        first = ProcLock(self.path)
        second = ProcLock(self.path)
        self.assertTrue(first.acquire())
        first.release()
        self.assertTrue(second.acquire(), "释放后下一个持有者要立即可进")
        second.release()

    def test_acquire_is_idempotent_for_same_object(self) -> None:
        lock = ProcLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()  # 重复 release 是空操作，不许抛

    def test_owner_info_is_readable_while_held(self) -> None:
        """持有信息写在锁文件里：拒绝启动的那句话要能指到占用者 pid。"""
        lock = ProcLock(self.path)
        self.assertTrue(lock.acquire())
        who = read_owner(self.path)
        self.assertEqual(who.get("pid"), os.getpid())
        self.assertTrue(who.get("acquired_at"))
        lock.release()

    def test_read_owner_tolerates_missing_or_garbage(self) -> None:
        self.assertEqual(read_owner(self.path), {})
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(b"not json")
        self.assertEqual(read_owner(self.path), {})

    def test_dead_holder_releases_via_kernel(self) -> None:
        """持有者进程一死，内核立刻放锁——不需要任何超时。

        子进程真实地拿到锁再 `os._exit`（跳过一切清理），父进程在它死后
        **立即** acquire 必须成功。这就是哨兵「杀掉旧进程后新实例可启动」
        的验收本体。
        """
        holder_path = os.path.join(self.root, "dead_holder.lock")
        code = ("import os, sys; sys.path.insert(0, %r);"
                "from core.plocks import ProcLock;"
                "assert ProcLock(%r).acquire(); os._exit(0)"
                % (_REPO_ROOT, holder_path))
        subprocess.run([sys.executable, "-B", "-c", code],
                       check=True, timeout=60, cwd=_REPO_ROOT)
        self.assertTrue(ProcLock(holder_path).acquire(),
                        "持有者进程已死，锁必须已被内核释放")


class _DataDirCase(unittest.TestCase):
    """lifespan 走 LEGADO_DATA_DIR 的那组：每个用例一个独立 data 目录。"""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="plocks_app_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old

    def _enter_lifespan(self):
        from backend.app import app

        ctx = app.router.lifespan_context(app)
        return ctx


class SentinelWiringTests(_DataDirCase):
    def test_lifespan_holds_and_releases_the_sentinel(self) -> None:
        """服务活着锁就在（占用者可对账），进程收尾锁就放。"""
        from core.paths import data_path

        lock_path = data_path("locks", "backend.lock")

        async def _start():
            ctx = self._enter_lifespan()
            await ctx.__aenter__()
            try:
                self.assertTrue(os.path.exists(lock_path))
                who = read_owner(lock_path)
                self.assertEqual(who.get("pid"), os.getpid())
            finally:
                await ctx.__aexit__(None, None, None)

        asyncio.run(_start())
        # 退出后同一把锁可再持有（等价于「旧实例退了新实例能起」）
        self.assertTrue(ProcLock(lock_path).acquire())

    def test_second_lifespan_entry_is_refused_with_reason(self) -> None:
        """第一个实例活着时第二个必须被拒，且原因指到占用者——静默退出或
        只抛裸异常都算没修：双开的人要知道是谁占着。"""
        first = self._enter_lifespan()

        async def _run():
            await first.__aenter__()
            try:
                second = self._enter_lifespan()
                with self.assertRaises(RuntimeError) as cm:
                    await second.__aenter__()
                self.assertIn(str(os.getpid()), str(cm.exception))
            finally:
                await first.__aexit__(None, None, None)

        asyncio.run(_run())


class LayoutTests(unittest.TestCase):
    def test_lock_body_stays_empty_and_sidecar_cleans_up(self) -> None:
        """锁文件本体保持 0 字节（普通读者不会撞上 Windows 的锁区域，
        实测缓冲 read 一旦覆盖锁字节就整个 EACCES）；持有信息在 sidecar，
        正常 release 后 sidecar 一并清掉。"""
        lock = ProcLock(os.path.join(tempfile.mkdtemp(prefix="plocks_l_"),
                                     "x.lock"))
        self.addCleanup(shutil.rmtree, os.path.dirname(str(lock.path)),
                        ignore_errors=True)
        self.assertTrue(lock.acquire())
        self.assertEqual(os.path.getsize(str(lock.path)), 0)
        self.assertTrue(os.path.exists(str(lock.path) + ".owner.json"))
        lock.release()
        self.assertFalse(os.path.exists(str(lock.path) + ".owner.json"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_plocks` → 确认变红 → 还原）。
#
#  M1  lifespan 里把 `if not sentinel.acquire():` 改成 `if False:`（哨兵判定被拿掉）
#       → test_second_lifespan_entry_is_refused_with_reason 红（第二个实例没被拒）
#       → test_lifespan_holds_and_releases_the_sentinel 红（锁文件没被创建）
#       判据层用例（ProcLockTests）照样全绿——原语对了但启动时没接，双开照样发生。
#
#  M2  `core/plocks.py` 的 `_try_lock` 首行插 `return`（争用判定被拿掉，人人都拿得到）
#       → test_second_acquire_while_held_loses 红（第二个 acquire 不再失败）
#       → test_second_lifespan_entry_is_refused_with_reason 红
#       互斥不存在时两条都要红：调用点的「被拒」依赖原语的「争用只有一个赢家」。

# -*- coding: utf-8 -*-
"""跨进程互斥：OS 字节区间锁。单实例哨兵与（将来的）worker liveness 锁共用它。

为什么是内核锁而不是库表 + 心跳：进程死则内核立刻释放锁——「持有者已死」是
进程边界能确定的事实，不是超时猜测（lessons §二十八）。心跳 + TTL 把「活着
但慢」（系统休眠、进程卡顿）误判成死，接管方就会和假死方并发写同一份运行
目录；内核锁下这一类错误不存在。代价是「多快发现持有者死了」由轮询间隔决定
（将来的 worker 回收用），而它永远不影响对错——TTL 把这两件事焊死了。

实现选**字节区间锁**而不是 O_EXCL 建文件：O_EXCL 的锁在崩溃后留下残留文件，
下一次 acquire 永远失败，还得配一套「pid 还活着吗」的僵尸清理；字节区间锁
挂在常驻文件上，句柄一关（或进程一死）锁就没了，文件留着无害。

句柄必须**不可被子进程继承**：后端拉起的 Gradle/JVM 若继承句柄，父进程退出
后锁仍被钉着，第二实例永远起不来。Python 3 默认非继承（PEP 446），`os.open`
的返回值满足；别换会打开可继承句柄的写法。

Windows 走 `msvcrt.locking`（按字节区间，从当前文件位置起），POSIX 走
`fcntl.flock`（整文件）；两边对外语义一致：非阻塞 try-acquire，持有期间
其他句柄拿不到，进程退出内核统一释放。

**锁文件本体不承载信息**（保持 0 字节）：Windows 的区域锁挡的是「其他句柄读
被锁字节」，而普通读者（`open().read`、编辑器、cat）读「整个文件」都会撞上
——实测缓冲读 `read(4096)` 发出的系统调用覆盖到 4097 字节处，整个 read 直接
EACCES。谁在持有写在旁边的 `.owner.json` sidecar，谁都能读。sidecar 在持有者
崩溃后会残留，但不会误导：它只在「acquire 失败 = 锁确实被持有」时被读，那时
它的内容必属于当前持有者；下一个持有者 acquire 后会重写它。
"""

import json
import os
import pathlib
import sys
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl

#: 锁定的字节位。文件本体保持 0 字节，这个偏移落在「任何人都不会去读」的
#: 稀疏区里，与内容彻底无关。
LOCK_OFFSET = 4096


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _owner_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".owner.json")


def read_owner(path: str) -> dict:
    """读 sidecar 里的持有者信息（pid / acquired_at / cmd）。

    best-effort：sidecar 不存在（持有者从没正常写过、或已被 release 清掉）、
    半截写入——都返回空 dict，调用方（拒绝启动的那句话）自己兜「？」。
    """
    try:
        with open(_owner_path(pathlib.Path(path)), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class ProcLock:
    """一把跨进程互斥锁。非阻塞；持有信息写在 sidecar 供排错与对账。"""

    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """尝试持有；拿不到（别人在持有）返回 False，不等待。重复 acquire 幂等。"""
        if self._fd is not None:
            return True
        parent = self.path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.path), flags, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self._write_owner()
        return True

    def release(self) -> None:
        """放锁。没持有过就是空操作；放锁失败不掩盖 close——句柄关了锁终究会没。"""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
            # 正常收尾顺手清掉 sidecar；崩溃路径清不掉也无妨（见模块注释）
            try:
                os.unlink(_owner_path(self.path))
            except OSError:
                pass

    def _write_owner(self) -> None:
        owner = {
            "pid": os.getpid(),
            "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": " ".join(sys.argv)[:200],
        }
        try:
            with open(_owner_path(self.path), "w", encoding="utf-8") as f:
                json.dump(owner, f, ensure_ascii=False)
        except OSError:
            # sidecar 只是排错信息，写不出去不影响锁的正确性
            pass

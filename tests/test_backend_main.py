# -*- coding: utf-8 -*-
"""后端启动参数解析的测试。

重点不是「参数能不能解析」，而是**命令行与环境变量的优先级**——
两者都能设，取错一边不会有任何报错，只会让人以为设了却没生效。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from backend.__main__ import RELOAD_DIRS, _ROOT, _address_line, build_parser

_ENV_KEYS = ("LEGADO_HOST", "LEGADO_PORT", "LEGADO_RELOAD")


def parse(argv, env=None):
    """按给定的环境变量解析参数。清掉真实环境里的同名变量，避免串味。"""
    clean = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True):
        return build_parser().parse_args(argv)


class ParserTests(unittest.TestCase):
    def test_defaults(self):
        args = parse([])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8787)
        self.assertFalse(args.reload)

    def test_env_supplies_defaults(self):
        args = parse([], {"LEGADO_HOST": "127.0.0.1", "LEGADO_PORT": "9000",
                          "LEGADO_RELOAD": "1"})
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9000)
        self.assertTrue(args.reload)

    def test_reload_env_accepts_common_truthy_values(self):
        for value in ("1", "true", "YES", "on", " true "):
            with self.subTest(value=value):
                self.assertTrue(parse([], {"LEGADO_RELOAD": value}).reload)
        for value in ("0", "false", "", "no"):
            with self.subTest(value=value):
                self.assertFalse(parse([], {"LEGADO_RELOAD": value}).reload)

    def test_cli_beats_env(self):
        args = parse(["--host", "10.0.0.1", "--port", "1234"],
                     {"LEGADO_HOST": "127.0.0.1", "LEGADO_PORT": "9000"})
        self.assertEqual(args.host, "10.0.0.1")
        self.assertEqual(args.port, 1234)

    def test_no_reload_can_override_env(self):
        # 环境变量设了还得能关掉，否则只能去改 shell 环境
        self.assertTrue(parse([], {"LEGADO_RELOAD": "1"}).reload)
        self.assertFalse(parse(["--no-reload"], {"LEGADO_RELOAD": "1"}).reload)

    def test_reload_flag_turns_it_on_without_env(self):
        self.assertTrue(parse(["--reload"]).reload)


class ReloadScopeTests(unittest.TestCase):
    """热重载的监视范围必须限死在代码目录。

    不限的话 uvicorn 会盯着整个仓库根，StatReload 每 0.25 秒递归扫一遍
    `*.py`——.venv 和 frontend/node_modules 都在里面（实测 519 个文件 vs 72 个）。
    """

    def test_all_reload_dirs_exist(self):
        for name in RELOAD_DIRS:
            with self.subTest(dir=name):
                self.assertTrue((_ROOT / name).is_dir(),
                                "%s 不存在，uvicorn 会静默少监视一处" % name)

    def test_heavy_dirs_are_not_watched(self):
        for name in (".venv", "frontend", "data", "tests"):
            self.assertNotIn(name, RELOAD_DIRS)


class AddressLineTests(unittest.TestCase):
    """启动日志里的地址行（`--host 0.0.0.0` 时不能照搬）。

    0.0.0.0 不是一个能打开的地址，照搬等于给出一条打不开的链接；而这行日志正是
    「手机连不上」时第一个被看的地方。
    """

    def test_any_host_uses_the_lan_ip(self):
        self.assertEqual(_address_line("0.0.0.0", 8787, ["192.168.1.5"]),
                         "地址：http://192.168.1.5:8787")

    def test_only_the_first_nic_is_reported(self):
        # 装了 WSL / Hyper-V 的机器上会有多张网卡，但日志里的地址点不动、复制
        # 还得手选，列一串只会让人不知道用哪个；要挑网卡去导出抽屉挑
        self.assertEqual(_address_line("0.0.0.0", 8787,
                                       ["192.168.1.5", "172.28.80.1"]),
                         "地址：http://192.168.1.5:8787")

    def test_explicit_host_is_kept_as_is(self):
        self.assertEqual(_address_line("127.0.0.1", 9000, ["192.168.1.5"]),
                         "地址：http://127.0.0.1:9000")

    def test_probe_failure_is_not_papered_over(self):
        # 探不到就说探不到，不编一个地址出来
        line = _address_line("0.0.0.0", 8787, [])
        self.assertIn("127.0.0.1", line)
        self.assertIn("未探测到", line)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（照项目惯例：改坏 → 跑 → 确认变红 → 改回）。
#
#  M1  --reload 的 default 写死 False（不再读 LEGADO_RELOAD）
#        → ParserTests.test_env_supplies_defaults / test_no_reload_can_override_env 红
#  M2  去掉 --no-reload（改回 action="store_true"）
#        → ParserTests.test_no_reload_can_override_env 红（报 unrecognized arguments）
#  M3  _address_line 里把 0.0.0.0 判断换成恒真（照搬 host）
#        → AddressLineTests 红 3 条（地址变成打不开的 http://0.0.0.0:8787）

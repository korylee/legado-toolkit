# -*- coding: utf-8 -*-
"""显式准备 Gradle Wrapper 分发包，不在校验/调试动作中隐式下载。"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Dict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.gradle_distribution import (  # noqa: E402
    GradleDistributionError,
    ensure_gradle_distribution,
)
from core.paths import data_path  # noqa: E402


def _properties(path: pathlib.Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _config_distribution_url() -> str:
    configured = _properties(pathlib.Path(data_path("jvm.properties")))
    return (os.environ.get("LEGADO_GRADLE_DISTRIBUTION_URL") or
            configured.get("gradle.distribution.url", "")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="准备 Gradle Wrapper 分发包；此命令会联网，校验/调试不会自动执行它")
    parser.add_argument("--app-repo", default=os.environ.get("LEGADO_REPO", ""),
                        help="legado-with-MD3 仓库根目录（默认读取 LEGADO_REPO）")
    parser.add_argument(
        "--gradle-user-home",
        default=os.environ.get("GRADLE_USER_HOME", str(pathlib.Path.home() / ".gradle")),
        help="Gradle User Home（默认读取 GRADLE_USER_HOME，未设置时使用 Gradle 默认目录）")
    parser.add_argument(
        "--distribution-url", default=_config_distribution_url(),
        help="分发 ZIP 的完整地址；也可配置 LEGADO_GRADLE_DISTRIBUTION_URL 或 data/jvm.properties")
    parser.add_argument("--timeout", type=int, default=60, help="单次网络读取超时秒数")
    args = parser.parse_args()

    if not args.app_repo:
        parser.error("缺少 --app-repo；也可以先设置 LEGADO_REPO")
    print("App 仓库       : %s" % args.app_repo)
    print("Gradle User Home: %s" % args.gradle_user_home)
    if args.distribution_url:
        print("下载地址       : %s" % args.distribution_url)
    else:
        print("下载地址       : 使用 Wrapper 配置中的 distributionUrl")
    try:
        result = ensure_gradle_distribution(
            args.app_repo,
            args.gradle_user_home,
            distribution_url=args.distribution_url or None,
            timeout=max(1, args.timeout),
        )
    except (GradleDistributionError, OSError, ValueError) as exc:
        print("准备失败：%s" % exc)
        return 1

    print("状态           : %s" % result.get("status", "unknown"))
    if result.get("cache_dir"):
        print("缓存目录       : %s" % result["cache_dir"])
    if result.get("status") == "ready":
        print("无需下载，缓存已就绪")
    elif result.get("status") == "downloaded":
        print("准备完成，后续校验/调试不会再次下载")
    return 0 if result.get("status") in {"ready", "downloaded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

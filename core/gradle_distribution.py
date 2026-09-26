# -*- coding: utf-8 -*-
"""Inspect and explicitly prepare the Gradle Wrapper distribution.

The upstream repository owns ``gradle-wrapper.properties``.  Its official
URL remains the cache identity used by the Wrapper, while an explicitly
configured URL may provide the archive before the Wrapper starts.  Downloading
is intentionally outside the JVM business flow.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import urllib.error
import urllib.request
import zipfile
from typing import Dict, Optional
from urllib.parse import urlsplit


class GradleDistributionError(RuntimeError):
    """The Wrapper archive could not be inspected or prepared."""


def _read_properties(path: pathlib.Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        result[key] = value.replace(r"\:", ":").replace(r"\=", "=")
    return result


def _base36(value: int) -> str:
    if value == 0:
        return "0"
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while value:
        value, remainder = divmod(value, 36)
        out.append(chars[remainder])
    return "".join(reversed(out))


def wrapper_cache_dir(gradle_user_home: str, distribution_url: str) -> pathlib.Path:
    """Return the cache directory used by the current Gradle Wrapper format."""
    parsed = urlsplit(distribution_url)
    filename = pathlib.PurePosixPath(parsed.path).name
    if not filename:
        raise GradleDistributionError("Gradle distributionUrl 没有 ZIP 文件名")
    name = filename[:-4] if filename.lower().endswith(".zip") else filename
    # Gradle 9.x uses the positive MD5 integer rendered in base 36.
    digest = hashlib.md5(distribution_url.encode("utf-8")).digest()
    cache_key = _base36(int.from_bytes(digest, byteorder="big", signed=False))
    return pathlib.Path(gradle_user_home).expanduser() / "wrapper" / "dists" / name / cache_key


def wrapper_distribution_url(app_repo: str) -> str:
    """Read the Wrapper URL without guessing an alternate download source."""
    properties_path = pathlib.Path(app_repo) / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if not properties_path.is_file():
        raise GradleDistributionError("找不到 Gradle wrapper 配置：%s" % properties_path)
    properties = _read_properties(properties_path)
    original_url = properties.get("distributionUrl", "").strip()
    if not original_url:
        raise GradleDistributionError("wrapper 配置缺少 distributionUrl：%s" % properties_path)
    return original_url


def distribution_status(app_repo: str, gradle_user_home: str) -> Dict[str, str]:
    """Inspect Wrapper cache state without creating directories or using network."""
    if not app_repo or not gradle_user_home:
        return {"status": "missing", "reason": "缺少 App 仓库或 Gradle 用户目录"}
    original_url = wrapper_distribution_url(app_repo)
    cache_dir = wrapper_cache_dir(gradle_user_home, original_url)
    parsed = urlsplit(original_url)
    filename = pathlib.PurePosixPath(parsed.path).name
    zip_path = cache_dir / filename
    installed_dirs = [
        child for child in cache_dir.iterdir()
        if child.is_dir() and (child / "bin").is_dir()
    ] if cache_dir.is_dir() else []
    if len(installed_dirs) > 1:
        return {"status": "invalid", "url": original_url, "cache_dir": str(cache_dir),
                "reason": "缓存目录包含多个解压目录"}
    if len(installed_dirs) == 1:
        return {"status": "ready", "url": original_url, "cache_dir": str(cache_dir),
                "source": "extracted"}
    if zip_path.is_file() and zip_path.stat().st_size > 0:
        if zipfile.is_zipfile(zip_path):
            return {"status": "ready", "url": original_url, "cache_dir": str(cache_dir),
                    "source": "archive"}
        return {"status": "invalid", "url": original_url, "cache_dir": str(cache_dir),
                "reason": "缓存 ZIP 无效"}
    return {"status": "missing", "url": original_url, "cache_dir": str(cache_dir),
            "reason": "缓存中没有分发包"}


def _read_sidecar_sha256(url: str, timeout: int = 30) -> Optional[str]:
    try:
        with urllib.request.urlopen(url + ".sha256", timeout=timeout) as response:
            text = response.read(4096).decode("ascii", errors="ignore")
    except (OSError, urllib.error.URLError):
        return None
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return match.group(1).lower() if match else None


def ensure_gradle_distribution(
    app_repo: str,
    gradle_user_home: str,
    *,
    distribution_url: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, str]:
    """Download the Wrapper archive into its official cache identity.

    This is an explicit provisioning primitive. It has no mirror default:
    ``distribution_url`` must be supplied by the caller when an alternate
    source is desired, otherwise the URL declared by the App repository is
    used.
    """
    if not app_repo or not gradle_user_home:
        return {"status": "skipped", "reason": "缺少 App 仓库或 Gradle 用户目录"}
    properties_path = pathlib.Path(app_repo) / "gradle" / "wrapper" / "gradle-wrapper.properties"
    original_url = wrapper_distribution_url(app_repo)
    properties = _read_properties(properties_path)
    target_url = (distribution_url or os.environ.get("LEGADO_GRADLE_DISTRIBUTION_URL") or
                  original_url).strip()
    cache_dir = wrapper_cache_dir(gradle_user_home, original_url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(original_url)
    filename = pathlib.PurePosixPath(parsed.path).name
    zip_path = cache_dir / filename
    installed_dirs = [child for child in cache_dir.iterdir()
                      if child.is_dir() and (child / "bin").is_dir()]
    if len(installed_dirs) > 1:
        raise GradleDistributionError(
            "Gradle 缓存目录包含多个解压目录，Wrapper 无法判断应使用哪一个：%s" % cache_dir)
    if len(installed_dirs) == 1:
        return {"status": "ready", "url": target_url, "cache_dir": str(cache_dir), "downloaded": "false"}
    if zip_path.is_file() and zip_path.stat().st_size > 0 and zipfile.is_zipfile(zip_path):
        return {"status": "ready", "url": target_url, "cache_dir": str(cache_dir), "downloaded": "false"}

    partial = zip_path.with_suffix(zip_path.suffix + ".part")
    try:
        with urllib.request.urlopen(target_url, timeout=timeout) as response, partial.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if partial.stat().st_size == 0 or not zipfile.is_zipfile(partial):
            raise GradleDistributionError("下载地址返回的文件不是有效 ZIP：%s" % target_url)
        expected = properties.get("distributionSha256Sum", "").strip().lower()
        expected = expected or _read_sidecar_sha256(target_url, timeout=timeout)
        actual = hashlib.sha256(partial.read_bytes()).hexdigest()
        if expected and actual != expected:
            raise GradleDistributionError(
                "Gradle 分发包 SHA-256 不匹配：expected=%s actual=%s" % (expected, actual))
        os.replace(partial, zip_path)
    except GradleDistributionError:
        if partial.exists():
            partial.unlink()
        raise
    except (OSError, urllib.error.URLError, ValueError) as exc:
        if partial.exists():
            partial.unlink()
        raise GradleDistributionError("Gradle 分发包下载失败（%s）：%s" % (target_url, exc)) from exc
    return {
        "status": "downloaded",
        "url": target_url,
        "cache_dir": str(cache_dir),
        "downloaded": "true",
    }

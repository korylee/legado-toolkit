import io
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from core.gradle_distribution import (distribution_status, ensure_gradle_distribution,
                                          wrapper_cache_dir)


class _Response:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def _gradle_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("gradle-9.6.1/bin/gradle.bat", "@echo off\n")
    return out.getvalue()


class GradleDistributionTests(unittest.TestCase):
    def test_cache_key_matches_gradle_wrapper(self) -> None:
        path = wrapper_cache_dir(
            r"C:\Users\koryl\.gradle",
            "https://services.gradle.org/distributions/gradle-9.6.1-bin.zip",
        )
        self.assertEqual(path.name, "4ticwg1pgcbps2hj28r8so764")
        self.assertEqual(path.parent.name, "gradle-9.6.1-bin")

    def test_downloads_mirror_into_official_cache_identity(self) -> None:
        archive = _gradle_zip()
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "app"
            app.joinpath("gradle", "wrapper").mkdir(parents=True)
            app.joinpath("gradle", "wrapper", "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.6.1-bin.zip\n",
                encoding="utf-8",
            )
            user_home = Path(root) / "gradle-home"

            def open_url(url, timeout):
                self.assertEqual(timeout, 12)
                if url.endswith(".sha256"):
                    raise urllib.error.URLError("test: no sidecar")
                self.assertEqual(url, "https://mirror.example/gradle-9.6.1-bin.zip")
                return _Response(archive)

            with mock.patch("core.gradle_distribution.urllib.request.urlopen", side_effect=open_url):
                result = ensure_gradle_distribution(
                    str(app), str(user_home),
                    distribution_url="https://mirror.example/gradle-9.6.1-bin.zip", timeout=12)

            target = Path(result["cache_dir"]) / "gradle-9.6.1-bin.zip"
            self.assertEqual(result["status"], "downloaded")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), archive)

    def test_status_checks_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "app"
            app.joinpath("gradle", "wrapper").mkdir(parents=True)
            app.joinpath("gradle", "wrapper", "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.6.1-bin.zip\n",
                encoding="utf-8",
            )
            user_home = Path(root) / "gradle-home"
            with mock.patch("core.gradle_distribution.urllib.request.urlopen") as open_url:
                result = distribution_status(str(app), str(user_home))
            open_url.assert_not_called()
            self.assertEqual(result["status"], "missing")
            self.assertIn("cache_dir", result)

    def test_reuses_installed_distribution_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "app"
            app.joinpath("gradle", "wrapper").mkdir(parents=True)
            app.joinpath("gradle", "wrapper", "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.6.1-bin.zip\n",
                encoding="utf-8",
            )
            user_home = Path(root) / "gradle-home"
            cache = wrapper_cache_dir(str(user_home), "https://services.gradle.org/distributions/gradle-9.6.1-bin.zip")
            cache.joinpath("gradle-9.6.1").joinpath("bin").mkdir(parents=True)
            with mock.patch("core.gradle_distribution.urllib.request.urlopen") as open_url:
                result = ensure_gradle_distribution(str(app), str(user_home))
            open_url.assert_not_called()
            self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()

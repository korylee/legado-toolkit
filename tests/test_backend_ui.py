# -*- coding: utf-8 -*-
"""后端托管前端产物的约定（backend/app.py 末尾那段）。

只为一件会**静默坏掉**的事写测试：缓存头。index.html 被浏览器缓存住不是
「慢一点」，而是重建之后白屏——旧页面引用的那些带哈希的文件已经不存在了。
而这里恰好有个平台陷阱：starlette 的 `get_path` 是拿 `os.path.join` 拼路径的，
Windows 上传进来的是 `assets\\index-xxx.js`，按 `"assets/"` 前缀判会整个漏掉。

不起 FastAPI app（沿用本仓库的惯例，见 test_settings_api.py 开头）——本仓库
没有 TestClient 依赖，这里测的也都是同进程就能看清的事。
"""

from __future__ import annotations

import mimetypes
import unittest

from backend.app import WEB_DIST, _cache_control_for, app


class MimeTypeTests(unittest.TestCase):
    def test_js_is_served_as_javascript(self):
        """`.js` 不能落成 text/plain。

        Windows 的注册表把 .js 关联成 text/plain，`mimetypes` 会读它，而
        `<script type="module">` 对 MIME 是严格校验的——浏览器拒绝执行，整页
        全白，且在 Linux/macOS 上复现不出来。backend/app.py 里显式钉死了它。
        """
        for name in ("index-abc.js", "chunk.mjs"):
            self.assertEqual(mimetypes.guess_type(name)[0], "text/javascript", name)


class CacheControlTests(unittest.TestCase):
    def test_html_is_never_cached(self):
        # 根路径与显式 index.html 都落到这里；两种分隔符都覆盖
        for p in ("D:/x/dist/index.html", r"D:\x\dist\index.html", "index.html"):
            self.assertEqual(_cache_control_for(p), "no-store", p)

    def test_assets_are_cached_forever(self):
        # 产物名带内容哈希，名字一变就是新文件，缓存越久越省事
        for p in ("D:/x/dist/assets/index-abc.js", r"D:\x\dist\assets\index-abc.css"):
            self.assertEqual(_cache_control_for(p),
                             "public, max-age=31536000, immutable", p)

    def test_other_files_get_no_header(self):
        self.assertEqual(_cache_control_for("D:/x/dist/favicon.ico"), "")


class MountTests(unittest.TestCase):
    def test_mount_follows_dist_existence(self):
        """有 dist 就必须挂上；没有就只能提供 API，此时**不能**挂（挂了就 404 一片）"""
        mounted = [r for r in app.routes if getattr(r, "name", "") == "ui"]
        self.assertEqual(bool(mounted), WEB_DIST.is_dir())

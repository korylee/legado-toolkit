# -*- coding: utf-8 -*-
"""S1 读回：把 JVM 校验服务（ValidateService）的 NDJSON 结论写进管理库。

来源与口径（JVM 校验服务）：
  - 服务端：appservice/test/io/legado/app/service/ValidateService.kt
    （Robolectric 跑 App 真引擎，剥 webView 选项后走普通 HTTP；启动方式见
    appservice/args.properties + legado-gradle.bat）
  - 结论四态：ok / empty_js_shell（unknown 语义）/ no_result / error / timeout / invalid

写库策略：
  - 结论经 `core.jvm_health.store_checks` 落 `checks`（与产品那条路同一份映射：
    五档 / 验证阶段 / 谁判的），同时把原文另存 `meta`（key 前缀 `jvm_check:`）便于复核。
  - 本脚本只做「读回 + 汇总 + 落库」，幂等（重跑覆盖同 key）。

用法：
    python scripts/jvm_readback.py data/app_probe/s1_results.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.loader import _normalize_url  # noqa: E402
from core.store import Store  # noqa: E402

#: JVM 结论 state → 我们口径的 health 映射（**展示层用，不写 checks.health**）
STATE_NOTE = {
    "ok": "搜索命中（App 引擎）",
    "empty_js_shell": "本机无法验证：源声明 webView，剥掉后页面疑似要 JS 渲染",
    "login_wall": "搜索页要求登录（本次可能没带登录态）",
    "no_result": "搜索成功但无结果（App 引擎，关键词无命中）",
    "timeout": "搜索超时（App 引擎）",
    "error": "执行出错（App 引擎）",
    "invalid": "源 JSON 无效",
}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        # 合并产物是 JSON 数组（s1_full_merged.json）；逐行 NDJSON 也兼容
        rows = json.loads(text)
    else:
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    if not isinstance(rows, list):
        print("输入既不是数组也不是 NDJSON 行")
        return 2
    print("读回 %d 条 JVM 结论（%s）" % (len(rows), path))

    st = Store()
    batch_id = time.strftime("%Y%m%d_%H%M%S")
    try:
        for r in rows:
            url = _normalize_url(str(r.get("url", "") or ""))
            if not url:
                continue
            key = "jvm_check:%s:%s" % (batch_id, url)
            st.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, json.dumps(r, ensure_ascii=False)))
        st.conn.commit()
        # 与产品那条路**同一份映射**（core/jvm_health）：结论写进 checks，五档 / 验证阶段 / 谁判的
        from core import jvm_health
        n_checks = jvm_health.store_checks(rows, batch=batch_id, store=st)
    finally:
        st.close()

    dist = Counter(r.get("state") for r in rows)
    print("状态分布: %s" % dict(dist))
    print("已写入 meta（batch=%s）与 checks %d 条（batch 同 id）。" % (batch_id, n_checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())

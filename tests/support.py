# -*- coding: utf-8 -*-
"""调试材料类用例的共用夹具：一条任务行 + 假模型/假引擎。

任务类用例（agent_loop / rule_outline / 端点分流）都要「从一个 job_id 取材料」，
各抄一份任务行必然在某个字段上分家——材料的形状（steps[].page_id → pages[].html）
是本仓最容易悄悄改坏的一份约定。
"""
from __future__ import annotations

import json

SOURCE = {"bookSourceUrl": "http://example.com", "bookSourceName": "测试源"}

DETAIL_HTML = """<html><body>
<h1 class="title">甲书</h1>
<a class="chapter" href="/c/1">第1页</a>
<a class="chapter" href="/c/2">第2页</a>
</body></html>"""


def job_store(*, source=None, steps=(), pages=()):
    """假 Store：``get_job`` 返回一条任务行（材料从这里取）。"""
    job = {
        "payload": json.dumps({"source": source or SOURCE}),
        "result_json": json.dumps({"steps": list(steps), "pages": list(pages)}),
    }

    class _St:
        def get_job(self, _job_id):
            return job

    return _St()


def one_step_job(html=DETAIL_HTML, *, step="bookUrl", structured=None, source=None):
    """一条任务行、一步、一页：最常见的那种材料。"""
    step_row = {"name": step, "page_id": "detail", "url": "http://x/d"}
    if structured is not None:
        step_row["structured"] = structured
    return job_store(source=source, steps=[step_row],
                     pages=[{"id": "detail", "url": "http://x/d", "html": html}])


def fake_suggest(rule="class.title@text", *, model="", usage=None, response=""):
    """假 suggest_many：请求里的每个字段都回同一条固定提议。

    真实形状是 ``{field: [candidates]}``（一族字段一次调用，单字段缺席 =
    没有候选）。``prompt`` 按这次问的字段数生成，供调用账本的用例取认。
    """
    async def fake(ctx):
        fields = ctx.get("fields") or []
        return {"llm": "ok", "model": model,
                "fields": {f["field"]: [{"rule": rule, "why": "w"}] for f in fields},
                "prompt": "PROMPT-" + str(len(fields)),
                "response": response, "usage": usage or {}}
    return fake


def fake_rule_try(results):
    """假 rule_try：按调用次序返回 results 里的项（最后一项复用）。"""
    calls = {"n": 0}

    def fake(source, step, body, base, rule):
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return r
    return fake

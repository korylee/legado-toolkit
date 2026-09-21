# -*- coding: utf-8 -*-
"""知识库体检：报几个形状数字，用来早一点发现「失控」。

准入与形状要求见 ``AGENTS.md`` #23（三问 + 论证型文档每节末尾必须有 ``→`` 结论 + 标题按症状写）。
那三条是语义判断，机器抓不到；本脚本只报**形状**，回答四个问题：

- **文件还在长吗** —— 行数 / 节数 / 平均与最长一节（lessons 看得最细）；
- **有没有节没给出结论** —— 缺 ``→`` 结论行的节会列出来（那是互查欠账的清单）；扫的是
  **论证型文档**：lessons 与三份 skill——README / AGENTS / TODO 是 reference 与待办，
  不要求 ``→`` 行；
- **最近十次提交净增多少行** —— 只增不减是要看的信号；
- **水位到了没** —— 单节 / 单文件 / 净增三个阈值，只是「该看一眼了」，不是闸门。

**手动跑，不挂测试**：挂上去就多一个「只减不增」的维护面，而这里的数字该由人判断
（阈值会自己变成要维护的东西）。用法：

    .venv/Scripts/python.exe tools/kb_stats.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 论证型文档（要 `→` 结论行）。lessons 放第一个，它的数字与欠账列得最细。
DOCS = (
    "skills/legado-source-lessons/SKILL.md",
    "skills/legado-book-source/SKILL.md",
    "skills/legado-source-toolchain/SKILL.md",
    "skills/agent-write-safety/SKILL.md",
)

#: 体检阈值：只是「该看一眼了」的水位，不是闸门。改它不需要理由，看数字变红才需要。
WARN_LINES = 60          # 单节行数
WARN_TOTAL = 3000        # 单文件行数
WARN_NET = 200           # 最近十次提交对 lessons 的净增行
LIST_MAX = 12            # 每个文件最多列几个「无结论」的节


def sections(text: str):
    """``[(标题, 起始行号, 行数, 有没有 → 结论行)]``。"""
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out = []
    for k, i in enumerate(idx):
        end = idx[k + 1] if k + 1 < len(idx) else len(lines)
        body = lines[i:end]
        out.append((lines[i][3:], i + 1, end - i,
                    any(l.startswith("→") for l in body)))
    return out


def main() -> int:
    warn = []
    for rel in DOCS:
        path = ROOT / rel
        if not path.exists():
            print("%-24s **文件不在**（被删了？更新 DOCS）" % rel)
            continue
        text = path.read_text(encoding="utf-8")
        secs = sections(text)
        if not secs:
            print("%-24s %5d 行（没有 `## ` 节）" % (path.parent.name, text.count("\n") + 1))
            continue
        n_lines = text.count("\n") + 1
        longest = max(secs, key=lambda s: s[2])
        no_arrow = [s for s in secs if not s[3]]
        print("%-24s %5d 行 / %3d 节 / 平均 %4.1f 行 / 最长 %2d 行 / 无 `→` 结论 %d 节"
              % (path.parent.name, n_lines, len(secs),
                 sum(s[2] for s in secs) / max(1, len(secs)), longest[2], len(no_arrow)))
        if no_arrow and rel == DOCS[0]:
            for title, ln, n, _ in sorted(no_arrow, key=lambda s: -s[2])[:LIST_MAX]:
                print("      %4d 行  :%-5d %s" % (n, ln, title[:58]))
            if len(no_arrow) > LIST_MAX:
                print("      …… 另有 %d 节" % (len(no_arrow) - LIST_MAX))
        if n_lines > WARN_TOTAL:
            warn.append("%s 超过 %d 行" % (path.parent.name, WARN_TOTAL))
        if longest[2] > WARN_LINES:
            warn.append("%s 有单节超过 %d 行" % (path.parent.name, WARN_LINES))

    net = None
    try:
        out = subprocess.run(
            ["git", "log", "-10", "--numstat", "--format=%h", "--", DOCS[0]],
            cwd=str(ROOT), capture_output=True, text=True, timeout=20).stdout
        add = drop = 0
        for line in out.split("\n"):
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit():
                add += int(parts[0])
                drop += int(parts[1])
        net = add - drop
        print("\nlessons 最近 10 次提交：+%d / −%d 行（净 +%d）" % (add, drop, net))
    except Exception as e:                                    # noqa: BLE001
        print("\n（拿不到 git 数据：%s）" % e)

    if net is not None and net > WARN_NET:
        warn.append("最近十次提交净增超过 %d 行" % WARN_NET)
    print("\n水位：%s" % ("；".join(warn) if warn else "正常"))
    print("（准入三问见 AGENTS #23；数字只提示该看一眼，不用它拦提交）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

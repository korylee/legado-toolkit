# -*- coding: utf-8 -*-
# 行级补丁应用器：把改动清单按顺序应用到目标文件。
#
# 解决「用 shell 传多行代码必然被转义破坏」的问题：
#   1. 清单是行式纯文本，写它不需要转义
#   2. 只做「找行 -> 插入 / 删除 / 替换」，绝不做多行字符串匹配
#   3. 锚点必须唯一，重复即报错（防止改错位置）
#   4. 全部断言通过才写盘；中途失败则一个文件都不动
#
# 清单格式:
#   >>>FILE core/checker.py
#   >>>AFTER            (或 >>>BEFORE / >>>REPLACE / >>>DELETE)
#   <唯一锚点行>
#   ---
#   <新增行>
#   <<<
#
# 用法: python tools/apply_edits.py _edit.txt

import pathlib
import sys

NL = chr(10)
DASH = "---"
END = "<<<"
KINDS = (">>>AFTER", ">>>BEFORE", ">>>REPLACE", ">>>DELETE")


def parse(path):
    lines = pathlib.Path(path).read_text(encoding="utf-8").split(NL)
    ops, target, i = [], None, 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith(">>>FILE "):
            target = ln[8:].strip()
            i += 1
            continue
        if ln in KINDS:
            if not target:
                raise SystemExit("第 %d 行: 操作出现在 >>>FILE 之前" % (i + 1))
            kind, i, block = ln[3:], i + 1, []
            while i < len(lines) and lines[i] != END:
                block.append(lines[i])
                i += 1
            if i >= len(lines):
                raise SystemExit("缺少结束标记 <<<")
            i += 1
            ops.append((target, kind, block))
            continue
        i += 1
    return ops


def apply_ops(text, items):
    lines = text.split(NL)
    for kind, block in items:
        if kind == "DELETE":
            for ln in block:
                if lines.count(ln) != 1:
                    raise SystemExit("DELETE 目标不唯一(%d): %s" % (lines.count(ln), ln[:60]))
            for ln in block:
                lines.remove(ln)
            continue
        if DASH not in block:
            raise SystemExit(kind + " 缺少 --- 分隔符")
        k = block.index(DASH)
        old, new = block[:k], block[k + 1:]
        if kind == "REPLACE":
            if len(old) != len(new):
                raise SystemExit("REPLACE 前后行数必须一致")
            for a, b in zip(old, new):
                if lines.count(a) != 1:
                    raise SystemExit("REPLACE 目标不唯一(%d): %s" % (lines.count(a), a[:60]))
                lines[lines.index(a)] = b
            continue
        if len(old) != 1:
            raise SystemExit(kind + " 的锚点必须恰好一行")
        if lines.count(old[0]) != 1:
            raise SystemExit("锚点不唯一(%d): %s" % (lines.count(old[0]), old[0][:60]))
        i = lines.index(old[0])
        lines[(i + 1) if kind == "AFTER" else i:(i + 1) if kind == "AFTER" else i] = []
        lines[(i + 1) if kind == "AFTER" else i:(i + 1) if kind == "AFTER" else i] = new
    return NL.join(lines)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python tools/apply_edits.py <清单文件>")
    ops = parse(sys.argv[1])
    if not ops:
        raise SystemExit("清单里没有任何操作")
    by_file = {}
    for target, kind, block in ops:
        by_file.setdefault(target, []).append((kind, block))
    for target, items in by_file.items():
        p = pathlib.Path(target)
        if not p.exists():
            raise SystemExit("目标文件不存在: " + target)
        # **不要用 write_text**：Windows 上它把 \n 全翻成 \r\n，而本仓库源码是
        # LF——改一个字就整文件换行尾，且 diff 看不出来（两边都归一化），
        # 只在提交时冒一句「CRLF will be replaced by LF」。
        # 读时按通用换行解码（CRLF 归一成 LF，锚点才对得上），写回时还原原样式。
        raw = p.read_bytes()
        crlf = b"\r\n" in raw
        out = apply_ops(raw.decode("utf-8").replace("\r\n", "\n"), items)
        data = (out.replace("\n", "\r\n") if crlf else out).encode("utf-8")
        p.write_bytes(data)
        print("OK  %-30s %d 处" % (target, len(items)))
    print("全部应用完成")


if __name__ == "__main__":
    main()


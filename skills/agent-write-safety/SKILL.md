---
name: agent-write-safety
description: 在受限 agent 沙箱里安全写文件、改代码的传输通道与操作纪律。当需要创建/修改文件、或在 shell 中传输多行代码时使用。
---

# Agent 写入安全

受限沙箱里反复踩坑后总结的可靠做法：适用于任何把「在 shell 里传多行代码」当写入手段的 harness。
（本文件是这条通道的**唯一一份**；`AGENTS.md` 只留指针。`tools/apply_edits.py` 是它的行式封装。）

## 一、唯一可靠的传输通道：Python via stdin

工具会把命令包装成 `Invoke-Expression "..."`，其中换行变反引号-n、双引号变反引号-引号、
美元符变反引号-美元符——**把多行代码塞进命令行，必然被逐层转义破坏**。正确做法是 payload
只经过 PS here-string，然后走 stdin，中间没有引号层：

    $code = @'
    # 这里可以是任意 Python 代码：
    # 单引号 双引号 美元符 反引号 三引号 反斜杠 全都不用转义
    ' @
    $code | & .venv/Scripts/python.exe -

**不可靠的方式**：`Invoke-Expression` / `python -c` 里塞多行代码（引号层破坏它）；
超长 here-string（>5KB 有截断风险，失败时表现为「无任何输出」）。
**bash / heredoc 视环境而定**——2026-09 在本 harness 上起不来（`CreateFileMapping … Win32 error 5`）：
换环境先用一条无害命令（`echo ok`）实测，别照抄结论。

→ 判据是**通道里没有引号层**；`tools/apply_edits.py`（行式清单）比手写脚本更省事。

## 二、改代码的纪律（比通道更重要）

1. **不做多行字符串匹配**：只用单行锚点做行级插入 / 删除 / 替换
2. **写前必读锚点**，不靠记忆——同一行文本常出现多次
   （`os.makedirs(self.cache_dir, exist_ok=True)` 在一份文件里就有好几处）
3. **唯一性断言** + **全部断言通过才写盘**：断言放在写盘前，失败时磁盘不动、`git status` 干净。
   多文件时按文件逐个写，前一个已落盘**不会**回滚
4. **改完立刻 import 冒烟**（`python -c "import 模块名"`），一次只做一处改动
5. **优先结构定位**（正则找 def 边界、AST），而不是精确行号

→ 这五条与 AGENTS #6（改前 `git status` 干净、改完跑 import 与全量测试）是一套。

## 三、高频陷阱速查

| 陷阱 | 症状 | 对策 |
| :--- | :--- | :--- |
| `from __future__` 位置 | `SyntaxError: must occur at the beginning` | 往文件头插代码前先确认有没有它 |
| 反缩进方向搞反 | 类方法掉到模块级，`hasattr(Class, m)` 为假 | 包装函数内嵌 def 的层级等于类体层级，不要 dedent |
| 三引号嵌套 | 外层字符串提前闭合 | 拼接生成 Python 时用 `#` 注释代替 docstring |
| 换行写成字面量 | 文件里出现反斜杠-n 文本 | 用 `chr(10)` |
| **行尾被翻成 CRLF** | **不报错**：`git diff` 两边归一化看不见，只在 `git add` 时 warning，提交后 `git status` 长期显示 ` M`（内容与 HEAD 逐字节相同，是 stat 缓存） | 改已有文件用 `read_bytes` / `write_bytes`；整体重写（编辑工具、格式化、脚本）后**数一遍** `open(p,'rb').read().count(b'\r\n')`，本仓库除个别 Windows 脚本外全是 LF |
| 目标目录不存在 | `Could not find a part of the path` | 先 `os.makedirs(d, exist_ok=True)` |
| commit message 含反引号或美元符 | `fatal: Invalid path` | 提交信息里不写反引号和美元符 |
| 命令过长 | 随机解析失败、无任何输出 | 拆成多次调用 |

→ 这张表按现象索引；**CRLF 那条最容易漏**——它不报错、`git diff` 也看不见，改完就数一遍行尾。

## 四、沙箱本身的限制（2026-09 快照：Windows + 受限 harness——换环境先重测）

- **部分目录只读**：Python 也抛 `PermissionError`。搬迁用「读源 + 写到新位置 + `git rm --cached`」，别用 move
- **能改名不能删内容**：整个目录可以 rename，目录内文件删不掉
- **`%TEMP%` 可能不可写**：`tempfile.TemporaryDirectory` 会失败，测试要显式用仓库内临时目录

→ 三条都是 harness 属性、不是本仓知识：换环境先重测（bash 那条同理，见 §一）。

## 五、可复用的应用器

`tools/apply_edits.py` 把「改代码」降级成「写行式清单」（`>>>FILE` + `>>>AFTER` / `>>>BEFORE` /
`>>>REPLACE` / `>>>DELETE`），锚点不唯一直接报错、不会写半个文件；清单格式与语义在脚本头部。

→ 能用它就别手写脚本；要生成复杂内容（新文件、大段重排）时才回到 §一 的 stdin 通道。

---
name: agent-write-safety
description: 在受限 agent 沙箱里安全写文件、改代码的传输通道与操作纪律。当需要创建/修改文件、或在 shell 中传输多行代码时使用。
---

# Agent 写入安全

本文件记录在一个受限沙箱里反复踩坑后总结出的可靠做法。适用于任何
把「在 shell 里传多行代码」当作写入手段的 agent harness。

## 一、唯一可靠的传输通道：Python via stdin

工具会把命令包装成 `Invoke-Expression "..."`，其中换行变反引号-n、双引号变反引号-引号、
美元符变反引号-美元符。**把多行代码塞进命令行，必然被逐层转义破坏。**

正确做法——payload 只经过 PS here-string，然后走 stdin，中间没有引号层：

    $code = @'
    # 这里可以是任意 Python 代码：
    # 单引号 双引号 美元符 反引号 三引号 反斜杠 全都不用转义
    ' @
    $code | & .venv/Scripts/python.exe -

（上面 end 标记实际写作单引号加 @，此处拆开是为了避免嵌套。）

**已验证逐字节往返的字符**：单引号、双引号、$HOME $1、反引号、三引号、
反斜杠 w/d/n、连续两个单引号、@ 加单引号、%s %d、花括号方括号圆括号。

**不可靠的方式**：

- 把多行代码放进 `Invoke-Expression` 或 `python -c`，引号层会破坏它
- bash / heredoc：本沙箱里 bash 起不来（`CreateFileMapping ... Win32 error 5`）
- 超长 here-string（>5KB）：有截断风险，且失败时表现为「无任何输出」

## 二、改代码的纪律（比通道更重要）

1. **不做多行字符串匹配**，只用单行锚点做行级插入 / 删除 / 替换
2. **写前必读锚点**，不靠记忆。同一行文本常出现多次，
   例如 `os.makedirs(self.cache_dir, exist_ok=True)`、
   `if self.cache_dir:` 在同一文件里都各有多处
3. **唯一性断言**：`assert lines.count(anchor) == 1`
4. **全部断言通过才写盘**——中途失败时磁盘不动，`git status` 保持干净。
   这条在实战中救过多次：三次锚点找错都在写盘前被拦住
5. **改完立刻 import 冒烟**：`python -c "import 模块名"`
6. **一次一处改动**。一次下 5 处，一处锚点错就全废；拆开做反而更快
7. **优先用结构定位**（正则匹配 def 边界、AST），而不是精确行号

## 三、高频陷阱速查

| 陷阱 | 症状 | 对策 |
| :--- | :--- | :--- |
| from __future__ 位置 | SyntaxError: must occur at the beginning | 往文件头插代码前先确认有没有它 |
| 反缩进方向搞反 | 类方法掉到模块级，hasattr(Class, m) 为假 | 包装函数内嵌 def 的层级等于类体层级，不要 dedent |
| 三引号嵌套 | 外层字符串提前闭合，生成无效占位代码 | 拼接的 Python 里用 # 注释代替 docstring |
| 换行写成字面量 | 文件里出现反斜杠-n 文本 | 用 chr(10) |
| read_text/write_text 往返改已有文件 | **静默**把整个文件的行尾转成 CRLF（Windows 上 write_text 把 \n 写成 \r\n，read_text 又照常读回来，全程不报错） | 改已有文件用 `read_bytes`/`write_bytes`；非要用文本模式就显式 `newline=""`。改完对比一下兄弟文件的行尾习惯 |
| 目标目录不存在 | Could not find a part of the path | 先 os.makedirs(d, exist_ok=True) |
| commit message 含反引号或美元符 | fatal: Invalid path | 提交信息里不写反引号和美元符 |
| 命令过长 | 随机解析失败，无任何输出 | 拆成多次调用 |

## 四、沙箱本身的限制

- **部分目录只读**：表现为 Python 也抛 PermissionError。
  搬迁文件用「读源 + 写到新位置 + git rm --cached」，不要用 move
- **能改名不能删内容**：整个目录可以 rename，但目录内文件删不掉
- **%TEMP% 可能不可写**：tempfile.TemporaryDirectory 会失败，
  测试要显式指定仓库内临时目录
- **bash 存在但不可用**：`C:/Program Files/Git/bin/bash.exe` 存在，
  但 fork 时 CreateFileMapping 被拒，不要指望它

## 五、可复用的应用器

`tools/apply_edits.py` 把「改代码」降级成「写行式清单」：

    >>>FILE core/checker.py
    >>>AFTER
    <唯一锚点行>
    ---
    <新增行>
    <<<

支持 `>>>AFTER` / `>>>BEFORE` / `>>>REPLACE` / `>>>DELETE`，
锚点不唯一直接报错，全部断言通过才写盘。

## 六、给 harness 的最小提示词

> 修改文件时，优先写一个完整 Python 脚本，经 stdin 传给解释器执行。
> 脚本内先读取文件、用唯一性断言定位锚点、全部断言通过后再写回。
> 不要在 shell 的多行字符串里拼接代码。

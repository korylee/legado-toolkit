# Skills 索引

本目录沉淀可复用的技能文档：每个子目录一个 SKILL.md，带 YAML frontmatter（name /
description），可被 Claude Skills、DeepSeek harness、Codex 等直接加载。
**这里是技能的唯一索引**（`AGENTS.md` 只留一行指针）。

| Skill | 用途 | 什么时候用 |
| :--- | :--- | :--- |
| agent-write-safety | 受限沙箱里的写入通道与改代码纪律 | 需要创建/修改文件、或传多行代码时 |
| legado-book-source | Legado 书源规则分析、生成、调试 | 新增/修复一个漫画或小说站时 |
| legado-source-toolchain | SQLite 管理库（迁移 / 对拍）、规则回放器的边界、AI 修复循环 | 批处理书源、归因、修复 |
| legado-source-lessons | 本项目的架构决策与踩过的坑 | 改动核心逻辑前，避免重犯 |

## 给 agent harness 的接入方式

- **Codex**：读根目录的 `AGENTS.md`（已索引本目录）
- **Claude Skills**：把 `skills/` 作为 skills 根目录，每个子目录自动被发现
- **其他 harness**：直接把对应 SKILL.md 内容塞进 system prompt

## 写作约定

**只有一条**：按 `AGENTS.md` 硬性约定 #23 写（注释与长期文档同构的三问 + 论证型文档每节末尾
必须有一行 `→` 结论）。写「症状 → 根因 → 对策」，坑要记下**失败现象**便于匹配。形状体检跑
`tools/kb_stats.py`——**别在这里复述那三问**。

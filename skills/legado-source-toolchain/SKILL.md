---
name: legado-source-toolchain
description: 本仓库 Legado 书源工具链的用法——SQLite 管理库、规则回放器的边界、AI 提议与验收的边界。
---

# Legado 书源工具链

## 一、SQLite 管理库（事实来源）

**分工**：SQLite = 管理库；JSON = 交付格式。`Store` 的接口（`upsert_sources` / `query` /
`checks_map` / `save_checks` / `export_json` / `backup` / `create_job`）看 `core/store.py`——
**不在这里抄第二份签名**。

**设计要点**：`raw_json` 存完整原文（导出无损）；只把要 WHERE / ORDER BY 的字段抽成列；
规则不拆表；视图 `v_sources` = 源 LEFT JOIN 最近一次校验。缓存后端是 SQLite（`checks`
表）；备份对象就是 `data/sources.sqlite3`。

→ 给 Legado 的 JSON 永远由 `Store.export_json` 生成，别拿它当事实来源（AGENTS #2）。

## 二、规则回放器（core/rules/replayer.py）

**不支持的语法要返回明确原因，不是空列表**（`@js:`、`<js>`、`@xpath:`、`||` 备选、JSONPath
递归 `..`…）——**完整清单以 `parse_rule(...).unsupported` 的返回为准**，每条未实现的分支
都带自己的原因码；别在文档里维护第二份枚举。调用方据此判「无法验证」，不是「规则失效」。

    from core.rules.replayer import extract_all_ex, parse_list, parse_field
    values, err = extract_all_ex(html, rule)   # err 非空 = 无法回放

`@html`（没有冒号）是**取值动作**不是前缀（带冒号那支已删，见 `replayer.RULE_PREFIXES`）。

→ 这是 AGENTS #4 的落点：**「我们做不到」必须说出来**，不能被读成源坏了。

## 三、AI 提议（只有调试工作台一条路）

AI 只做**单步提议**：

    POST /api/rules/suggest-rule      # 只提议；dry_run 那趟免费（回放器初筛，不发模型请求）
    POST /api/rules/verify-candidate  # 用户显式点击才跑，本机引擎验收这一条

**模型只提议，验收一律由本机引擎完成**；回放器只用在不花钱的初筛上（AGENTS #3）。
失效归因看跑批校验的明细（自带归因，结论落在管理库），没有独立命令。

→ 花钱与验收都在用户点击之后；回放器不是第二个健康事实（lessons §七十三）。

## 四、一次批量的完整动作

    # 动手前先单独备份管理库（data/sources.sqlite3 不在版本库里）
    Copy-Item data/sources.sqlite3 "data/backups/sources_$(Get-Date -Format yyyyMMdd_HHmm).bak"
    # 校验在 Web 管理台：书源列表的「全量校验 / 校验选中」（本机引擎）

→ 先备份、跑完看分布；**「频繁跑全量会被封 IP」是硬约束**，能跑增量就别跑全量。

---
name: legado-source-toolchain
description: 本仓库 Legado 书源工具链的用法——SQLite 管理库（含迁移与对拍）、规则回放器的边界、AI 修复循环。命令清单与典型参数见 README 的「按场景查命令」。
---

# Legado 书源工具链

## 一、SQLite 管理库（事实来源）

**分工**：SQLite = 管理库；JSON = 交付格式。`Store` 的接口（`upsert_sources` / `query` /
`checks_map` / `save_checks` / `export_json` / `backup` / `create_job`）看 `core/store.py`——
**不在这里抄第二份签名**。

**设计要点**：`raw_json` 存完整原文（导出无损）；只把要 WHERE / ORDER BY 的字段抽成列；
规则不拆表；视图 `v_sources` = 源 LEFT JOIN 最近一次校验。

迁移与对拍（都不联网）：

    python -m core.store_migrate migrate --reset   # 导入并自动做一致性校验
    python -m core.store_migrate verify            # 比对源数与类型分布，全一致才算成功

缓存后端是 SQLite（`checks` 表）；旧 NDJSON 后端与对拍脚本随本地校验链一起退场
（2026-09-22），`--legacy-cache` 那类开关已不存在。



→ 给 Legado 的 JSON 永远由 `Store.export_json` 生成，别拿它当事实来源（AGENTS #2）。

## 二、规则回放器（core/rules/replayer.py）

**不支持的语法要返回明确原因，不是空列表**（`@js:`、`<js>`、`@xpath:`、`||` 备选、JSONPath
递归 `..`…）——**完整清单以 `parse_rule(...).unsupported` 的返回为准**，每条未实现的分支
都带自己的原因码；别在文档里维护第二份枚举。调用方据此判「无法验证」，不是「规则失效」。

    from core.rules.replayer import extract_all_ex, parse_list, parse_field
    values, err = extract_all_ex(html, rule)   # err 非空 = 无法回放

`@html`（没有冒号）是**取值动作**不是前缀（带冒号那支已删，见 `replayer.RULE_PREFIXES`）。

→ 这是 AGENTS #4 的落点：**「我们做不到」必须说出来**，不能被读成源坏了。

## 三、AI 修复循环

    $env:LEGADO_LLM_API_KEY = "sk-..."       # 没配 key 时自动降级成 dry_run
    python cli/main.py diagnose -i x.json -o out/diagnose.md --only-dead
    python cli/main.py repair   -i x.json --report out/repair.md
    python cli/main.py repair   -i x.json --write -o data/x_fixed.json

闭环是「抓证据 → 模型提议 → 回放验证 → 失败差异回喂重试」；**模型只提议，验收一律由回放器
完成**，不通过不落地（AGENTS #3）。

→ 花钱那一步永远在 `--write` 之前；`dry_run` 那趟不花模型。

## 四、一次批量的完整动作

    # 动手前先单独备份（candidates.json 不在版本库里）
    Copy-Item data/candidates.json "data/backups/candidates_$(Get-Date -Format yyyyMMdd_HHmm).json"
    # 校验在 Web 管理台：书源列表的「全量校验 / 校验选中」（本机引擎）
    #   cli 的 check / organize / report / run 已于 2026-09-22 退场（本地校验链收成一台引擎）

→ 先备份、跑完看分布；**「频繁跑全量会被封 IP」是硬约束**，能跑增量就别跑全量。

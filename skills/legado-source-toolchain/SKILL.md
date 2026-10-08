---
name: legado-source-toolchain
description: 本仓库 Legado 书源工具链的用法——SQLite 管理库、规则由谁执行、AI 提议与验收的边界。
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

## 二、规则由谁执行

**只有一台执行者：本机 App 引擎**（`core/jvm_debug`）或真机（`core/app_debug`）。
Python 侧不解释 Legado 规则——曾经的本地回放器已随「引擎是唯一执行者」删除，同一份
页面上两套解释器必然把「我们不会算」说成「规则不好」（AGENTS #4）。

唯一残留的规则文本解析是给**提示词取景框**用的：`core.repair.suggest.focus_selector`
把首段的 `class.` / `id.` / `tag.` 简写转成 CSS，认不出就退回整篇。它不取值、不判定。

→ 候选的真伪一律由引擎验收（`POST /api/rules/verify-candidate`）；不支持的语法由引擎
自己给原因。

## 三、AI 提议（只有调试工作台一条路）

AI 只做**单步提议**：

    POST /api/rules/suggest-rule      # 只提议；dry_run 那趟免费（按候选样本挑一遍，不发模型请求）
    POST /api/rules/verify-candidate  # 用户显式点击才跑，本机引擎验收这一条

**模型只提议、不做本地判定**；免费那趟只做样本比对（`core/repair/suggest.preselect`），
不跑规则、不调模型（AGENTS #3）。失效归因看跑批校验的明细（自带归因，结论落在管理库），
没有独立命令。

→ 花钱与验收都在用户点击之后（lessons §七十三）。

## 四、一次批量的完整动作

    # 动手前先单独备份管理库（data/sources.sqlite3 不在版本库里）
    Copy-Item data/sources.sqlite3 "data/backups/sources_$(Get-Date -Format yyyyMMdd_HHmm).bak"
    # 校验在 Web 管理台：书源列表的「全量校验 / 校验选中」（本机引擎）

→ 先备份、跑完看分布；**「频繁跑全量会被封 IP」是硬约束**，能跑增量就别跑全量。

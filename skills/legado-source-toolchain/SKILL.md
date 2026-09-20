---
name: legado-source-toolchain
description: 本仓库 Legado 书源工具链的用法——13 个 CLI 命令、SQLite 管理库、规则回放器、失效归因、AI 修复循环。批量处理书源时使用。
---

# Legado 书源工具链

## 一、目录结构

    core/          领域逻辑（零 Web 依赖）
      models loader sanitize organizer reporter checker tags
      constants.py  fetch.py  analyzer.py  build.py  verify.py
      urls.py  paths.py  dns_check.py  quality.py  settings_store.py
      rules/replayer.py       Legado 规则回放器
      repair/{llm,evidence,loop}.py   AI 修复循环
      reclassify.py  store.py  store_migrate.py  cache_parity.py  dups.py
    services/                加源/合并流程（add_source.py、merge_sources.py）
    cli/main.py              13 个命令入口
    data/                    运行时数据（gitignore）
    tools/apply_edits.py     行级补丁应用器

> 目录清单**只列常用的那几个**（`ls core/` 为准）。它是文档，不跟着模块增删走——
> 具体有哪些模块，看代码。

## 二、命令速查

| 命令 | 用途 |
| :--- | :--- |
| `check` | 高并发联网校验（健康度 + 星级） |
| `organize` | 按 类型+生命周期状态 重建分组 |
| `report` | Markdown 诊断报告 |
| `run` | 一条龙：校验 → 整理 → 报告 |
| `add` | 给搜索 URL，自动推断规则生成书源 |
| `reclassify` | 按实测信号重判类型（修正漫画/小说错标） |
| `diagnose` | 失效归因：死站/需翻墙/规则漂移/站点转型/需登录 |
| `dups` | 找重复源（同站点 / 镜像 / 同名），只读 |
| `repair` | AI 修复循环：抓证据 → 提议 → 回放验证 → 重试 |
| `merge` / `prepare` / `dedupe` | 合并 / 生成候选版 / 去重 |
| `sanitize` | 清洗字段类型脏值（Legado 导入前必跑） |

统一入口：`python cli/main.py <命令> -h`

## 三、SQLite 管理库

**分工**：SQLite = 管理库；JSON = 交付格式。

    from core.store import Store
    with Store() as st:              # 默认 data/sources.sqlite3
        st.upsert_sources(srcs)      # 幂等，按规范化 URL 覆盖
        st.query(source_type=2, health="dead", limit=50, offset=0)
        st.checks_map()              # {url: 最近一条校验}，字段兼容旧 NDJSON
        st.save_checks(items)
        st.export_json("out/x.json") # 重新生成给 Legado 的 JSON
        st.backup("data/backups/x.sqlite3")   # VACUUM INTO，不能用裸拷
        st.create_job("j1", "check", total=100)

**设计要点**：`raw_json` 存完整原文（导出无损）；只把需要 WHERE/ORDER BY
的字段抽成列；规则本身不拆表。视图 `v_sources` = 源 LEFT JOIN 最近一次校验。

**迁移与校验**：

    python -m core.store_migrate migrate --reset   # 导入并自动做一致性校验
    python -m core.store_migrate verify

一致性校验比对四项分布：源数 / 类型 / 健康 / 星级。**四项全一致才算迁移成功。**

**缓存后端双跑对比**（不联网）：

    python -m core.cache_parity --sample 400

对比 SQLite 与 NDJSON 两种后端的命中矩阵与 16 个字段，退出码 0=等价。

## 四、缓存后端开关

`AsyncChecker` 默认走 SQLite；退回旧 NDJSON：

    python cli/main.py check -i x.json --legacy-cache
    # 或设环境变量 LEGADO_LEGACY_CACHE=1

## 五、规则回放器（core/rules/replayer.py）

支持：`class.`/`id.`/`tag.` 简写、`@` 链式、`.0`/`.-1` 索引、
`##正则##替换`（支持 $1）、`@css:`/`@json:`、JSONPath 子集
`$.data.list[*].name`、取值动作 `text`/`textNodes`/`ownText`/`html`/任意属性。

> **没有 `@html:` 前缀**：Legado 的 `@html`（没有冒号）是**取值动作**，不是前缀。
> 带冒号的那个当年被误放成合法前缀（返回整份响应体），已随前缀分支一起删掉
> （见 `replayer.RULE_PREFIXES` 的注释与 `parse_rule` 的 `unsupported` 文案）。

**不支持的语法返回明确原因而不是空列表**（`@js:`、`<js>`、`@xpath:`、`||` 备选规则、
JSONPath 递归下降 `..` 等）。**完整清单以 `parse_rule(...).unsupported` 的返回为准**，
不在文档里维护第二份枚举——每条未实现的分支都带自己的原因码。调用方据此判为
「无法验证」而非「规则失效」。

    from core.rules.replayer import extract_all_ex, parse_list, parse_field
    values, err = extract_all_ex(html, rule)   # err 非空 = 无法回放

## 六、AI 修复循环

    $env:LEGADO_LLM_API_KEY = "sk-..."          # 无 key 时自动降级 dry_run
    $env:LEGADO_LLM_BASE_URL = "https://api.deepseek.com/v1"
    $env:LEGADO_LLM_MODEL = "deepseek-chat"

    python cli/main.py diagnose -i x.json -o out/diagnose.md --only-dead
    python cli/main.py repair   -i x.json --report out/repair.md
    python cli/main.py repair   -i x.json --write -o data/sources/x_fixed.json

闭环是「抓证据 → 模型提议 → 回放验证 → 失败差异回喂重试」。
**模型只提议，验收一律由回放器完成**，不通过不落地。

## 七、日常 runbook

    # 全量校验（增量，缓存命中不联网）
    python cli/main.py check -i data/sources/candidates.json -o data/out/checked.json -c 50
    # 整理 + 报告
    python cli/main.py organize -i data/out/checked.json -o data/out/organized.json -r data/check_cache
    python cli/main.py report -i data/out/organized.json -r data/check_cache -o data/out/report.md
    # 校验前的备份（candidates.json 不在 git 里，务必单独备份）
    Copy-Item data/sources/candidates.json "data/backups/candidates_$(Get-Date -Format yyyyMMdd_HHmm).json"

---
name: legado-source-lessons
description: 本项目的架构决策与踩过的坑——规则回放器一致性、失效分桶、URL 规范化、SQLite 选型等。改动核心逻辑前必读，避免重犯。
---

# 本项目的架构决策与踩坑记录

每条都按「症状 → 根因 → 结论」写。改动核心逻辑前请先扫一遍。

## 一、规则回放器必须和 Legado 语义一致

**症状**：脚本验证通过的源，导入 App 后不能用；反之 App 能用的源被判失败。

**根因**：早期的 `apply_css_rule` 只做

    selector, _, attr = rule.rpartition("@")   # 取最后一个 @
    soup.select(selector)                      # 然后 el.get(attr)

它**不支持 Legado 的 `class.` / `tag.` 前缀简写、`##正则##替换`、`@js:`、JSONPath**。
一条 `tag.a@text##作者：##` 的规则会被当成属性名 `text##作者：##`，取到空字符串——
于是「内容为空」被误判成「规则失效」。

**结论**：验证器和执行引擎必须是同一套语义。规则解析统一走
`core/rules/replayer.py`，且**不支持的语法要返回明确原因**，
由调用方判为「无法验证」而不是「失效」。误杀比漏放更贵。

## 二、静默失败是最贵的 bug

**症状**：`--probe-depth 2/3` 跑完，`toc_complete` / `content_ok` 全是 `None`，
深度验证看起来「没查出来」，实际**一个额外请求都没发**。

**根因**：`requirements.txt` 里漏了 `beautifulsoup4`。
`apply_css_rule` 里 `from bs4 import BeautifulSoup` 写在 `try` 之外，
一调用就抛 `ModuleNotFoundError`；上层 `except Exception` 把它吞了，
统一变成「无法验证」。连带 `_confirm_hit` 的异常分支返回 `True`，
命中判定退化成「响应体里出现关键词就算命中」。

**结论**：所有「无法验证」的返回路径都要能说出原因，并且要有
`err` / `fail_reason` 字段一路透传到报告。**异常吞掉之后必须留下痕迹。**

## 三、失效必须分桶，不能只有一个 DEAD

**症状**：报告里 `❌失效` 一大堆，人没法判断哪些该删、哪些该修。

**根因**：`checker` 只判「域名通不通 + 搜索有没有命中测试词」。
三种完全不同的情况挤在一个桶里：

- 域名注销的死站（该删）
- 域名活着但页面改版、规则过期的源（**可修，不该删**）
- 域名活着但已转型的站点（该改类型）

**结论**：拆成四类归因——`死站` / `规则漂移` / `站点转型` / `需验证`。
判据是「域名可达性 × 反爬特征 × 搜索页能否解析 × 实测类型」的组合。
`diagnose` 命令就是这个设计的实现。

## 四、类型必须实测，不能信声明值

**症状**：明明是漫画站，被标成小说源。

**根因**：`add` 命令的类型来自 `--type`（默认 `novel`），
`bookSourceType` 从未被实测校验过。默认值一路写进库。

**结论**：`reclassify` 用「静态信号 + 首页/正文实测」反推类型：
域名/名称关键词、是否有 `ruleContent.image`、正文规则取的是图片还是文本、
`imageStyle=FULL`。累计打分 ≥3 且高于另一方向才改判，否则保持原样。
**实测优先于声明。**

## 五、URL 必须规范化后再作为 key

**症状**：迁移后 `v_sources` 里 571 个源查不到校验记录，
健康度分布比报告少 571 条。

**根因**：`upsert_sources` 存的是 `_normalize_url(bookSourceUrl)`，
而 `save_checks` 存的是缓存里的原始 `url`。两者不是同一个字符串，
SQL JOIN 自然对不上。

**结论**：**任何跨表/跨库关联的 URL，两侧都必须用同一套规范化函数**
（`core/loader._normalize_url`）。这类 bug 不会报错，只会静默丢数据——
所以**迁移必须做一致性校验**（源数/类型/健康/星级四项分布对比），
不做这步它就一直藏在库里。

## 六、缓存去重键也要规范化

**症状**：两个缓存目录合计 10166 条，归一后只剩 5591 条。

**根因**：同一站点存在多个 URL 变体（带不带尾斜杠、http/https、
带不带 `#` 锚点），`load_cache` 按原始 url 去重，等于没去。

**结论**：去重和查询都用规范化 URL。合并掉 4575 条重复后，
`cache_parity` 的命中矩阵才从「185 单侧差异」变成「200/200 完全对齐」。

## 七、SQLite 而不是 MySQL

**理由**：单用户本地工具。Python 标准库自带、零运维、单文件、
`VACUUM INTO` 就能备份。3734 源 + 5591 校验 ≈ 25MB，
离 PostgreSQL/MySQL 的合理起步量级差几个数量级。

**必须配的 PRAGMA**：

    journal_mode=WAL          # 读写并发（Job 写的同时前端能查）
    synchronous=NORMAL
    foreign_keys=ON
    busy_timeout=5000         # 避免 backend/job 撞车直接 database is locked
    temp_store=MEMORY

**关键约束**：**DB 文件不能放网络盘（SMB/NFS）**。SQLite 依赖文件锁，
在网络挂载上会静默损坏。若服务跑在 NAS 上，DB 放 NAS 本地卷，
备份产物才放网络盘。

**什么时候才该换**：① 多机多人同时写；② `busy_timeout` 频繁超时；
③ 单库到几十 GB。单机单用户不会碰到。

## 八、状态归一：别让三个 store 靠代码 merge

**症状**：一个源的信息散在 `candidates.json`（规则/分组/类型）、
`check_cache/*.ndjson`（健康/星级）、`book_sources.sqlite3`（审批），
`organizer.py` 存在的意义就是把这几个拼起来。

**结论**：一行一个源放进 `sources` 表，校验结果进 `checks` 表，
`v_sources` 视图直接 LEFT JOIN 最近一次。merge 逻辑消失。

## 九、AI 只提议，验收必须由回放器做

**结论**：`repair` 的闭环是

    抓证据 → 模型提议 → legado_rules 回放验证 → 失败差异回喂 → 重试

让模型直接输出书源、然后相信它，和早期的启发式推断一样不可靠。
**验证证据要压缩**：把几 MB 的 HTML 压成 90 行缩进 DOM 大纲，
模型只做「修正」而不是「从零发明」选择器。

## 十、搬迁与重构的教训

**症状 → 根因 → 结论**：

- **反向依赖**：`checker.py`（核心）为了一个 `_abs_url` 依赖了 57KB 的
  `add_source.py`。→ 抽出 `core/urls.py`。**核心层不应依赖 CLI 工具。**
- **`from __future__` 位置**：往 `cli/main.py` 头部插 `sys.path` 引导，
  直接把 `from __future__ import annotations` 挤到非法位置。→
  插代码前先检查文件头。
- **venv 可以搬**：Windows 上 `.venv` 移动后仍可用（`pyvenv.cfg` 里的
  home 是绝对路径，没变）。但**别在搬迁脚本里做**，单独一步、单独验证。
- **`tests/` 目录只读**：沙箱限制。搬迁时用「读源 + 写到新位置 +
  `git rm --cached`」，不要用 move。

## 十一、验证手段的优先级

1. **单元测试**（离线、可注入假依赖）——`repair` 循环有 8 项离线测试，
   注入假 LLM 与假验证器，不联网就能验完整闭环
2. **import 冒烟**——每次改完立刻 `python -c "import 模块"`
3. **一致性校验**——迁移后四项分布对比
4. **双跑对比**——切换后端后 `cache_parity` 验等价
5. **真实联网跑小样本**——最后才做，且比分布不比逐条（网络有抖动）

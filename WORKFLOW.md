# Legado 书源管理 · 完整链路

> 本文件定义「新增源 / 外部源 → 校验 → 整理 → 报告 → 交付」的完整工作流。
> 命令在**仓库根目录**下执行（PowerShell）。
>
> **两条入口，事实来源只有一个**：
> - **Web 管理台**（`python -m backend`，浏览器开 `/`）：日常主用——源的增删改查、
>   导入、校验、整理、报告都在界面上；数据落在 **`data/sources.sqlite3`**。
> - **CLI**（`python cli/main.py <命令>`）：批量与离线场景——对**文件**跑
>   check/organize/report/dedupe 等，产物默认写在当前目录（约定放 `out/`）。
>
> **JSON 是交付格式，不是事实来源**（AGENTS #2）：管理库才是。给 Legado 的 JSON 由
> 管理库导出；CLI 的文件式流程与它互不影响，两边别互相覆盖。

---

## 一、链路总览

```
                    ┌───────────────────────────────┐
                    │      三个入口（主要在 Web）    │
                    └───────────────────────────────┘
    ① 新增源(URL)          ② 外部来源              ③ 已有的源
    Web「新增」/ add        Web「导入」对话框         （管理库里）
        │                      │                         │
        ▼                      ▼                         │
   自动推断规则           三分类入库：                    │
                      新 URL / 重复 / 冲突                │
        └──────────────────────┴────────────┬────────────┘
                                            ▼
                          ┌───────────────────────────────────┐
                          │  管理库 data/sources.sqlite3      │  ← 事实来源
                          └───────────────────────────────────┘
                                            ▼
                          ┌───────────────────────────────────┐
                          │  校验 check                       │  ← 指纹一致且未过期的
                          │  （Web 按钮 / CLI 对文件跑）      │     缓存才复用；出健康度+星级
                          └───────────────────────────────────┘
                                            ▼
                          ┌───────────────────────────────────┐
                          │  整理 organize                    │  ← 按 类型+健康度 重建分组
                          └───────────────────────────────────┘
                                            ▼
                          ┌───────────────────────────────────┐
                          │  报告 report                      │  ← 星级分布/健康分布/问题清单
                          └───────────────────────────────────┘
                                            ▼
                 给 Legado 的 JSON（交付格式） + Markdown 报告（人工审阅）
```

---

## 二、三类来源怎么进最终源

### ① 新增源（给一个搜索 URL，自动推断规则）

```powershell
# 标准做法：URL 走 stdin（规避 shell 破坏 %XX 编码）
echo 'https://www.koudaimh.com/search?q=%E7%BB%8D%E5%AE%8B' |
  python cli/main.py add - --name "口袋漫画" --type manga --no-ask

# 交互向导（推荐新手）：逐项确认名称/分组/类型
echo 'https://www.koudaimh.com/search?q=%E7%BB%8D%E5%AE%8B' |
  python cli/main.py add - --interactive
```

- 自动完成：搜索规则推断 → 详情页目录推断 → 全链路验证（search/bookUrl/toc/content）
- 产物：`auto_added.json`（临时累积区，新源先落这里）
- 生成后走 Web 的「导入」把它并入管理库；不要用 `merge --mode replace` 覆盖管理库里的源。

### ② 外部获取的源（他人分享 / 论坛 / 网络）

**只有一条路：Web 管理台的「导入」对话框**（后端 `backend/api/imports.py`）。
（旧的 CLI `import-sources` / `review-imports` 与它那套独立台账 **2026-09-19 已删**：
它写的是另一个 SQLite 文件、而那个文件在本机根本不存在，批准又写回 `candidates.json`
——都已不是事实来源。）

粘贴外部 JSON，按 URL + 规则指纹三分类：

- **新 URL** → 写入管理库，按原分组推断健康状态（无信号默认「待验证」）。
- **同 URL 同规则** → 重复，跳过。
- **同 URL 不同规则** → 冲突，**不自动覆盖**；按对话框里选的策略处理
  （`keep` 保留库里那份 / `overwrite` 用外部覆盖），冲突源原样存到
  `data/imports/conflicts/` 供回查。

导入后照常走「校验 → 整理 → 报告」；要人工确认的外部规则，跑一次
`check` 看结果，确实更好再用导入对话框的 `overwrite` 策略重新导入一次。

### ③ 已有的源（管理库里的源；CLI 侧对应 `candidates.json`）

- **事实来源是管理库 `data/sources.sqlite3`**（Web 侧）；CLI 侧对应的是
  `candidates.json`。`out/` 下的 `checked.json`/`organized.json`、`archive/` 里的
  `final_*.json` 都是**处理或导出产物**，不能反向当作主库覆盖回去。
- 库里保留可用、待验证、需翻墙的源；校验判死的源会被标记，**删不删由人定**
  （回收站里留着历史版本，可回滚）。

---

## 三、合入后的处理链（每次新增/合入后必跑）

> **Web 上不用敲这些命令**：管理台的「校验 / 整理 / 报告」按钮跑的是同一条链
> （同一个 `core/` 实现），结论写回管理库。本节是**离线/批量**形态——操作对象是
> JSON 文件。**校验缓存默认在管理库**（`data/sources.sqlite3` 的 `checks` 表）——
`--cache-dir` / `-r` 只在 `--legacy-cache` 下才被读写，默认传了也是空转。

附件或自用源需要高优先级时，可一条命令生成三个产物：

```powershell
python cli/main.py prepare -i "D:\DownloadsshareBookSource(1).json" -i candidates.json --prefer-input first
```

- `candidates-merged.json`：附件优先的去重合并底稿；
- `candidates-full.json`：完整候选版，分组为类型 + 状态；
- `candidates-fast.json`：仅保留当前标记为可用的快速使用版。

未联网复检时，状态只从旧分组迁移：明确带可用标记的归为“可用”，无法确认的全部归为“待验证”。

```powershell
# 1. 校验：仅 URL、规则指纹一致且缓存未过期的源可跳过联网
python cli/main.py check -i candidates.json -o out/checked.json --cache-dir check_cache -c 50 -t 8

# 网络刚恢复，绕过旧缓存并用本次成功结果更新缓存
python cli/main.py check -i candidates.json -o out/checked.json --refresh-cache --cache-dir check_cache

# 临时完全不使用缓存（不读也不写，适合一次性诊断）
python cli/main.py check -i candidates.json -o out/checked.json --no-cache

# 2. 整理（按 类型+生命周期状态 重建分组，-r 恢复缓存里的星级数据）
python cli/main.py organize -i out/checked.json -o out/organized.json -r check_cache

# 3. 报告（-r 恢复缓存星级，报告才有星级分布/优质TOP）
python cli/main.py report -i out/organized.json -r check_cache -o out/final_report.md
```

> 等价的一条龙：`python cli/main.py run -i candidates.json -o out/checked.json`
> （效果相同，但中间产物文件名固定为 checked.json / report.md，均落在 out/）

**最终交付两件东西（均在 `out/`）：**
| 文件 | 用途 |
|---|---|
| `out/organized.json` | 分组已重建，**导入 Legado** 的书源文件 |
| `out/final_report.md` | 人工审阅报告（哪些源挂了/被墙/重复/优质） |

---

## 四、整理后的源怎么分类（organize 规则）

分组名 = **`{类型},{生命周期状态}`**，例如 `📖小说,可用`、`📖小说,待验证`。
星级只作为书源质量字段，不再作为分组维度，避免分组数量随检测波动。

### 第一层：类型（bookSourceType）
| Emoji | 类型 | 数值 |
|---|---|---|
| 📖 | 小说 | 0 |
| 🎧 | 听书 | 1 |
| 🎨 | 漫画 | 2 |
| 📥 | 下载 | 3 |

### 第二层：健康度（联网校验结果）
六档按**下一步动作**划分——两档该不该并成一档，看动作是否相同：

| 标签 | 含义 | 下一步 |
|---|---|---|
| ✅可用 | 域名通 + 搜索有结果 | 直接用 |
| 🔒需登录 | 403/验证码/登录墙等反爬，站点明确拒绝 | 连 App 试 |
| 🌐需翻墙 | 连接被重置/TLS 握手失败（GFW 特征） | 挂代理复测 |
| 🔐证书问题 | 站点可达、只是证书不被信任 | 关证书校验或换 http |
| ❓待验证 | **我们没结论**：超时、校验异常、从未校验 | 跑/重跑一次校验 |
| ❌已失效 | 域名注销 / 服务端 5xx / 入口 4xx | 删 |

> 「未校验」（没有 `checks` 行）不是一档状态，是数据缺失——统计条上是灰 chip。
> 失败**原因**（超时/异常/DNS…）不丢，落在校验记录的 `error` 与详情里。

### 第三层：星级（可用源才有，0~5★，阶梯规则，方案D 分档宽松）
星级是**逐级累加**的阶梯，每一级依赖上一级：

| 星级 | 达标条件 |
|---|---|
| 1★ | 可达：域名通或能访问（含需登录） |
| 2★ | +搜索连通：有搜索规则且搜索请求有响应 |
| 3★ | +弱证据档：命中测试集作品（实测搜索成功）**或** 规则完整（目录+正文规则齐全） |
| 4★ | +目录完整：命中源实测目录章节数 ≥ 参考表阈值（小说 80% / 漫画 60%） |
| 5★ | +正文可用：命中源抽样章节能解析出正文（**非空即通过**；<500 字符会附一句
「正文较短」的注，不因此扣分） |

- **分档逻辑（宽松边界）**：
  - 未命中测试集的源**最高 3★**——测试集仅覆盖大众作品，未命中≠源差，规则齐全即给 3★；
    4★/5★ 保留给有命中实测证据的源
  - 深度验证**无法验证**（XPath/JS 规则、网络失败、参考表缺项）的维度**回退静态规则判定**，
    不误杀规则齐全的源；实测**明确不达标**（False）仍扣分，不误放真坏的源
- **验证深度**由 `--probe-depth` 控制。**一根轴四档，一档对一级星级**（默认 **2** = 搜索）：
  - `1` 主页：只测域名连通（1 个请求/源），目录/正文维度走静态规则判定
  - `2` 搜索：再**实测搜索**是否命中测试作品（+1~2 个请求/源）→ 2★连通 / 3★命中
  - `3` 目录：命中后**实测目录**——解析详情页数章节数与参考表比对（+2 个请求/源）→ 4★
  - `4` 正文：再**抽样一章实测正文**——中位章节，计时 + 文本/图片判定（+1 个请求/源）→ 5★
  - 定期全量审计用 `--probe-depth 4`，日常增量校验用默认 2
  - 合并前是「深度 1/2/3 + 独立的 `--no-search-probe` 开关」，两个旋钮能配出「关掉搜索
    却选目录档」这种永远进不去的组合。旧编号对应新编号：旧 1+搜索开 = 新 2，旧 2 = 新 3，
    旧 3 = 新 4（设置文件自动迁移，见 `core/settings_store._migrate_legacy`）
- 内置参考表（models.py `TEST_TITLES`，数据截至 2026-08）：斗破苍穹 1681 / 凡人修仙传 2446 / 赘婿 1358 / 诡秘之主 1432 / 庆余年 826 章；海贼王 1190 / 火影忍者 700 / 斗罗大陆 750 / 进击的巨人 139 / 龙珠 519 话

### 质量标签（追加在分组后，逗号分隔）
`📖小说,可用,规则完整`
- **规则完整**：目录+正文规则都齐全（静态判定）
（**「原创」标签已退役**：`checker` 会把它当旧缓存遗留剔除，别再依赖它。）
- 搜索命中结果**不再**以「命中《xx》」标签展示；命中信息仅用于星级评分与报告「命中作品」列

---

## 五、标准操作速查（日常三问）

| 场景 | 命令 |
|---|---|
| 新增一个站 | `echo '搜索URL' \| python cli/main.py add - --interactive` |
| 导入外部源 | **Web 管理台「导入」对话框**（CLI 无此命令；冲突按策略处理并存档） |
| 清洗类型脏值（导入前必跑） | `python cli/main.py sanitize -i 外部.json`（缺省就地覆盖） |
| 校验+整理+报告 | `python cli/main.py run -i candidates.json -o out/checked.json` |
| 深度验证审计（目录+正文实测） | `python cli/main.py check -i candidates.json --probe-depth 4 --cache-dir check_cache` |
| 仅可用源精简版 | `python cli/main.py run -i candidates.json --keep-only-ok -o out/checked_ok.json` |
| 只要报告（不重新校验） | `python cli/main.py report -i out/organized.json -r check_cache -o out/report.md` |
| 去重检查 | `python cli/main.py dedupe -i 某文件.json -o 去重后.json` |
| 忘了命令 | `python cli/main.py`（进菜单）或 `python cli/main.py -h` |

---

## 六、维护建议

1. **新增源走 `add`（CLI）或 Web 的「新增」；外部源走 Web 的「导入」**。不要用 `merge --mode replace` 直接覆盖管理库里的源。
2. **规则冲突要人工判断**：先用 `check` 看外部那版的实测结果，确实更好再用导入对话框的 `overwrite` 覆盖；覆盖后指纹变化会强制复检。
3. **缓存会自动过期**：可用源 14 天、其余状态 7 天；其中「需登录」单独 1 天（它常由
   当时的页面启发式判出，锁久了点重校验只会看到复用缓存）；「证书问题」**永不复用**
   （关掉证书校验就能变好，复用等于修了没修）。旧版本缓存也会自动复检。
4. **被墙源**（🌐）可加 `--proxy` 复检：`python cli/main.py check -i x.json --proxy http://127.0.0.1:7890`
   （原先这里写的 `socks5://` 是失实示例——urllib 与 aiohttp 都不认，会直接连接失败。
   代理只支持 `http://` / `https://`。）
   Web 端不必敲命令：**设置 → 校验 → 代理** 里配一次，或在校验前用工具栏的
   「校验参数」只对本次生效。注意 CLI 与 Web 的参数**各自独立**，不共享。
5. 备份只看两处：**管理库 `data/sources.sqlite3`** 与 `data/candidates*.json`。

---

## 七、目录约定

**运行时数据都在 `data/`（已 gitignore）**，可用 `LEGADO_DATA_DIR` 覆盖。核心只有两件：

| 路径 | 用途 |
|---|---|
| `data/sources.sqlite3` | **管理库：事实来源**。源 / 校验缓存 / 任务 / 导出记录 |
| `data/candidates*.json` | 候选源与导出产物（**交付格式**，可单独备份） |

其余运行时目录：

| 路径 | 用途 |
|---|---|
| `data/imports/conflicts/` | Web 导入时规则冲突的源（原样留存，可回查） |
| `data/backups/` | 软删除记录（`deleted.jsonl`）+ 手动备份 |
| `data/out/exports/` | 临时导出快照 |
| `data/app_probe/` | App 实测产物（源清单备份 + searchBook 结果），产生它的脚本已删 |

**CLI 的文件式产物**默认落在**当前目录**（`checked.json`、`check_cache/` …），项目约定
统一放 `out/` 与 `check_cache/`：

| 路径 | 用途 |
|---|---|
| `out/` | 所有 `-o` 产物（`organized.json`/`check_*.json`/`report.md`） |
| `check_cache/` | **遗留** NDJSON 校验缓存：现役缓存在管理库 `checks` 表，这里只在
`--legacy-cache` 与迁移/对拍时才读。**不要再建 `check_cache_full`/`_deep3` 变体** |
| `archive/` | 历史产物归档，**可回溯不删除** |

约定：
- `candidates.json` 之外的书源 JSON（外卖 / 手机导出 / 群分享）一律视为**外部源**，
  走 **Web 导入**，不进仓库根目录。
- 别在根目录堆产物（`checked.json`/`final_*.json`/`shareBookSource.json`）——
  要么 `-o out/...`，要么让 Web 去做。
- 运行前若缺依赖：`uv pip install -r requirements.txt`（在 `.venv/` 下用 `uv run`）。

---

## 八、类型重判定与失效归因（2026-09 新增）

### `reclassify` —— 修正「漫画源被标成小说源」

`add` 命令的类型来自 `--type`（默认 novel），从未实测，所以错标很常见。本命令用
「静态信号（域名/名称/正文规则形态）+ 可选首页实测」反推真实类型。

```powershell
# 只看判定结果，不改文件
python cli/main.py reclassify -i candidates.json --limit 50

# 回写 bookSourceType（之后重跑 organize 刷新分组）
python cli/main.py reclassify -i candidates.json --write
```

判定信号（累计打分，>=3 且高于另一方才改判，否则保持原样）：

| 方向 | 信号 | 分值 |
|---|---|---:|
| 漫画 | 域名含 manhua/manga/comic/acg/dm5... | +3 |
| 漫画 | 名称/分组含 漫画/漫畫/汉化/图集/里番 | +3 |
| 漫画 | 正文规则取图片（`img` / `@src` / `@data-original`） | +2 |
| 漫画 | `imageStyle=FULL` / 章节 URL 含 manhua、comic | +1 |
| 小说 | 域名含 xs/book/shu/biqu/novel... | +2 |
| 小说 | 名称/分组含 小说/书屋/文学/书城/网文 | +3 |
| 小说 | 正文规则取文本且无图片规则 | +2 |

### `diagnose` —— 区分「死站」和「规则漂移」

原先 `check` 只判「域名通不通 + 搜索有没有命中」，三类问题全挤在 `❌失效`：

- 域名注销的死站（该删）
- 域名活着但页面改版、规则过期的源（**可修，不该删**）
- 域名活着但已转型（如漫画站变综合站，该改类型）

`diagnose` 对失效源重新探测，按归因分桶（完整清单以 `core/reclassify.py` 的
`ACTION_OF` 为准）：死站 / 需翻墙 / 需登录 / 规则漂移 / 站点转型 / 疑似可用，
另有兜底的「其他」。

```powershell
# 只探测非可用源，输出 Markdown 报告
python cli/main.py diagnose -i out/checked.json -o out/diagnose.md --only-dead -c 20
```

| 归因 | 含义 | 建议动作 |
|---|---|---|
| 死站 | 域名解析失败 / 连接失败 / 重置 / TLS | 两次明确失败后淘汰 |
| 规则漂移 | 域名可达（200），但搜索跑不出结果或列表解析为空 | **保留**，进 AI 修复队列 |
| 站点转型 | 实测类型与 `bookSourceType` 不符 | 改类型或按新类型重建规则 |
| 需登录 | 403/429/验证码/登录墙 | 保留，人工或改 UA/Cookie 复检 |
| 疑似可用 | 搜索能解析出列表 | 复检一次，可能是缓存过期或临时故障 |

### 规则回放器 `core/rules/replayer.py`

`check`/`add`/`diagnose` 的规则解析统一走 `core/rules/replayer.py`，语义对齐 Legado：

- `class.xxx` / `id.xxx` / `tag.a` 选择器简写
- `class.item@tag.a@href` 链式选择、`.-1` / `.0` 索引
- `##正则##替换`（支持 `$1` 反向引用）
- `@css:` / `@json:` 前缀；JSONPath 子集 `$.data.list[*].name`
- 取值动作 `text` / `textNodes` / `ownText` / `html`（**没有冒号**）/ 任意属性
  > `@html:`（带冒号）**不是前缀**，早已删掉；混了会回空。

**明确不支持的语法会返回原因（而不是静默返回空）**，调用方据此判为「无法验证」
而非「规则失效」，避免误杀（`@js:`、`<js>`、`@xpath:`、`||` 备选规则、JSONPath `..` 等）。
**完整清单以 `parse_rule(...).unsupported` 的返回为准**——每条未实现的分支都带自己的
原因码，不在文档里维护第二份枚举。

> 注意：`beautifulsoup4` 是必需依赖（`pyproject.toml` 已加）。此前缺失导致所有规则
> 解析静默失败、深度验证（`--probe-depth` 3/4）实际未生效。


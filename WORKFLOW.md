# Legado 书源管理 · 完整链路

> 本文件定义「新增源 / 外部源 → 最终源 → 最终报告」的完整工作流。
> 所有命令在 `legado-tools/` 目录下执行（PowerShell）。

---

## 一、链路总览

```
                    ┌─────────────────────────────────────────┐
                    │           输入（三类来源）               │
                    └─────────────────────────────────────────┘
       ① 新增源(URL)          ② 外部获取的源           ③ 候选主库
   add_source 自动推断       (论坛/群分享/他人导出)     candidates.json
        │                        │                          │
        ▼                        ▼                          │
  auto_added.json          source_import.json  ◄───────────┘
        └──────────────┬─────────┘
                       ▼
               ┌───────────────┐
               │ 安全导入批次  │   ← 留存原文件、清洗、URL+规则指纹比对
               └───────────────┘
                       ▼
          新 URL → 待校验；规则冲突 → 待审；同规则 → 仅记录
                       ▼
          人工确认校验或审批冲突后，才写入 candidates.json
                       ▼
               ┌───────────────┐
               │  check 校验   │   ← 指纹一致且未过期的缓存才复用
               │ (check_cache) │      健康度 + 星级 + 被墙检测
               └───────────────┘
                       ▼
               ┌───────────────┐
               │ organize 整理 │   ← 按 类型+健康度+星级 重建分组
               └───────────────┘
                        ▼
                ┌───────────────┐
                │ sanitize 清洗 │   ← 外部文件常带类型脏值(''/[]/字符串数字)
                │  (Gson兼容)   │      必跑，否则 Legado 导入报
                └───────────────┘      IllegalStateException
                        ▼
                ┌───────────────┐
                │  report 报告  │   ← 星级分布/健康分布/问题清单/重复源
                └───────────────┘
                        ▼
             final_all.json（导入 Legado）
             final_report.md（人工审阅）
```

---

## 二、三类来源怎么进最终源

### ① 新增源（给一个搜索 URL，自动推断规则）

```powershell
# 标准做法：URL 走 stdin（规避 shell 破坏 %XX 编码）
echo 'https://www.koudaimh.com/search?q=%E7%BB%8D%E5%AE%8B' |
  python main.py add - --name "口袋漫画" --type manga --no-ask

# 交互向导（推荐新手）：逐项确认名称/分组/类型
echo 'https://www.koudaimh.com/search?q=%E7%BB%8D%E5%AE%8B' |
  python main.py add - --interactive
```

- 自动完成：搜索规则推断 → 详情页目录推断 → 全链路验证（search/bookUrl/toc/content）
- 产物：`auto_added.json`（临时累积区，新源先落这里）
- 生成后使用 `import-sources` 导入候选管理链路；不要用 `--to` 直接写入候选主库。

### ② 外部获取的源（他人分享 / 论坛 / 网络）

```powershell
# 首次迁移：以当前清理后的候选库建立唯一主库（仅做一次）
Copy-Item final_clean.json candidates.json

# 外部源只允许安全导入：原文件会留存，候选库不会被直接改写
python main.py import-sources --candidate candidates.json -i source_import.json --registry book_sources.sqlite3 --raw-dir imports/raw

# 查看新源与同 URL 规则冲突；先用现有 check 校验外部批次，再人工确认
python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --list
python main.py check -i source_import.json -o source_import_checked.json --cache-dir check_cache

# 已确认的新源才进入候选库；规则冲突必须单独批准
python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve-new "https://new.example"
python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve "https://changed.example"
```

- 新 URL 进入待校验，**不会**自动写入候选库。
- 同 URL 且规则相同只记录为重复；不会制造副本。
- 同 URL 但规则不同进入待审，**不会**自动覆盖候选库；只有 `--approve` 才能替换对应条目。
- 每个外部文件会保留到 `imports/raw/`，导入批次和处置结果记录在 `book_sources.sqlite3`。

### ③ 现有主源（candidates.json）

- `candidates.json` 是唯一候选主库；`out/` 下的 `checked.json`、`organized.json` 及 `archive/` 里的 `final_*.json` 都是处理或导出产物，不可反向当作主库覆盖。
- 主库保留可用、待验证和需代理复检源；待审和待校验源在获得人工确认前不进入主库。

---

## 三、合入后的处理链（每次新增/合入后必跑）

附件或自用源需要高优先级时，可一条命令生成三个产物：

```powershell
python main.py prepare -i "D:\DownloadsshareBookSource(1).json" -i candidates.json --prefer-input first
```

- `candidates-merged.json`：附件优先的去重合并底稿；
- `candidates-full.json`：完整候选版，分组为类型 + 状态；
- `candidates-fast.json`：仅保留当前标记为可用的快速使用版。

未联网复检时，状态只从旧分组迁移：明确带可用标记的归为“可用”，无法确认的全部归为“待验证”。

```powershell
# 1. 校验：仅 URL、规则指纹一致且缓存未过期的源可跳过联网
python main.py check -i candidates.json -o out/checked.json --cache-dir check_cache -c 50 -t 8

# 网络刚恢复，绕过旧缓存并用本次成功结果更新缓存
python main.py check -i candidates.json -o out/checked.json --refresh-cache --cache-dir check_cache

# 临时完全不使用缓存（不读也不写，适合一次性诊断）
python main.py check -i candidates.json -o out/checked.json --no-cache

# 2. 整理（按 类型+生命周期状态 重建分组，-r 恢复缓存里的星级数据）
python main.py organize -i out/checked.json -o out/organized.json -r check_cache

# 3. 报告（-r 恢复缓存星级，报告才有星级分布/优质TOP）
python main.py report -i out/organized.json -r check_cache -o out/final_report.md
```

> 等价的一条龙：`python main.py run -i candidates.json -o out/checked.json`
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
| 标签 | 含义 |
|---|---|
| ✅ | 可用（域名通 + 搜索有结果） |
| ❌ | 失效（连接失败 / 域名注销 / 搜索无结果） |
| 🔒 | 需验证（403/验证码/登录墙等反爬，需人工确认） |
| 🌐 | 需翻墙（连接被重置/TLS 握手失败，GFW 特征） |

### 第三层：星级（可用源才有，0~5★，阶梯规则，方案D 分档宽松）
星级是**逐级累加**的阶梯，每一级依赖上一级：

| 星级 | 达标条件 |
|---|---|
| 1★ | 可达：域名通或能访问（含需登录/验证、不可搜索） |
| 2★ | +搜索连通：有搜索规则且搜索请求有响应 |
| 3★ | +弱证据档：命中测试集作品（实测搜索成功）**或** 规则完整（目录+正文规则齐全） |
| 4★ | +目录完整：命中源实测目录章节数 ≥ 参考表阈值（小说 80% / 漫画 60%） |
| 5★ | +正文可用：命中源抽样章节能解析出正文（文本>100字符 或 图片≥3张） |

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
`📖小说★★★★★,规则完整,原创`
- **规则完整**：目录+正文规则都齐全（静态判定）
- **原创**：书源注释带 自写/自建/原创/整理 等标记
- 搜索命中结果**不再**以「命中《xx》」标签展示；命中信息仅用于星级评分与报告「命中作品」列

---

## 五、标准操作速查（日常三问）

| 场景 | 命令 |
|---|---|
| 新增一个站 | `echo '搜索URL' \| python main.py add - --interactive` |
| 导入外部源 | `python main.py import-sources --candidate candidates.json -i 外部.json --registry book_sources.sqlite3` |
| 查看待校验/待审 | `python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --list` |
| 批准校验通过的新源 | `python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve-new URL` |
| 批准规则冲突 | `python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve URL` |
| 清洗类型脏值（导入前必跑） | `python main.py sanitize -i 外部.json`（缺省就地覆盖） |
| 校验+整理+报告 | `python main.py run -i candidates.json -o out/checked.json` |
| 深度验证审计（目录+正文实测） | `python main.py check -i candidates.json --probe-depth 4 --cache-dir check_cache` |
| 仅可用源精简版 | `python main.py run -i candidates.json --keep-only-ok -o out/checked_ok.json` |
| 只要报告（不重新校验） | `python main.py report -i out/organized.json -r check_cache -o out/report.md` |
| 去重检查 | `python main.py dedupe -i 某文件.json -o 去重后.json` |
| 忘了命令 | `python main.py`（进菜单）或 `python main.py -h` |

---

## 六、维护建议

1. **新增源先进 `auto_added.json` 或外部批次**，通过 `import-sources` 进入待校验；不要再用 `merge --mode replace` 直接覆盖候选库。
2. **规则冲突必须审阅**：确认外部规则更可靠后才执行 `--approve`，批准后该源会在下次校验时因指纹变化强制复检。
3. **缓存会自动过期**：可用源 14 天后复检；待验证、需代理复检及其他状态 7 天后复检；旧版本缓存也会自动复检。
4. **被墙源**（🌐）可加 `--proxy` 复检：`python main.py check -i x.json --proxy http://127.0.0.1:7890`
   （原先这里写的 `socks5://` 是失实示例——urllib 与 aiohttp 都不认，会直接连接失败。
   代理只支持 `http://` / `https://`。）
   Web 端不必敲命令：**设置 → 校验 → 代理** 里配一次，或在校验前用工具栏的
   「校验参数」只对本次生效。注意 CLI 与 Web 的参数**各自独立**，不共享。
5. 候选主库与 `imports/raw/` 建议一同备份；台账 `book_sources.sqlite3` 保存了来源与审批历史。

---

## 七、目录约定（2026-09 治理后）

工作区根目录只保留：**代码文件 + 候选主库 + 台账 + 目录骨架**，所有处理产物不进根目录。

| 路径 | 用途 |
|---|---|
| `candidates.json` | **唯一候选主库**（当前 3734 源，已清洗）。日常唯一读写的书源文件。 |
| `auto_added.json` | `add` 命令的新增源暂存区（临时，导入批准后不长期留存）。 |
| `out/` | 所有 `-o` 输出产物（`organized.json`/`check_*.json`/`report.md` 等）统一放这里。 |
| `imports/raw/` | 外部导入的原始文件留存（`import-sources` 的 `--raw-dir`，含手机导出的原始 JSON）。 |
| `imports/` | 外部导入批次、台账 `book_sources.sqlite3` 的归属目录。 |
| `check_cache/` | **唯一**校验缓存目录（`--cache-dir`）。不要新建 `check_cache_full`/`_deep3` 变体。 |
| `.venv/` | uv 虚拟环境（`uv venv` + `uv pip install -r requirements.txt`）。用 `uv run python main.py ...` 运行。 |
| `archive/` | 历史产物归档（`productions/` 校验产物、`reports/` 报告、`caches/` 缓存、时间戳备份），**可回溯不删除**。 |
| `tests/` | 单元测试。 |

约定：
- `candidates.json` 之外的书源 JSON（外卖/手机导出/群分享）一律视为外部源，走 `import-sources` 安全导入，不进根目录。
- 不创建的产物：`checked.json`/`checked_full.json`/`checked_deep3.json`/`checked_ok.json`/`final_*.json`/根目录 `shareBookSource.json`——均由 `out/` 或 `archive/` 承载。
- 运行前若缺依赖：`uv pip install -r requirements.txt`（在当前 `.venv/` 下用 `uv run`）。

---

## 八、类型重判定与失效归因（2026-09 新增）

### `reclassify` —— 修正「漫画源被标成小说源」

`add` 命令的类型来自 `--type`（默认 novel），从未实测，所以错标很常见。本命令用
「静态信号（域名/名称/正文规则形态）+ 可选首页实测」反推真实类型。

```powershell
# 只看判定结果，不改文件
python main.py reclassify -i candidates.json --limit 50

# 回写 bookSourceType（之后重跑 organize 刷新分组）
python main.py reclassify -i candidates.json --write
```

判定信号（累计打分，>=3 且高于另一方才改判，否则保持原样）：

| 方向 | 信号 | 分值 |
|---|---|---:|
| 漫画 | 域名含 manhua/manga/comic/acg/dm5... | +3 |
| 漫画 | 名称/分组含 漫画/漫畫/汉化/图集/里番 | +3 |
| 漫画 | 有 `ruleContent.image` | +3 |
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

`diagnose` 对失效源重新探测，输出四类归因：

```powershell
# 只探测非可用源，输出 Markdown 报告
python main.py diagnose -i out/checked.json -o out/diagnose.md --only-dead -c 20
```

| 归因 | 含义 | 建议动作 |
|---|---|---|
| 死站 | 域名解析失败 / 连接失败 / 重置 / TLS | 两次明确失败后淘汰 |
| 规则漂移 | 域名可达（200），但搜索跑不出结果或列表解析为空 | **保留**，进 AI 修复队列 |
| 站点转型 | 实测类型与 `bookSourceType` 不符 | 改类型或按新类型重建规则 |
| 需验证 | 403/429/验证码/登录墙 | 保留，人工或改 UA/Cookie 复检 |
| 疑似可用 | 搜索能解析出列表 | 复检一次，可能是缓存过期或临时故障 |

### 规则回放器 `legado_rules.py`

`check`/`add`/`diagnose` 的规则解析统一走 `legado_rules.py`，语义对齐 Legado：

- `class.xxx` / `id.xxx` / `tag.a` 选择器简写
- `class.item@tag.a@href` 链式选择、`.-1` / `.0` 索引
- `##正则##替换`（支持 `$1` 反向引用）
- `@css:` / `@json:` / `@html:` 前缀；JSONPath 子集 `$.data.list[*].name`
- 取值动作 `text` / `textNodes` / `ownText` / `html` / 任意属性

**明确不支持的语法会返回原因（而不是静默返回空）**，调用方据此判为「无法验证」
而非「规则失效」，避免误杀：`@js:`、`<js>`、`@xpath:`、`||` 备选规则、JSONPath `..`。

> 注意：`beautifulsoup4` 是必需依赖（`pyproject.toml` 已加）。此前缺失导致所有规则
> 解析静默失败、深度验证（`--probe-depth` 3/4）实际未生效。


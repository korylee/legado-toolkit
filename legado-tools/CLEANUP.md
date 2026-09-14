# 清理失效书源 · 操作指南

> 适用场景：书源库积累一段时间后，大量失效源（域名注销/连接失败/搜索无结果）拖累导入与搜索体验。
> 本文档说明**如何安全地清理失效源**：先备份，再剔除，最后报告复核。
> 所有命令在 `legado-tools/` 目录下执行（PowerShell）。

---

## 一、为什么需要清理

| 问题 | 影响 |
|---|---|
| 失效源占用分组 | 分组被 `📖小说,❌失效` 大量占据，导入后搜索命中率下降 |
| 搜索请求浪费 | Legado 搜索时会逐个尝试可用源，失效源拖慢整体响应 |
| 维护成本 | 失效源与可用源混在一起，人工维护难以聚焦 |

**清理原则：宁可保留待观察，不可误删可用源。** 本工具链的失效判定基于联网实测（域名连接 + 搜索请求），不会误判。

---

## 二、清理前置：先校验，拿到健康度

清理依据是「健康度」（联网校验结果），必须先跑校验：

```powershell
# 全量校验（增量：check_cache 命中的源不重复联网，只有新源/无缓存的源联网）
python main.py check -i final_all.json -o checked.json --cache-dir check_cache
```

> 若要做**深度验证审计**（目录+正文实测）后清理，用：
> ```powershell
> python main.py check -i final_all.json -o checked.json --probe-depth 3 --cache-dir check_cache_deep3
> ```

校验后每组健康度含义：

| 健康度 | 含义 | 清理处置 |
|---|---|---|
| ✅ 可用 | 域名通 + 搜索有结果 | **保留** |
| ❌ 失效 | 连接失败 / 域名注销 / 搜索无结果 | **剔除**（本次清理对象） |
| 🔒 需验证 | 403/验证码/登录墙等反爬，需人工确认 | **保留**（可能只是临时反爬，见第五节复核） |
| 🌐 需翻墙 | 连接被重置 / TLS 握手失败（GFW 特征） | **保留**（加 `--proxy` 复检） |

---

## 三、三步清理法

### 第 1 步：备份（必做，可回退）

```powershell
Copy-Item final_all.json "archive\final_all_清理前_$(Get-Date -Format 'yyyyMMdd_HHmmss').json"
```

### 第 2 步：剔除失效源

```powershell
# 剔除 ❌失效 分组（Health.DEAD）；需验证/被墙源保留
python main.py organize -i final_all.json -o final_clean.json -r check_cache --drop-dead
```

命令完成后会打印 `已剔除失效源: N 个`，确认 N 与预期一致（见第四节当前数据）。

> `--drop-dead` 只剔 **❌失效**，不会动 🔒需验证 / 🌐需翻墙 / ✅可用。
> 若想**只保留可用源**（连需验证/被墙也一并去掉，产出精简版），用：
> ```powershell
> python main.py organize -i final_all.json -o checked_ok.json -r check_cache --keep-only-ok
> ```

### 第 3 步：报告复核（确认剔除结果符合预期）

```powershell
python main.py report -i final_clean.json -r check_cache -o report_clean.md
```

检查报告：
- 健康状态分布：❌失效 应为 0（全部剔除）
- 星级分布 / 优质 TOP：确认可用源没被误伤
- 问题清单：剩余需人工处理的源（见第五节）

### 导入 Legado

`final_clean.json` 即为可导入 Legado 的最终文件。

---

## 四、当前数据快照（2026-08-16，深度3 全量校验）

来源：`report_deep3.md`，文件 `final_all.json`（5436 源）

| 健康度 | 数量 | 占比 | 处置 |
|---|---:|---:|---|
| ✅ 可用 | 2299 | 42.3% | 保留 |
| ❌ 失效 | **1702** | 31.3% | **本次剔除对象** |
| 🔒 需验证 | 1379 | 25.4% | 保留 + 人工复核 |
| 🌐 需翻墙 | 56 | 1.0% | 保留 + `--proxy` 复检 |

**预计剔除后**：5436 → 3734 个（可用 + 需验证 + 需翻墙）。

---

## 五、剔除后的人工复核清单

剔除失效源后，剩下三类源值得人工过一遍（报告的问题清单会列明细）：

### 1. 🔒 需验证源（1379 个，反爬待确认）
- 报告「问题清单」会列出这些源；多为 403/验证码/登录墙
- 抽查：用浏览器打开源域名，若站点正常只是反爬，源本身可用 → 保留
- 若域名已注销/解析失败（误判为需验证的），手动从文件删除该条

### 2. 疑似目录不完整（8 个，章节数低于参考阈值）
- 典型：`超越漫画网`（海贼王 31 章 < 714 阈值）、`免费小说`（斗破苍穹 60 章 < 1344 阈值）
- 多为 chapterList 选择器只抓到部分章节（分页加载/选择器不全），**规则可修**——修复后重新校验大概率升星
- 若不修：源能用但目录不全，搜索命中后点进书籍可能看不到全部章节

### 3. 目录完整但正文验证失败（1 个）
- `口袋漫画`：无正文规则且图片不足 → 需补 ruleContent.content 或 image 规则

### 4. 🌐 需翻墙源（56 个）
- 通过代理复检：`python main.py check -i x.json --proxy socks5://127.0.0.1:1080`
- 复检仍失败的再从文件剔除

---

## 六、回退与维护建议

1. **备份留存**：主源文件保留最近两版（如 `final_all.json` + `archive/` 里的清理前快照），出问题可回退
2. **定期清理节奏**：建议每 1-2 轮全量校验后清一次，或失效占比超过 30% 时清理
3. **清理后重新合并**：新源/外部源合入时用 `final_clean.json` 作为主源，避免失效源复活
4. **深度验证审计**：清理前跑 `--probe-depth 3` 能拿到目录/正文实测数据，剔除决策更准确
5. **不要清完就删缓存**：`check_cache` 保留失效源的校验记录，下次全量校验增量秒过；删了缓存所有源需重新联网

---

## 速查

| 操作 | 命令 |
|---|---|
| 备份 | `Copy-Item final_all.json archive\final_all_备份.json` |
| 剔除失效源 | `python main.py organize -i final_all.json -o final_clean.json -r check_cache --drop-dead` |
| 只要可用源 | `python main.py organize -i final_all.json -o checked_ok.json -r check_cache --keep-only-ok` |
| 复核报告 | `python main.py report -i final_clean.json -r check_cache -o report_clean.md` |
| 被墙源复检 | `python main.py check -i x.json --proxy socks5://127.0.0.1:1080` |
| 一条龙（含清理） | `python main.py run -i final_all.json --drop-dead` |
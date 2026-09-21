# Legado 书源管理工具

Legado（阅读）书源管理工具链：CLI + FastAPI + SQLite + Vue 3。

用于对大量书源进行 **导入、校验、分组、诊断、AI 修复、导出/订阅**，并提供一套适合手机/桌面的 Web 管理台。

---

## 功能特性

- **候选主库**：SQLite 管理库 + JSON 交付格式；软删除 + 回收站；外部源安全导入（新 URL
  待校验、规则冲突待审）
- **批量校验**：走本机引擎（JVM 里跑「阅读」的真源码），分搜索 / 目录 / 正文段选深度；
  有效期内的源复用缓存（提示写明新校验几条、复用几条）；严格遵守源声明的 `concurrentRate`
- **分组与标签**：系统标签（类型 / 健康 / 规则完整度）自动重建，健康状态可人工锁定；
  用户标签独立存储，支持增删改、合并、规范化
- **调试与修复**：调试抽屉逐段摊开证据（本机引擎或连 App，第二次约 1 秒）；页面缓存
  5 分钟内不重复联网；失效归因分桶；AI 只提议、每条都过验证
- **导出与订阅**：导出快照（二维码 / 链接分享）+ 固定订阅 `/api/feed/ok.json`
- **Web 管理台**：列表 / 编辑 / 导入导出 / 任务 / 标签 / 回收站，桌面与移动端适配

---

## 技术栈

Python 3.10+ / FastAPI + Uvicorn / SQLite / aiohttp + BeautifulSoup4 + orjson；前端
Vue 3 + Vite + Element Plus + Pinia。依赖与版本以 `pyproject.toml`、`frontend/package.json` 为准。

---

## 项目结构

```text
backend/     FastAPI 后端（入口之一）：api/ 是路由，jobs/ 是任务运行器 + SSE
cli/         CLI 入口（入口之二）
core/        核心引擎——不依赖上面两个入口（对 services 只有两处惰性 import 的例外）
services/    应用编排层：串起多个 core 模块完成一件业务动作（加源 / 合并）
frontend/    Vue 3 管理台（构建产物 dist 由后端托管）
appservice/  App 侧：Robolectric 里跑「阅读」的真源码，校验与调试共用
scripts/     手工脚本（jvm_debug_run / jvm_debug_direct / jvm_login…）
tests/       Python 测试；tools/ 开发与 agent 工具（**不是运行时依赖**）
skills/      Agent / 开发技能文档；deploy/fnos/ 飞牛 NAS 部署示例
data/        运行时数据（已 gitignore）
```

改了前端要 `cd frontend && pnpm build`（后端启动会提示 dist 落后于源码）；改了文案跑
`python -m tools.check_copy --errors`（文案机检也挂在测试里）。

---

## 快速开始

### 1. 环境要求

- Python `>= 3.10`
- Node.js `>= 18` + pnpm（前端推荐）
- 可选：`uv`（推荐，项目含 `uv.lock`）

### 2. 启动后端

推荐使用 `uv`：

```powershell
uv sync
uv run python -m backend
```

默认监听：

- 后端：`http://127.0.0.1:8787`
- API 文档：`http://127.0.0.1:8787/docs`

也可以使用传统 venv：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install .
.\.venv\Scripts\python.exe -m backend
```

或直接启动 Uvicorn：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8787
```

开发热重载：

```powershell
uv run python -m backend --reload
```

热重载走 uvicorn 的 WatchFiles（OS 文件事件，空闲不耗 CPU），只监视代码目录
（`backend/ core/ services/ cli/ tools/`），不碰 `data/`、`.venv/`、`frontend/node_modules/`。
`watchfiles` 是正式依赖而非可选 extra：漏装时 uvicorn 会**静默**退回 StatReload 轮询
（代价与实测数字见 `backend/__main__.py` 的注释），启动时会打印一句警告。

注意：重载会**中断进行中的后台任务**（校验/修复），别在做全量校验时开着。

`--host` / `--port` 也可用命令行给，优先于环境变量：

```powershell
uv run python -m backend --host 127.0.0.1 --port 9000
```

### 3. 启动前端

```powershell
cd frontend
pnpm install
pnpm dev
```

访问：`http://127.0.0.1:5173`

Vite 会把 `/api` 代理到 `http://127.0.0.1:8787`。如需改后端地址：

```powershell
$env:VITE_BACKEND = "http://127.0.0.1:8787"
pnpm dev
```

构建静态文件：

```powershell
pnpm build
```

输出目录：`frontend/dist/`，**它由后端托管**：构建之后不必再开 vite，只启动后端，
访问 `http://127.0.0.1:8787/` 就是完整界面（手机换成局域网 IP + 同一端口）。
界面与 API 同源，所以既不需要 CORS，也不需要 rewrite 规则（路由用 hash 模式）。

`pnpm dev`（5173 + 代理）仍然用于开发，它带热更新；两者互不影响，可以同时开着。

> 改了前端源码却没重新 `pnpm build` 时，后端启动会提示「dist 落后于源码」——
> 不提示的话，「改动没生效」这条线索会被误当成 bug 去找。

### 4. JVM 校验（可选，在本机跑 App 真引擎）

不装也能用——它是**多一条证据**，不是前置条件。装好后，源列表会多一列 JVM 结论
（搜索 / 目录 / 正文三段），比本地调试更接近 App 的真实行为。

前置（缺一不可）：

- 「阅读」App 仓库的本地克隆（如 `D:\Documents\GitHub\legado-with-MD3`）
- JDK 21 + Android SDK
- Gradle 缓存——**必须与 App 仓库同盘**（跨盘会走拷贝，慢到不可用）

步骤：

1. 设置 → 「JVM 校验」页签 → 只填**一个**路径：App 源码目录。
   JDK / Android SDK / Gradle 用户目录**全部自动推导**，不用填。
2. 同一个页签里点「自检环境」：逐项全绿才能跑批。首次编译要十几分钟（在下载依赖），
   属正常现象。**这一页只配环境**（App 源码目录 + 自检 + 最近一次的结果）。
3. **跑批的入口在书源列表**：工具栏「全量校验」（或先勾选几条 → 「校验选中」）→
   「开始校验」。一次调用跑完即退（后端起 `appservice` 子进程）。
   没勾选 = 全部在用书源（受「条数上限」约束）；勾了 = 只跑这几条（那时条数上限不参与）。
4. **参数在弹框里填**（测试关键词 / 超时 / 并发 / 校验深度 / 条数上限）：它们是"这次怎么跑"，
   跟着动作走，**只作用于这一次、不写回设置**；默认值与取值范围仍由后端一份定义。

**本机引擎调试**（与跑批共用同一套 JVM 环境，但快得多）：调试抽屉里选「本机引擎」——
第一次约 10 秒（要把 JVM 起起来），**之后每次约 1 秒**。它会把那个 JVM 留在后台
（**空闲 30 分钟自动退出**，也可以随时 `python scripts/jvm_debug_direct.py --daemon-stop`）。
两个会「慢回 10 秒」的时刻：改了 `appservice/` 里的 Kotlin 之后（下一次走 Gradle 重新
编译一遍，不然跑的是旧类），以及空闲太久进程已经退出之后。**走不成常驻时会写出来**：
第一条分段下面会有一行「这次没能用常驻进程（…），已改用 Gradle（启动慢一些）」。

**批量校验走本机引擎**（「全量校验」/「校验选中」= 在 JVM 里跑「阅读」App 的真源码）；
**单条校验（列表行按钮）仍走本地引擎**——两条都会写 `checks`，`engine` 字段记着这一行的
结论是谁判的。结论落在两处：健康档位 / 星级 / 深度进 `checks` 表，逐段明细（命中的书、
章数、字数、渲染与登录态）进管理库 meta（`jvm_check:<批次>:<URL>`）。跑完一次跑批，
健康档位、星级、列表上的「验证」列都会跟着更新。

---

## CLI 使用

CLI 入口：

```powershell
python cli/main.py --help
```

无子命令时会进入交互菜单：

```powershell
python cli/main.py
```

`python cli/main.py --help` 看命令与选项；无子命令时进交互菜单。

### 按场景查命令

| 场景 | 命令 |
| :--- | :--- |
| 新增一个站 | `echo '搜索URL' \| python cli/main.py add - --interactive` |
| 导入外部源 | **Web 管理台「导入」对话框**（CLI 无此命令） |
| 清洗类型脏值（导入前必跑） | `python cli/main.py sanitize -i 外部.json`（缺省就地覆盖） |
| 校验 + 整理 + 报告（一条龙） | `python cli/main.py run -i candidates.json -o out/checked.json` |
| 深度验证审计（目录 + 正文实测） | `python cli/main.py check -i candidates.json --probe-depth 4` |
| 仅可用源精简版 | `python cli/main.py run -i candidates.json --keep-only-ok -o out/checked_ok.json` |
| 只要报告（不重新校验） | `python cli/main.py report -i out/organized.json -r -o out/report.md` |
| 去重检查 | `python cli/main.py dedupe -i 某文件.json -o 去重后.json` |

新增源时 URL 走 stdin，避免 shell 破坏百分号编码：

```powershell
@('https://example.com/search?q=%E7%BB%8D%E5%AE%8B') | python cli/main.py add - --name "示例站" --type novel --no-ask
```

**校验缓存在管理库**（`data/sources.sqlite3` 的 `checks` 表）：`--cache-dir` / `-r` 读的就是
它。要读旧的 NDJSON 缓存目录得加 `LEGADO_LEGACY_CACHE=1`（`check` / `run` 另有
`--legacy-cache` 开关，`organize` / `report` 没有）——所以示例里的 `-r` 指的是「从缓存恢复
星级」，**不是**读那个目录。

---

## Web 管理台

访问地址取决于怎么起：

- 开发（`pnpm dev`）：http://127.0.0.1:5173 ，带热更新
- 构建过前端（`pnpm build`）：只起后端即可，http://127.0.0.1:8787/

主要页面：

- 书源列表：搜索、筛选、排序、分页、批量打标签、批量校验、导出
- 编辑源：类型、健康状态（自动跟随/锁定）、标签、规则编辑、连 App 调试（含推送）、快速生成
- 分组管理：标签总览、重命名、合并、删除、规范化
- 导入/导出：外部导入、导出快照、固定订阅二维码
- 任务：校验、诊断、AI 修复等后台任务进度（统计条右侧的「任务」按钮）
- 统计条：健康度 chip（点一下即按该档筛选）；星级是可选排序与行提示里的明细
- 模型设置：OpenAI 兼容模型配置、测试、激活

### 系统标签说明

系统标签由程序维护：

- 类型：📖小说、🎧听书、🎨漫画、📥下载（对应 bookSourceType 0/1/2/3）
- 健康状态（五档）：✅可用、🔒需登录、🌐需翻墙、❓待验证、❌已失效
  （**枚举只有一份**：显示名在 `core/models.py` 的 `HEALTH_NAMES`，写进分组/判定的是
  `core/tags.py` 的 `SYSTEM_STATUS_TAG_ORDER`（无 emoji）；前端从
  `GET /api/sources/tags/meta` 拿）
- 规则质量：规则完整

五档按**下一步动作**划分——**两档该不该合成一档，看动作是否相同**（不是语义相近、
更不是名字像不像）：

| 标签 | 含义 | 下一步动作 |
| :--- | :--- | :--- |
| ✅可用 | 域名通 + 搜索有结果 | 直接用 |
| 🔒需登录 | 403 / 验证码 / 登录墙等反爬，站点明确拒绝 | 连 App 试 |
| 🌐需翻墙 | 连接被重置 / TLS 握手失败（GFW 特征） | 挂代理复测 |
| ❓待验证 | **我们没结论**：超时、校验异常、从未校验 | 跑或重跑一次校验 |
| ❌已失效 | 域名注销 / 服务端 5xx / 入口 4xx | 删 |

「证书问题」这一档 2026-09-20 撤了（理由与存量迁移见 lessons §七十二）；「未校验」不是状态，
是**没有 `checks` 行**（统计条上是灰 chip、筛选项里可选，不进健康枚举）。失败**原因**
（超时 / 异常 / DNS…）不丢，落在校验记录的 `error` 与详情里。

其中：

- 类型由 bookSourceType 决定，编辑类型后保存会自动重建类型标签
- 健康状态默认自动跟随校验结果；可锁定，锁定后校验任务不再覆盖
- 关掉锁定即交回校验结果，按最近一次校验重建
- 用户标签独立存储，永远不会被系统标签重建覆盖
- 用户标签按别名表归一（如「精品排版」→「精排」），编辑时即时提示

---

## 核心 API

**完整清单与字段看 `/docs`**（FastAPI 自带，随代码走）。这里只留几条「为什么长这样」的：

| 接口 | 要点 |
| :--- | :--- |
| GET /api/sources/urls | 当前筛选下的全部 URL（不分页、不含回收站），供「选中全部 N 条筛选结果」 |
| POST /api/sources/delete | urls 走 body：全库几千条拼进查询串会超请求行上限 |
| POST /api/sources/restore | 按**行 id** 恢复（同 URL 可有多份历史版本，按 URL 会含糊） |
| POST /api/sources/purge | 清空回收站（**唯一**的硬删除路径），先落快照再删 |
| POST /api/import | `conflict_strategy`：`keep`（默认，冲突进待审）/ `overwrite`（覆盖） |
| POST /api/rules/jvm-debug | **本机引擎调试**（JVM 里跑 App 真源码，含 JS 规则），与 `/api/rules/app-debug` 同形 |
| POST /api/rules/app-push | 推送会**改 App 数据**，所以必须显式触发（由预检三态决定要不要先推） |
| POST /api/jvm/run | 跑批建一条任务（`kind=jvm_run`，关页面不丢）；范围 `urls` / `filter`，本次参数走 `params` |
| POST /api/rules/suggest-rule | AI 只提议：每条都过验证，验不了的单独标「只能连 App 试」 |
| GET /api/settings | 全局设置，同时下发 `defaults` 与 `limits`（前端不硬编码上下界） |

任务进度走 `GET /api/jobs/{id}/events`（SSE）；导出与订阅按 `/docs` 用。

---

## 数据与存储

运行时数据默认放在 data/。`.gitignore` **按子目录白名单**忽略（archive/backups/
cache/check_cache/imports/out/config/app_probe 等）——**新增子目录要自己补一条**，
别以为整个 data/ 都被忽略了。

**事实来源是 `data/sources.sqlite3`（管理库）：给 Legado 的 JSON 是交付格式，不是事实来源**；
`data/candidates*.json` 请单独备份；`data/config/`（全局设置 + LLM 配置）含 API Key，**勿提交**。

| 路径 | 用途 |
| :--- | :--- |
| `data/sources.sqlite3` | 管理库：源 / 校验缓存 / 任务 / 导出记录 |
| `data/candidates*.json` | 候选源与导出产物（交付格式，请单独备份） |
| `data/imports/conflicts/` | Web 导入时规则冲突的源（原样留存，可回查） |
| `data/backups/` | 软删除记录（`deleted.jsonl`）+ 手动备份 |
| `data/out/` | Web / 后端产出的临时快照与导出记录 |
| `data/archive/` | 历史产物归档（可回溯，不删） |
| `data/app_probe/` | App / JVM 实测产物：调试 NDJSON 与侧车、跑批导出、常驻日志 |
| `data/check_cache/` | **遗留** NDJSON 校验缓存：现役缓存在管理库 `checks` 表，只在 `--legacy-cache` 与迁移对拍时读。**别再建 `check_cache_full` / `_deep3` 这类变体** |

**两条操作纪律**：① 新增源走 `add`（CLI）或 Web「新增」，外部源走 Web「导入」——
**别用 `merge --mode replace` 直接覆盖管理库**；② 规则冲突要人工判断：先用 `check` 看外部
那版的实测结果，确实更好再用导入对话框的 `overwrite`（覆盖后特征变化会强制复查）。

CLI 的文件式产物默认落在**当前目录**（`checked.json` 等），项目约定统一 `-o out/...`；
别在仓库根堆产物。

可通过环境变量覆盖数据目录：

```powershell
$env:LEGADO_DATA_DIR = "D:\legado-data"
```

---

## 配置项（部署常用）

| 环境变量 | 说明 |
| :--- | :--- |
| `LEGADO_HOST` / `LEGADO_PORT` | 监听地址与端口（默认 `0.0.0.0` / `8787`；`--host` / `--port` 优先） |
| `LEGADO_RELOAD` | `1/true/yes/on` 开热重载（`--reload` / `--no-reload` 优先） |
| `LEGADO_DATA_DIR` | 运行时数据目录（默认 `data/`） |
| `LEGADO_SETTINGS` / `LEGADO_LLM_CONFIG` | 全局设置与模型配置路径（测试靠前者隔离） |
| `LEGADO_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | 没配模型 profile 时的兜底（界面里配更常用） |
| `VITE_BACKEND` | Vite 开发代理目标（默认 `http://127.0.0.1:8787`） |

---

## 测试

运行完整测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

常见核心测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_tags tests.test_store_tags tests.test_organizer -v
```

如果在受限 Windows 环境遇到 .tmp/ 权限问题，可先处理目录权限，或仅运行核心业务测试。

---

## 部署建议

### 开发环境

`uv run python -m backend` + `cd frontend && pnpm dev`（vite 把 `/api` 代理到后端）
——完整步骤见上文「快速开始」。

### 生产 / 局域网部署

与开发唯一的区别是**前端不由 vite 提供**：`pnpm build` 出 `frontend/dist`，只启动后端
（由后端托管），手机用局域网 IP + 同一端口访问。要放到别的机器/端口时再做同源反向代理：

```text
/        -> frontend/dist 静态文件
/api/    -> http://127.0.0.1:8787
```

注意：deploy/fnos/ 是阅读服务器 + 小说下载器的 NAS 部署示例，不是本管理台的前端部署方案。

---



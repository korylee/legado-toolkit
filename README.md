# Legado 书源管理工具

Legado（阅读）书源管理工具链：CLI + FastAPI + SQLite + Vue 3。

用于对大量书源进行 **导入、校验、分组、诊断、AI 修复、导出/订阅**，并提供一套适合手机/桌面的 Web 管理台。

- 当前版本：`0.2.0`
- Python：`>= 3.10`
- 后端：FastAPI + SQLite
- 前端：Vue 3 + Vite + Element Plus

---

## 功能特性

- **候选主库管理**
  - 以 SQLite 作为管理库，JSON 作为给 Legado 的交付格式
  - 软删除 + 回收站，UI 不直接硬删源
  - 支持外部书源安全导入：新 URL 待校验、规则冲突待审

- **高并发校验**
  - 异步批量校验书源可用性、搜索命中、目录/正文完整度
  - 支持代理复检、SSL 跳过、自定义测试集
  - 校验缓存可复用：有效期内的源不回源复检；全量校验按钮的下拉里可「忽略缓存，
    全部重校」。完成提示会写明本次新校验几条、复用缓存几条——不说的话，
    「点校验 → 完成」和「一条请求都没发」长得一模一样
  - 严格遵守书源自己声明的 `concurrentRate` 限速；校验 / 试跑 / 连 App 调试 /
    快速新增源四条抓取链路都遵守（声明了限速的源会慢一些）

- **分组与标签**
  - 系统标签：类型、健康状态、规则完整度，自动重建
  - 用户标签：独立存储，支持增删、重命名、合并、规范化
  - 系统健康状态支持人工锁定，避免被后续校验覆盖

- **诊断与 AI 修复**
  - 失效归因：死站 / 规则漂移 / 站点转型 / 需验证
  - AI 只负责提议规则，必须通过规则回放器验证
  - 调试抽屉里改不动某一步时，**先用 App 实测到的值在候选里挑一遍**（免费，多数情况
    一次就对上了）；挑不出来才让 AI 按这一步的 DOM 大纲提规则。AI 提的每条都由回放器
    验过才显示「验过几条」，`@js:` 这类本地跑不了的会标明**只能连 App 试**
  - 支持 OpenAI 兼容接口及多种本地/云端模型配置
  - **调试的页面缓存**：补抓的页面 5 分钟内不重复联网（单页 0.8 秒 → 毫秒级），
    调试卡片上可选「用缓存 / 只读缓存 / 忽略缓存重抓」，抽屉里每页都标着抓取时刻

- **导出与订阅**
  - 临时导出快照：适合生成二维码/链接分享
  - 固定订阅：`/api/feed/ok.json`、`/api/feed/all.json`
  - Web/API 支持按筛选结果导出

- **Web 管理台**
  - 桌面 + 移动端适配
  - 书源列表、编辑、导入、导出、任务中心、诊断看板
  - 连 App 调试、快速新增源、标签管理、回收站

---

## 技术栈

| 层 | 技术 |
| :--- | :--- |
| CLI / 业务核心 | Python 3.10+ |
| HTTP 服务 | FastAPI + Uvicorn |
| 数据库 | SQLite |
| 抓取/解析 | aiohttp、BeautifulSoup4、orjson |
| 前端 | Vue 3、Vite、Element Plus、Pinia |
| 规则验证 | 自研 Legado 规则回放器 |
| AI 修复 | OpenAI 兼容 `/chat/completions` |

---

## 项目结构

```text
.
├─ backend/                FastAPI 后端（**入口之一**）
│  ├─ api/                 路由：sources/jobs/export/feed/rules/llm
│  ├─ jobs/                后台任务运行器 + SSE
│  ├─ app.py               FastAPI 应用
│  └─ __main__.py          后端启动入口
├─ cli/main.py             CLI 入口（**入口之二**）
├─ core/                   核心引擎——**不依赖上面两个入口，也不依赖 services**
│  ├─ rules/               Legado 规则回放器
│  ├─ repair/              AI 修复循环
│  ├─ store.py             SQLite 管理库
│  ├─ checker.py           校验器
│  ├─ organizer.py         分组整理
│  ├─ tags.py              标签规范化 / 系统标签
│  └─ ...
├─ services/               **应用编排层**：串起多个 core 模块完成一件业务动作
│  └─ add_source.py        快速新增源——CLI 与 backend **共用**，所以不放在任一个入口里
├─ frontend/               Vue 3 管理台
├─ tests/                  Python 测试
├─ tools/                  **开发与 agent 工具，不是运行时依赖**
│  ├─ apply_edits.py       行级补丁应用器（绕开 shell 转义），见 skills/agent-write-safety
│  └─ probe_app_debug.py   手工探测 App 调试 WS 推了什么
├─ skills/                 Agent / 开发技能文档
├─ deploy/fnos/            飞牛 NAS 书库部署示例
├─ data/                   运行时数据（已 gitignore）
├─ README.md               本文件：功能、用法、API
├─ AGENTS.md               硬性约定（**改代码前必读**）
├─ WORKFLOW.md             书源「新增/导入 → 校验 → 整理 → 报告」完整链路
├─ TODO.md                 待办 / 需先调研
└─ pyproject.toml
```

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

热重载走 uvicorn 的 WatchFiles（基于 OS 文件事件，空闲不耗 CPU），且只监视代码目录
（`backend/ core/ services/ cli/ tools/`），不碰 `data/`、`.venv/`、
`frontend/node_modules/`——不限范围的话这一千多个目录都要被注册监视。

`watchfiles` 列在正式依赖而不是可选 extra，是因为漏装时 uvicorn 会**静默**退回
StatReload 轮询：每 0.25 秒把每个监视目录递归扫一遍 `*.py` 并逐个 stat，实测持续占
约 4% 单核。启动时会打印警告提示这种情况。

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

### 常用命令速查

| 命令 | 用途 |
| :--- | :--- |
| `check` | 高并发联网校验书源可用性/星级 |
| `organize` | 按类型 + 健康状态重建分组 |
| `report` | 生成 Markdown 诊断报告 |
| `run` | 一条龙：校验 → 整理 → 报告 |
| `merge` | 合并/更新多份书源 JSON |
| `prepare` | 附件优先合并，生成完整候选版/快速使用版 |
| `dedupe` | 按 URL/名称去重（写出新文件） |
| `dups` | 找重复源：规则相同、只有地址/署名不同的（**只读清单**，缺省读管理库） |
| `sanitize` | 清洗字段类型，兼容 Legado/Gson 导入 |
| `add` | 给搜索 URL 自动推断规则生成书源 |
| `import-sources` | 安全导入外部书源，生成待校验/待审记录 |
| `review-imports` | 查看或批准外部书源规则冲突 |
| `reclassify` | 按实测信号重判书源类型 |
| `diagnose` | 失效源归因 |
| `repair` | AI 修复规则：证据 → 提议 → 回放验证 → 重试 |

### 示例

```powershell
# 合并两个源库
python cli/main.py merge -i a.json -i b.json -o merged.json --mode replace

# 新增源：URL 走 stdin，避免 shell 破坏百分号编码
@('https://example.com/search?q=%E7%BB%8D%E5%AE%8B') | python cli/main.py add - --name "示例站" --type novel --no-ask
```

校验、整理、导入、审批、报告这些日常操作不在这里重复列举——按场景查命令比逐个记参数好用，
见 [`WORKFLOW.md`](WORKFLOW.md) 的「标准操作速查」。

---

## Web 管理台

访问地址取决于怎么起：

- 开发（`pnpm dev`）：http://127.0.0.1:5173 ，带热更新
- 构建过前端（`pnpm build`）：只起后端即可，http://127.0.0.1:8787/

主要页面：

- 书源列表：搜索、筛选、排序、分页、批量打标签、批量校验、导出
- 编辑源：类型、健康状态（自动跟随/锁定）、标签、规则编辑、连 App 调试（含推送）、快速生成
- 分组/标签管理：标签总览、重命名、合并、删除、规范化
- 导入/导出：外部导入、导出快照、固定订阅二维码
- 任务中心：校验、诊断、AI 修复等后台任务进度
- 诊断看板：健康度、星级、失效归因统计
- 模型设置：OpenAI 兼容模型配置、测试、激活

### 系统标签说明

系统标签由程序维护：

- 类型：📖小说、🎧听书、🎨漫画、📥下载（对应 bookSourceType 0/1/2/3）
- 健康状态：✅可用、❌失效、🔒需验证、🌐需翻墙、🔍不可搜、⏱超时、⚠️异常、🔐证书、⏭跳过
  （**权威清单是 `core/models.py` 的 `HEALTH_NAMES`**，这里只是给人看的枚举）
- 规则质量：规则完整

其中：

- 类型由 bookSourceType 决定，编辑类型后保存会自动重建类型标签
- 健康状态默认自动跟随校验结果；可锁定，锁定后校验任务不再覆盖
- 关掉锁定即交回校验结果，按最近一次校验重建
- 用户标签独立存储，永远不会被系统标签重建覆盖
- 用户标签按别名表归一（如「精品排版」→「精排」），编辑时即时提示

---

## 核心 API

| 接口 | 说明 |
| :--- | :--- |
| GET /api/health | 服务统计 |
| GET /api/net | 本机可用 IP/端口，供手机访问 |
| GET /api/sources | 书源列表、筛选、分页 |
| GET /api/sources/urls | 当前筛选下的全部 URL（不分页、不含回收站），供「选中全部 N 条筛选结果」 |
| POST /api/sources/delete | 批量软删除。urls 走 body：全库 3850 条拼进查询串约 139KB，会超请求行上限 |
| GET /api/sources/detail | 书源详情 |
| POST /api/sources/save | 保存书源与标签 |
| POST /api/import | 安全导入外部书源（新 URL 待校验 / 冲突留存 / 重复跳过） |
| GET /api/sources/tags | 系统/用户标签总览 |
| GET /api/sources/tags/meta | 系统标签枚举（类型/状态/质量）与用户标签别名表，前端不硬编码 |
| POST /api/sources/tags | 批量加/去用户标签 |
| POST /api/sources/tags/rename | 重命名用户标签 |
| POST /api/sources/tags/merge | 合并用户标签 |
| POST /api/sources/tags/delete | 删除用户标签 |
| POST /api/sources/tags/normalize | 全库规范化用户标签 |
| POST /api/jobs | 提交后台任务 |
| GET /api/jobs/{id}/events | SSE 任务进度 |
| POST /api/export | 创建导出快照 |
| GET /api/export/{uid}.json | 获取导出 JSON |
| GET /api/feed/ok.json | 固定订阅：仅可用源 |
| GET /api/feed/all.json | 固定订阅：全部启用源 |
| POST /api/rules/chain | 规则离线回放（本地粗略验证，跑不了 JS 规则） |
| POST /api/rules/app-debug | 连 App 调试：借 App 的调试 WS 跑完整链路，含 JS 规则。`cache` 选 auto / only / refresh（只影响我们补抓的页面） |
| POST /api/rules/app-preflight | 调试前预检：连不上 / App 里没有这个源 / 可以调试 |
| POST /api/rules/app-push | 把源推送到 App（幂等，会改动 App 数据，需显式触发） |
| POST /api/rules/replay-step | 用已抓到的 HTML 重放一步规则（不联网） |
| POST /api/rules/suggest-rule | 让 AI 给某一步提候选规则（只提议；每条都过本地回放器，验不了的单独标「只能连 App 试」） |
| GET /api/llm/profiles | LLM 模型配置 |
| GET /api/settings | 全局设置（校验参数的默认值），同时下发 defaults 与 limits |
| PATCH /api/settings | 修改全局设置（只改传了的键，未传的保持原值） |
| POST /api/settings/reset | 恢复默认设置 |
| GET /docs | Swagger API 文档 |

### 校验参数的取值优先级

并发数、超时、探测深度、校验 SSL、代理这 5 项，按三层取值：

    本次覆盖（书源页工具栏「校验参数」按钮） > 全局设置（设置 → 校验） > 内置默认

- **本次覆盖只作用于提交的那一次校验，不写回全局设置**。与全局相同的项不会进覆盖。
- 覆盖对**所有**校验入口生效（全量 / 校验选中 / 按行校验），生效时按钮上有计数徽标。
- 「忽略缓存，全部重校」不是设置项——它是每次动作，仍在全量校验的下拉里。
- 代理**只支持 `http://` 与 `https://`**：填了就是所有校验请求都走它（直连能通的源也会绕一圈）。
  `socks5` 需要额外依赖 `aiohttp_socks`，本项目未安装，接口会直接返回 400 而不是静默降级。
- CLI 的 `check` 命令**不读**全局设置，仍用自己的命令行参数（`--concurrency` 等），
  两边默认值各自独立、没有同步关系。

---

## 数据与存储

运行时数据默认放在 data/，已加入 .gitignore。

| 路径 | 内容 |
| :--- | :--- |
| data/sources.sqlite3 | 管理库：源、校验缓存、诊断、修复、任务 |
| data/check_cache/ | 书源校验缓存 |
| data/out/exports/ | 临时导出快照 |
| data/backups/ | 软删除记录（`deleted.jsonl`，一行一次删除操作，含原因）/ 手动备份 |
| data/imports/raw/ | 外部源原始文件 |
| data/config/llm_profiles.json | LLM 模型配置（含 API Key，勿提交） |
| data/config/settings.json | 全局设置：校验参数默认值（并发/超时/探测深度/代理等） |
| data/candidates*.json | 候选源/导出产物 |

注意：candidates.json 通常是唯一候选主库，请单独备份。
给 Legado 的 JSON 是交付格式，SQLite 才是管理事实来源。

可通过环境变量覆盖数据目录：

```powershell
$env:LEGADO_DATA_DIR = "D:\legado-data"
```

---

## 配置项

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| LEGADO_HOST | 0.0.0.0 | 后端监听地址（`--host` 优先）|
| LEGADO_PORT | 8787 | 后端端口（`--port` 优先）|
| LEGADO_RELOAD | 空 | 1/true/yes/on 开启热重载（`--reload` / `--no-reload` 优先）|
| LEGADO_DATA_DIR | data/ | 运行时数据目录 |
| LEGADO_LLM_CONFIG | data/config/llm_profiles.json | LLM 配置路径 |
| LEGADO_SETTINGS | data/config/settings.json | 全局设置路径（测试靠它隔离） |
| LEGADO_LLM_API_KEY | 空 | LLM API Key（未配置 profile 时使用） |
| LEGADO_LLM_BASE_URL | https://api.openai.com/v1 | LLM Base URL |
| LEGADO_LLM_MODEL | gpt-4o-mini | LLM 模型名 |
| VITE_BACKEND | http://127.0.0.1:8787 | Vite 开发代理目标 |

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

- 后端：uv run python -m backend
- 前端：cd frontend && pnpm dev
- Vite 代理 /api 到后端

### 生产 / 局域网部署

1. 构建前端：

```powershell
cd frontend
pnpm build
```

2. 启动后端：

```powershell
.\.venv\Scripts\python.exe -m backend
```

3. 使用 Nginx/Caddy 做同源反向代理：

```text
/        -> frontend/dist 静态文件
/api/    -> http://127.0.0.1:8787
```

注意：deploy/fnos/ 是阅读服务器 + 小说下载器的 NAS 部署示例，不是本管理台的前端部署方案。

---

## 开发约定

见 [`AGENTS.md`](AGENTS.md)——约定清单只在那里维护；每条背后的「症状 → 根因 → 结论」
见 `skills/legado-source-lessons`。

**这里不复述任何一条**：复述出来的那份会和 AGENTS 分叉，而两份分叉的规矩比没有规矩更糟
（这条经验本身写在 `AGENTS.md` 硬性约定 #9）。

---

## 相关文档

- WORKFLOW.md：书源新增/导入 → 校验 → 整理 → 报告完整链路
- AGENTS.md：Agent/开发约定
- TODO.md：明确记录但未实施的待办与二期候选
- skills/：书源规则、工具链、安全写入技能
- deploy/fnos/README.md：飞牛 NAS 书库部署


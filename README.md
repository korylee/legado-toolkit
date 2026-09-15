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
  - 校验缓存可复用、可刷新、可禁用

- **分组与标签**
  - 系统标签：类型、健康状态、规则完整度，自动重建
  - 用户标签：独立存储，支持增删、重命名、合并、规范化
  - 系统健康状态支持人工锁定，避免被后续校验覆盖

- **诊断与 AI 修复**
  - 失效归因：死站 / 规则漂移 / 站点转型 / 需验证
  - AI 只负责提议规则，必须通过规则回放器验证
  - 支持 OpenAI 兼容接口及多种本地/云端模型配置

- **导出与订阅**
  - 临时导出快照：适合生成二维码/链接分享
  - 固定订阅：`/api/feed/ok.json`、`/api/feed/all.json`
  - Web/API 支持按筛选结果导出

- **Web 管理台**
  - 桌面 + 移动端适配
  - 书源列表、编辑、导入、导出、任务中心、诊断看板
  - 规则试跑、快速新增源、标签管理、回收站

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
├─ backend/                FastAPI 后端
│  ├─ api/                 路由：sources/jobs/export/feed/rules/llm
│  ├─ jobs/                后台任务运行器 + SSE
│  ├─ app.py               FastAPI 应用
│  └─ __main__.py          后端启动入口
├─ core/                   核心业务
│  ├─ rules/               Legado 规则回放器
│  ├─ repair/              AI 修复循环
│  ├─ store.py             SQLite 管理库
│  ├─ checker.py           校验器
│  ├─ organizer.py         分组整理
│  ├─ tags.py              标签规范化 / 系统标签
│  └─ ...
├─ cli/main.py             CLI 入口
├─ services/add_source.py  快速新增源
├─ frontend/               Vue 3 管理台
├─ tests/                  Python 测试
├─ docs/                   设计/计划文档
├─ skills/                 Agent / 开发技能文档
├─ deploy/fnos/            飞牛 NAS 书库部署示例
├─ data/                   运行时数据（已 gitignore）
├─ AGENTS.md               开发约定
├─ WORKFLOW.md             完整工作流
├─ pyproject.toml
└─ requirements.txt
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
pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend
```

或直接启动 Uvicorn：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8787
```

开发热重载：

```powershell
$env:LEGADO_RELOAD = "1"
uv run python -m backend
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

输出目录：`frontend/dist/`

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
| `dedupe` | 按 URL/名称去重 |
| `sanitize` | 清洗字段类型，兼容 Legado/Gson 导入 |
| `add` | 给搜索 URL 自动推断规则生成书源 |
| `import-sources` | 安全导入外部书源，生成待校验/待审记录 |
| `review-imports` | 查看或批准外部书源规则冲突 |
| `reclassify` | 按实测信号重判书源类型 |
| `diagnose` | 失效源归因 |
| `repair` | AI 修复规则：证据 → 提议 → 回放验证 → 重试 |

### 示例

```powershell
# 联网校验
python cli/main.py check -i candidates.json -o data/out/checked.json -c 50 -t 8

# 整理分组
python cli/main.py organize -i data/out/checked.json -r data/check_cache -o data/out/organized.json

# 一条龙
python cli/main.py run -i candidates.json -o data/out/checked.json -c 50

# 合并两个源库
python cli/main.py merge -i a.json -i b.json -o merged.json --mode replace

# 清洗后导入 Legado
python cli/main.py sanitize -i merged.json -o final.json

# 新增源：URL 走 stdin，避免 shell 破坏百分号编码
@('https://example.com/search?q=%E7%BB%8D%E5%AE%8B') | python cli/main.py add - --name "示例站" --type novel --no-ask

# 安全导入外部源
python cli/main.py import-sources --candidate candidates.json -i source_import.json --registry book_sources.sqlite3 --raw-dir data/imports/raw

# 查看待审冲突 / 批准新源
python cli/main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --list
python cli/main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve-new "https://new.example"
```

完整工作流见：[`WORKFLOW.md`](WORKFLOW.md)

---

## Web 管理台

启动前后端后访问 http://127.0.0.1:5173 。

主要页面：

- 书源列表：搜索、筛选、排序、分页、批量打标签、批量校验、导出
- 编辑源：类型、系统标签、用户标签、规则编辑、全链路试跑、快速生成
- 分组/标签管理：标签总览、重命名、合并、删除、规范化
- 导入/导出：外部导入、导出快照、固定订阅二维码
- 任务中心：校验、诊断、AI 修复等后台任务进度
- 诊断看板：健康度、星级、失效归因统计
- 模型设置：OpenAI 兼容模型配置、测试、激活

### 系统标签说明

系统标签由程序维护：

- 类型：📖小说、🎧听书、🎨漫画、📥下载（对应 bookSourceType 0/1/2/3）
- 健康状态：可用、待验证、已失效、需代理复检
- 规则质量：规则完整

其中：

- 类型由 bookSourceType 决定，编辑类型后保存会自动重建类型标签
- 健康状态可人工选择并锁定，校验任务不会覆盖
- 选择自动后，会解除锁定并按最近校验结果重建
- 用户标签独立存储，永远不会被系统标签重建覆盖

---

## 核心 API

| 接口 | 说明 |
| :--- | :--- |
| GET /api/health | 服务统计 |
| GET /api/net | 本机可用 IP/端口，供手机访问 |
| GET /api/sources | 书源列表、筛选、分页 |
| GET /api/sources/detail | 书源详情 |
| POST /api/sources/save | 保存书源与标签 |
| GET /api/sources/tags | 系统/用户标签总览 |
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
| POST /api/rules/chain | 规则全链路试跑 |
| GET /api/llm/profiles | LLM 模型配置 |
| GET /docs | Swagger API 文档 |

---

## 数据与存储

运行时数据默认放在 data/，已加入 .gitignore。

| 路径 | 内容 |
| :--- | :--- |
| data/sources.sqlite3 | 管理库：源、校验缓存、诊断、修复、任务 |
| data/check_cache/ | 书源校验缓存 |
| data/out/exports/ | 临时导出快照 |
| data/backups/ | 软删除快照 / 手动备份 |
| data/imports/raw/ | 外部源原始文件 |
| data/config/llm_profiles.json | LLM 模型配置（含 API Key，勿提交） |
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
| LEGADO_HOST | 0.0.0.0 | 后端监听地址 |
| LEGADO_PORT | 8787 | 后端端口 |
| LEGADO_RELOAD | 空 | 1 开启 Uvicorn 热重载 |
| LEGADO_DATA_DIR | data/ | 运行时数据目录 |
| LEGADO_LLM_CONFIG | data/config/llm_profiles.json | LLM 配置路径 |
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

- SQLite 是管理库，JSON 是交付格式，不要反过来把 JSON 当唯一事实来源。
- 规则验证必须由规则回放器完成，AI 只负责提议。
- 不支持的规则语法要显式返回原因，不能静默返回空。
- URL 一律先规范化再作为 key，避免缓存/校验关联不上。
- 前端改弹窗/抽屉的样式要写全局 `frontend/src/styles.css`——el-dialog 是 teleport 到 body 的，组件内的 scoped 样式够不到它内部。
- 运行时数据全部放在 data/，不要提交数据库、缓存、导出和 API Key。
- 改代码后建议运行：

```powershell
.\.venv\Scripts\python.exe -c "import backend.app, core.store"
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

更多约定见 AGENTS.md。

---

## 相关文档

- WORKFLOW.md：书源新增/导入 → 校验 → 整理 → 报告完整链路
- CLEANUP.md：清理与迁移说明
- AGENTS.md：Agent/开发约定
- docs/：设计文档与实施计划
- skills/：书源规则、工具链、安全写入技能
- deploy/fnos/README.md：飞牛 NAS 书库部署


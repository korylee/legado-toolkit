# 用户标签一等公民改造计划

> 状态：已实施（2026-09-14）
> 目标：`bookSourceGroup = 系统标签 + 用户标签`
> 范围：`core/store.py`、`core/organizer.py`、后端 API、前端分组编辑 / 批量操作 / 分组管理抽屉

## 一、目标

1. 用户标签成为独立数据，可查询、可增删、可重命名、可合并、可规范化。
2. 系统标签由类型、健康状态、规则完整度自动重建，organize 不得覆盖用户标签。
3. 导出给 Legado 时，动态合并 `系统标签 + 用户标签`。
4. 批量操作从「覆盖整串分组」改为「加标签 / 去标签」。
5. 分组编辑框改为 `el-select multiple filterable allow-create`。
6. 新增「分组管理」抽屉：标签总览、重命名、合并、删除、规范化。

## 二、非目标

- 不引入新的第三方依赖。
- 不改变给 Legado 的 JSON 数组格式。
- 不在书源 JSON 里新增自定义字段。
- 不做标签层级、标签分组、标签颜色。
- 不做自动淘汰用户标签。
- 本轮不做公网权限系统。

## 三、关键设计决策

1. `sources.group_name` 只保存系统标签。
2. `sources.user_tags` 保存用户标签，逗号分隔，保持顺序，trim + 去重。
3. `raw_json` 继续保存导入原文；导出/详情时由代码动态合并 `bookSourceGroup`。
4. 系统标签集合先固定为：
   - 类型：`📖小说`、`🎧听书`、`🎨漫画`、`🎬视频`
   - 状态：`可用`、`待验证`、`已失效`、`需代理复检`
   - 规则：`规则完整`
5. `原创`、`精排`、`R18`、`正版`、`仅发现`、`自用` 以及用户自定义的全部作为用户标签。
6. `organizer.PRESERVED_TAG_RULES` 白名单逻辑删除，改为「非系统标签全部保留」。
7. `fingerprint()` 不包含 group 字段，因此编辑标签不会使校验缓存失效。
8. 废弃 `PATCH /api/sources/group` 的整串覆盖语义，新增标签加减 API。
9. 单源编辑和批量编辑都使用同一套标签加减 API，避免两套语义。

## 四、数据模型

### 4.1 标签规范

- 存储格式：`原创,精排,R18`
- 分隔符：半角逗号；输入时兼容全角逗号和分号，内部统一为半角逗号。
- 去重：保持首次出现顺序。
- 系统标签不进入 `user_tags`。
- 空标签、纯空白直接丢弃。

### 4.2 SQLite

```sql
ALTER TABLE sources ADD COLUMN user_tags TEXT NOT NULL DEFAULT '';
```

- `SCHEMA_VERSION` 从 2 升到 3。
- `v_sources` 增加 `s.user_tags`。
- 旧库通过 `NEW_COLUMNS` 自动补列，再执行一次回填迁移。

## 五、文件结构

| 文件 | 职责 |
| :--- | :--- |
| `core/tags.py` | 新增：标签规范化、拆分、合并、系统标签判定 |
| `core/store.py` | `user_tags` 列、标签 CRUD、导出合并、迁移 |
| `core/organizer.py` | 删除白名单，重建系统标签时保留任意用户标签 |
| `core/models.py` | 调整「原创」自动检测逻辑 |
| `backend/schemas.py` | `SourceOut.user_tags`、标签请求模型 |
| `backend/api/sources.py` | 标签总览、加减、重命名、合并、删除、规范化接口 |
| `backend/api/ops.py` | check job 完成后重建系统标签 |
| `frontend/src/api/sources.js` | 新增标签 API 调用 |
| `frontend/src/components/SourceEditDialog.vue` | 用户标签 `el-select`，系统标签只读 |
| `frontend/src/views/SourcesView.vue` | 行内标签展示、批量加/去标签 |
| `frontend/src/components/GroupManagerDrawer.vue` | 新增：分组管理抽屉 |
| `tests/test_tags.py` | 新增：标签工具测试 |
| `tests/test_store_tags.py` | 新增：Store 标签行为测试 |
| `tests/test_organizer.py` | 更新：用户标签保留、白名单删除 |

## 六、分任务计划

### Task 1：新建标签工具 `core/tags.py`

**Files**

- Create `core/tags.py`
- Create `tests/test_tags.py`

**Interfaces**

```python
SYSTEM_TAGS: set[str]
def normalize_tags(value: str | list[str]) -> list[str]: ...
def is_system_tag(tag: str) -> bool: ...
def split_system_user(tags: list[str]) -> tuple[list[str], list[str]]: ...
def merge_group(system_tags: list[str], user_tags: list[str]) -> str: ...
def extract_user_tags_from_group(group: str) -> list[str]: ...
def normalize_group(group: str) -> str: ...
```

**Steps**

- [x] Step 1: 写失败测试：`原创`、`精排`、`R18` 去重保序；`可用` 被判为系统标签。
- [x] Step 2: 实现 `normalize_tags`：支持 `,`、`，`、`;`、`；`，trim 后去重保序。
- [x] Step 3: 实现 `split_system_user` 和 `merge_group`。
- [x] Step 4: 实现旧分组解析：拆出 `✅★★★★★`、`📖小说/✅` 这类历史系统标记。
- [x] Step 5: 运行 `python -m unittest tests.test_tags -v`。

### Task 2：Store schema 与迁移

**Files**

- Modify `core/store.py`
- Create `tests/test_store_tags.py`

**Steps**

- [x] Step 1: `SCHEMA_VERSION = 3`。
- [x] Step 2: `DDL` 的 `sources` 加 `user_tags TEXT NOT NULL DEFAULT ''`。
- [x] Step 3: `NEW_COLUMNS["sources"]` 追加 `("user_tags", "TEXT NOT NULL DEFAULT ''")`。
- [x] Step 4: `VIEW_DDL` 加 `s.user_tags`。
- [x] Step 5: 新增 `migrate_user_tags_once()`：读旧 `group_name` 或 raw `bookSourceGroup`，拆成系统/用户，写回 `group_name` 和 `user_tags`，用 meta key 保证只跑一次。
- [x] Step 6: 测试旧库迁移、幂等、空标签、纯系统标签。

### Task 3：Store 标签读写与导出合并

**Files**

- Modify `core/store.py`
- Modify `tests/test_store_tags.py`

**Steps**

- [x] Step 1: `upsert_sources()` 入参拆成系统标签和用户标签。
  - 新 URL：从 `bookSourceGroup` 提取用户标签，系统标签按类型 + 旧分组推断。
  - 同 URL 冲突：更新规则和系统标签，但 `user_tags` 保持原值。
- [x] Step 2: 新增 `_source_view(raw_json, group_name, user_tags)`，导出/详情时合并 `bookSourceGroup`。
- [x] Step 3: `get_source()`、`export_sources()`、`export_by_filter()` 全部走 `_source_view()`。
- [x] Step 4: 新增标签方法：
  - `add_user_tags(urls, tags)`
  - `remove_user_tags(urls, tags)`
  - `rename_user_tag(old, new)`
  - `merge_user_tags(sources, target)`
  - `delete_user_tag(tag)`
  - `normalize_user_tags()`
  - `tags_overview()`
  - `rebuild_system_tags()`
- [x] Step 5: `_where()` 支持 `user_tag` 精确过滤：用 `(',' || user_tags || ',') LIKE '%,标签,%'`。
- [x] Step 6: 测试导出包含 `系统 + 用户`；加/去标签只动用户标签；重命名全局生效。

### Task 4：organizer 去白名单

**Files**

- Modify `core/organizer.py`
- Modify `core/models.py`
- Modify `tests/test_organizer.py`

**Steps**

- [x] Step 1: 删除 `PRESERVED_TAG_RULES` 和 `_preserved_group_tags`。
- [x] Step 2: `organize_sources()` 从原 `bookSourceGroup` 中提取用户标签，系统标签重建后再拼接。
- [x] Step 3: 系统标签由 `group_title()` + `rec.quality_tags` 中的 `规则完整` 组成。
- [x] Step 4: `core/models.py` 不再把识别到的 `原创` 自动追加到 `quality_tags`；旧数据由迁移一次性转入 `user_tags`。
- [x] Step 5: 更新 `test_organizer.py`：`原创`、`精排`、自定义标签在 organize 后必须原样存在。
- [x] Step 6: 增加测试：用户标签顺序保持；系统标签重复时不重复追加；标签冲突时保留用户标签。

### Task 5：后端 API

**Files**

- Modify `backend/schemas.py`
- Modify `backend/api/sources.py`
- Modify `backend/api/ops.py`

**Steps**

- [x] Step 1: `SourceOut` 增加 `user_tags: str = ""`。
- [x] Step 2: `list_sources()` 增加 `tag: str = ""` 查询参数，支持按用户标签筛选。
- [x] Step 3: 新增 `GET /api/sources/tags`，返回标签总览：
  - `tag`
  - `count`
  - `kind`: `system` / `user`
  - `editable`: bool
- [x] Step 4: 新增标签写接口：
  - `POST /api/sources/tags`：`{ urls, add, remove }`，批量加/去标签。
  - `POST /api/sources/tags/rename`：`{ old, new }`。
  - `POST /api/sources/tags/merge`：`{ sources: [...], target }`。
  - `POST /api/sources/tags/delete`：`{ tag }`。
  - `POST /api/sources/tags/normalize`：全库规范化。
- [x] Step 5: 删除或废弃 `PATCH /api/sources/group`；前端不再调用覆盖接口。
- [x] Step 6: `backend/api/ops.py` 的 check job 完成后调用 `st.rebuild_system_tags()`。
- [x] Step 7: 如果继续使用 `SourceEditDialog` 保存整源，需补 `POST /api/sources/save`；不在本任务内也要列为依赖。

### Task 6：前端分组编辑与分组管理

**Files**

- Modify `frontend/src/api/sources.js`
- Modify `frontend/src/components/SourceEditDialog.vue`
- Modify `frontend/src/views/SourcesView.vue`
- Create `frontend/src/components/GroupManagerDrawer.vue`

**Steps**

- [x] Step 1: `sources.js` 增加 `listTags`、`patchTags`、`renameTag`、`mergeTags`、`deleteTag`、`normalizeTags`。
- [x] Step 2: `SourceEditDialog.vue`：
  - 系统标签用只读 `el-tag` 展示。
  - 用户标签改为 `el-select multiple filterable allow-create default-first-option`。
  - 保存时调用标签 API，不能再整串覆盖 `bookSourceGroup`。
- [x] Step 3: `SourcesView.vue`：
  - 行内编辑改为标签展示 + 标签选择；或统一走单源标签编辑弹窗。
  - 批量条把 `el-input` 换成 `el-select multiple filterable allow-create`。
  - 增加两个动作按钮：`加标签`、`去标签`。
  - 删除 `applyBatchGroup()` 的覆盖循环。
- [x] Step 4: 新增 `GroupManagerDrawer.vue`：
  - 标签总览表格：标签、计数、系统/用户、可编辑。
  - 重命名、合并、删除、规范化。
  - 系统标签只读。
- [x] Step 5: 分组筛选拆成系统分组和用户标签两个筛选条件。

### Task 7：迁移、测试与验收

**Files**

- Modify `core/store_migrate.py`（如需 CLI 迁移入口）
- Modify `tests/test_tags.py`、`tests/test_store_tags.py`、`tests/test_organizer.py`

**Steps**

- [x] Step 1: 启动或迁移时自动补 `user_tags` 列并回填旧分组。
- [x] Step 2: 迁移只执行一次，meta key 例如 `user_tags_migrated_at`。
- [x] Step 3: 迁移前备份 `data/sources.sqlite3`，迁移失败不得写半截。
- [x] Step 4: 验证首次导出：`bookSourceGroup` 等于 `系统标签 + 用户标签`。
- [x] Step 5: 验证 organize：手工加 `精排`、`R18` 后执行 organize，标签仍在。
- [x] Step 6: 验证批量操作：给 10 条源加 `自用`，原有 `原创` 不丢。
- [x] Step 7: 运行：`python -m unittest discover -s tests -t .`。

## 七、回滚策略

- `raw_json` 不动，回滚代码后旧逻辑仍能读取原始 `bookSourceGroup`。
- `user_tags` 列是新增列，不参与 Legado JSON 输出，回滚时可以直接忽略。
- 迁移只做一次；如迁移结果有误，用迁移前的 SQLite 备份恢复。
- 不修改 `fingerprint()`，回滚不会造成缓存大规模失效。

## 八、验收标准

1. 新库、旧库都能自动添加并回填 `user_tags`。
2. `sources.user_tags` 只保存用户标签，`sources.group_name` 只保存系统标签。
3. 导出 / 订阅 / 详情返回的 `bookSourceGroup` 都是系统 + 用户合并结果。
4. 执行 organize 后，任意自定义用户标签不丢。
5. 批量操作有加标签、去标签两种明确语义，不再覆盖抹掉其他标签。
6. 标签总览可以区分系统标签和用户标签，并能重命名、合并、删除、规范化。
7. 标签编辑不会使 `fingerprint` 变化，不会触发校验缓存失效。
8. 给 Legado 的 JSON 仍然是合法数组，字段结构不变。

## 九、风险与待确认

1. `规则完整`：当前 organizer 有意不把它写进分组，本计划按需求将其列为系统标签；若后续希望继续只放在报告里，需要调整 Task 4。
2. `原创`：当前 `build_record()` 会根据名称/备注自动追加 `原创`。本计划建议改成用户标签，并移除自动追加，否则用户删不掉。
3. `SourceEditDialog.save()` 目前没有后端保存接口；标签编辑可以先走标签 API，但整源新建/保存仍需要单独补 `POST /api/sources/save`。
4. `PATCH /api/sources/group` 一旦保留，容易重新引入覆盖语义；建议前端完成切换后删除该路由。
5. 如果标签内含逗号，会被拆成两个标签；需要前端提示或用其他受限字符。


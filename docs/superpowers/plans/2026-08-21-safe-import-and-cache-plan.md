# 安全导入与校验缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为外部书源提供可追溯的导入批次和待审冲突队列，并让校验缓存仅在规则未变且结果未过期时复用。

**Architecture:** 新增 `registry.py`，以 SQLite 保存导入批次和同 URL 规则冲突；候选书源 JSON 保持为 Legado 导出来源，外部新源和冲突源均不自动写入它。`checker.py` 为缓存写入规则指纹和版本 5，并在复用前校验指纹和状态有效期。

**Tech Stack:** Python 3、标准库 `sqlite3`/`unittest`、现有 `orjson` 与 `aiohttp`。

## Global Constraints

- 不引入第三方依赖；SQLite 仅使用 Python 标准库。
- 外部同 URL 且规则变化的书源必须进入待审，禁止自动覆盖候选库。
- 新 URL 保持待校验，禁止自动写入候选库。
- 缓存仅在 URL、规则指纹和有效期均匹配时复用。
- 状态有效期默认：可用 14 天；待验证、需代理复检和其他状态 7 天。
- 书源可见分组与备注不得新增 `命中《》` 标记。
- 当前目录不是 Git 仓库；每项完成后运行测试，不执行提交。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `legado-tools/registry.py` | 创建 SQLite 表、导入批次、冲突记录、待审查询和审批后的候选库更新 |
| `legado-tools/main.py` | 新增 `import-sources`、`review-imports` 两个 CLI 入口 |
| `legado-tools/checker.py` | 缓存指纹、版本 5、有效期判定和复用控制 |
| `legado-tools/tests/test_registry.py` | 导入批次、重复、冲突隔离和审批测试 |
| `legado-tools/tests/test_checker_cache.py` | 规则变化和过期缓存不复用、有效缓存复用测试 |
| `legado-tools/WORKFLOW.md` | 更新外部书源的推荐操作链路 |

### Task 1: 建立可重复运行的测试基础

**Files:**
- Create: `legado-tools/tests/__init__.py`
- Create: `legado-tools/tests/test_registry.py`
- Create: `legado-tools/tests/test_checker_cache.py`

**Interfaces:**
- Consumes: `unittest`, `tempfile.TemporaryDirectory`
- Produces: `python -m unittest discover -s tests -v` 可执行的测试入口

- [ ] **Step 1: 写入导入行为的失败测试**

```python
def test_import_marks_new_url_pending_without_changing_candidate(self):
    result = import_sources(candidate, incoming, registry_path, raw_dir)
    self.assertEqual(result.pending_count, 1)
    self.assertEqual(read_json(candidate), original_candidate)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_registry -v`

Expected: FAIL，提示 `registry` 或 `import_sources` 尚不存在。

- [ ] **Step 3: 写入缓存有效性失败测试**

```python
def test_cache_with_changed_rule_fingerprint_is_not_reused(self):
    item = make_cache_item(source_a, checked_at="2026-08-21 10:00:00")
    self.assertFalse(is_cache_item_valid(source_b, item, now=NOW))
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m unittest tests.test_checker_cache -v`

Expected: FAIL，提示 `is_cache_item_valid` 尚不存在。

- [ ] **Step 5: 保持测试仅依赖临时目录和固定时间**

```python
NOW = datetime(2026, 8, 21, 12, 0, 0)

def tearDown(self):
    self.temp_dir.cleanup()
```

### Task 2: 实现导入批次、待审冲突与审批

**Files:**
- Create: `legado-tools/registry.py`
- Modify: `legado-tools/tests/test_registry.py`

**Interfaces:**
- Consumes: `candidate_path: str`, `incoming_path: str`, `registry_path: str`, `raw_dir: str`
- Produces: `import_sources(...) -> ImportSummary`、`list_pending_reviews(registry_path) -> list[ReviewItem]`、`approve_review(..., url: str) -> bool`、`approve_pending_source(..., url: str) -> bool`

- [ ] **Step 1: 使新 URL 测试通过**

实现 `import_sources`：读取候选库和外部数组，调用 `sanitize.clean_source` 清洗外部副本，按 `loader._normalize_url` 建立 URL 索引；新 URL 写入 `pending_sources` 表，候选库文件不写入。

```python
@dataclass(frozen=True)
class ImportSummary:
    batch_id: int
    new_count: int
    duplicate_count: int
    conflict_count: int
    pending_count: int
```

- [ ] **Step 2: 运行单项测试确认通过**

Run: `python -m unittest tests.test_registry.ImportSourcesTests.test_import_marks_new_url_pending_without_changing_candidate -v`

Expected: PASS。

- [ ] **Step 3: 写入并验证重复与冲突的失败测试**

```python
def test_same_url_same_rule_is_recorded_as_duplicate(self):
    self.assertEqual(import_sources(...).duplicate_count, 1)

def test_changed_rule_is_queued_for_review_without_overwriting_candidate(self):
    self.assertEqual(import_sources(...).conflict_count, 1)
    self.assertEqual(len(list_pending_reviews(registry_path)), 1)
```

- [ ] **Step 4: 创建 SQLite 架构并实现冲突隔离**

创建 `import_batches`、`pending_sources`、`review_items` 三张表。每条 `review_items` 保存规范 URL、候选规则 JSON、外部规则 JSON、候选和外部指纹、导入批次、状态 `pending` 和创建时间。相同 URL 且相同指纹只计重复；指纹不同则写待审项。

- [ ] **Step 5: 运行导入测试确认通过**

Run: `python -m unittest tests.test_registry.ImportSourcesTests -v`

Expected: PASS。

- [ ] **Step 6: 写入审批覆盖的失败测试**

```python
def test_approving_review_replaces_only_the_selected_candidate(self):
    self.assertTrue(approve_review(registry_path, candidate_path, "https://a.example"))
    self.assertEqual(read_json(candidate_path)[0]["ruleSearch"], incoming_rule)
```

- [ ] **Step 7: 实现审批与原始文件留存**

导入时以 `YYYYMMDD_HHMMSS_<原文件名>` 将原文件复制到 `raw_dir`；审批时仅用对应待审项的外部 JSON 更新候选库中同规范 URL 的条目，更新待审项状态为 `approved`。找不到待审项或候选 URL 时返回 `False`，不写文件。

- [ ] **Step 8: 运行全部导入测试确认通过**

Run: `python -m unittest tests.test_registry -v`

Expected: PASS。

- [ ] **Step 9: 补齐待校验新源的受控入库闭环**

新增 `approve_pending_source(registry_path, candidate_path, url)`：仅当该 URL 仍处于 `pending_check` 且候选库不存在同规范 URL 时，才将已清洗的暂存规则追加到候选库，并把暂存状态更新为 `approved`。`review-imports --approve-new URL` 调用该函数；该命令是人工确认校验通过后的动作，不会自动放行新源。

### Task 3: 暴露安全导入和待审审批 CLI

**Files:**
- Modify: `legado-tools/main.py`
- Modify: `legado-tools/tests/test_registry.py`

**Interfaces:**
- Consumes: `python main.py import-sources --candidate CANDIDATE -i INCOMING --registry REGISTRY --raw-dir DIR`
- Produces: 导入摘要；`python main.py review-imports --registry REGISTRY --candidate CANDIDATE --list`；`--approve URL`

- [ ] **Step 1: 写入 CLI 解析失败测试**

```python
def test_import_command_accepts_candidate_registry_and_raw_dir(self):
    args = build_parser().parse_args([
        "import-sources", "--candidate", "candidate.json", "-i", "incoming.json",
        "--registry", "registry.sqlite3", "--raw-dir", "imports/raw",
    ])
    self.assertEqual(args.command, "import-sources")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_registry.ImportCliTests -v`

Expected: FAIL，提示未知子命令 `import-sources`。

- [ ] **Step 3: 增加最小 CLI 实现**

添加 `cmd_import_sources` 和 `cmd_review_imports`。导入命令打印批次编号、新增、重复、冲突、待校验数量；审核命令无动作时列出待审 URL 和来源批次，`--approve URL` 调用审批函数。两个命令的候选文件均为显式必填参数，避免自动探测误写。

- [ ] **Step 4: 运行 CLI 与导入测试确认通过**

Run: `python -m unittest tests.test_registry -v`

Expected: PASS。

### Task 4: 为缓存加入指纹与有效期

**Files:**
- Modify: `legado-tools/checker.py`
- Modify: `legado-tools/tests/test_checker_cache.py`

**Interfaces:**
- Consumes: `is_cache_item_valid(record: BookSourceRecord, item: dict, now: datetime) -> bool`
- Produces: 缓存版本 5，包含 `fingerprint`；`AsyncChecker.run` 仅恢复有效缓存

- [ ] **Step 1: 写入过期规则的失败测试**

```python
def test_ok_cache_older_than_fourteen_days_is_not_reused(self):
    item = make_cache_item(source, health=Health.OK, checked_at="2026-08-07 11:59:59")
    self.assertFalse(is_cache_item_valid(source, item, now=NOW))

def test_auth_cache_within_seven_days_is_reused(self):
    item = make_cache_item(source, health=Health.AUTH, checked_at="2026-08-15 12:00:00")
    self.assertTrue(is_cache_item_valid(source, item, now=NOW))
```

- [ ] **Step 2: 运行缓存测试确认失败**

Run: `python -m unittest tests.test_checker_cache -v`

Expected: FAIL，提示有效期或版本 5 规则未实现。

- [ ] **Step 3: 实现缓存有效性判定**

在 `checker.py` 导入 `datetime` 和 `loader.fingerprint`。新增 `CACHE_VERSION = 5` 与 `is_cache_item_valid`：要求 `item["v"] == 5`、`item["fingerprint"] == fingerprint(record.raw)`，解析 `%Y-%m-%d %H:%M:%S` 的 `checked_at`，按 `Health.OK` 14 天、其他状态 7 天判断。时间缺失、格式错误和未来时间均视为无效。

- [ ] **Step 4: 更新写入与批量复用逻辑**

`save_cache_append` 写入版本 5 与规则指纹；`AsyncChecker.run` 改为调用 `is_cache_item_valid` 后才执行 `restore_from_cache`。旧版本 3/4 缓存自然失效并复检，不删除旧缓存文件。

- [ ] **Step 5: 运行缓存测试确认通过**

Run: `python -m unittest tests.test_checker_cache -v`

Expected: PASS。

### Task 5: 更新操作文档并做回归验证

**Files:**
- Modify: `legado-tools/WORKFLOW.md`
- Modify: `legado-tools/tests/test_registry.py`
- Modify: `legado-tools/tests/test_checker_cache.py`

**Interfaces:**
- Consumes: 新 CLI 命令与缓存策略
- Produces: 可复制的外部导入、查看待审和审批命令

- [ ] **Step 1: 更新工作流文档**

将外部书源流程替换为安全导入示例：

```powershell
python main.py import-sources --candidate candidates.json -i source_import.json --registry book_sources.sqlite3 --raw-dir imports/raw
python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --list
python main.py review-imports --candidate candidates.json --registry book_sources.sqlite3 --approve "https://example.com"
```

写明：新增源进入待校验，同 URL 同规则只留记录，规则变化必须人工审批；缓存会在规则改变或到期后自动复检。

- [ ] **Step 2: 运行全部测试**

Run: `python -m unittest discover -s tests -v`

Expected: PASS，所有导入、审批、规则指纹和有效期测试成功。

- [ ] **Step 3: 运行 CLI 帮助检查**

Run: `python main.py -h`

Expected: 帮助列表包含 `import-sources` 和 `review-imports`。

- [ ] **Step 4: 检查工作区改动范围**

Run: `Get-ChildItem registry.py, main.py, checker.py, WORKFLOW.md, tests -Recurse`

Expected: 仅列出本计划涉及的实现、文档和测试文件。

# 校验结果的缓存口径与处理闭环设计

> 状态：待实施（2026-09-16）
> 目标：修掉"不同探测能力的校验结果互相复用"这个静默失效，并让校验结果**可以被处理、可以被回看**。
> 范围：`core/checker.py`、`core/store.py`、`backend/api/ops.py`、`backend/api/sources.py`、`backend/schemas.py`、`frontend/src/views/SourcesView.vue`、`frontend/src/api/sources.js`、`frontend/src/components/JobsDrawer.vue`

---

## 一、背景与问题

### 1.1 库的现状

```
未删除源 3861   健康度 None（从未校验）3860   星级 None 3860
分布：type0 3508 / type2 222 / type3 63 / type1 62 / type4 6
```

**99.97% 的源从未被校验过。** 换句话说，"第一次全量校验"还没发生，而它一旦发生，写下的结论会被缓存 14 天（可用源）。**这一版判定口径里的任何错误，都会以"看起来正常"的形式固化两周。**

### 1.2 「先粗后细」是静默失效的（实测）

设想的分步体检是：先用关掉搜索探测的快速体检筛一遍，再对可达的源做标准校验。实测这个流程不成立：

| 遍 | `probe_search` | health | search_hit | 缓存命中 | 实际请求 |
|---|---|---|---|---|---|
| 第一遍 | `False` | `ok` | `''` | 0 | **是** |
| 第二遍 | `True` | `ok` | `''` | **1** | **否** |

第二遍**直接复用了第一遍的缓存**，搜索探测根本没跑，而界面显示「校验完成：全部命中缓存」。

根因：`is_cache_item_valid`（`core/checker.py`）判定复用只比版本 / 指纹 / 时间 / `min_depth`，**不比这条缓存是"带着什么探测能力"写下的**。

副作用比"没跑"更糟——`calc_stars` 里：

```python
stars = 1  # 可达
if not (has_search and search_response_ms > 0):
    return stars          # 快速体检的 search_response_ms 恒为 0 → 直接 1★
```

快速体检会给**每一个可达源**写下一星，并且因为第二遍复用缓存，这个错误结论两周内不会被纠正。

### 1.3 结果无法处理：选择是页级的

```
frontend/src/views/SourcesView.vue:112   order: "-stars", limit: 50, offset: 0
frontend/src/views/SourcesView.vue:510   @selection-change="(v) => (selected = v)"
```

`el-table` 的勾选只覆盖渲染出来的行，而列表是服务端分页的。筛出 800 条死链之后，要**翻 16 页**才能把它们处理完。

同时传输层也不支持批量：`deleteSources` 走 URL 查询串（`api.del("/sources?urls=" + ...)`）。
后端 `Store.soft_delete(urls, reason)` 本身接受列表、走 `IN (?,?,...)`，没有批量限制——**堵在传输层，不在存储层**。

> **阈值实测**（2026-09-16，库副本 + uvicorn）：约 1600 条 / 57KB **通过**，2000 条 / 72KB
> 被服务端以 `400 Bad Request` 拒绝；全库 3850 条拼起来约 139KB，必然撞上。
> 原稿在这里说的「800 个 URL 约 24KB，超长」**不成立**——24KB 远在阈值之下；评审时
> 据此推断的「一两百条就会超」同样不成立。真实触发条件是**上千条**，而「全选全部」
> 正好落在那里。所以改造仍然必要，但理由是**语义**（批量数据不该塞进 URL）与
> 「全选全部」这一档，不是几百条就崩。

### 1.4 结果无法回看

校验结果目前只有一句 toast（`reportCheckResult`），错过就没了。任务抽屉（`JobsDrawer.vue`）只显示 `id / 类型 / 状态 / 进度 / 更新时间`，**不显示 `result_json`**。

于是"这次校验改变了什么"这个唯一有价值的信息完全不可得——列表状态变了，但没人知道是哪些变了、往哪个方向变。

---

## 二、目标

1. **修缓存判定口径**：只有"探测能力不低于本次要求"的缓存才可复用。
2. **让筛出的结果可以被整体处理**：批量操作用得上当前筛选的全部结果，不受分页限制。
3. **让校验结果可回看**，且突出**与上一次相比的变化**（新增失效 / 新恢复 / 首次可用）。

---

## 三、非目标

- **不做「分级校验漏斗」。** 论证：①1.2 修好之后，快速体检的收益只有约 1/3 的耗时（全量标准校验 3861 条约 2–5 分钟，去掉搜索探测省 1–2 分钟），代价却是搜索未验、星级被压到 1★；②用户真正卡住的地方在 1.3 和 1.4，不在"怎么分批测"。**把一个测得更差、只快一点点的档位加进来，是负收益。**
- **不改 `CACHE_TTL_DAYS` 的默认值。** 可用源 14 天 / 其余 7 天是既有设计意图（见 `settings_store.DEFAULTS["check"]` 的注释），且已经可以在设置里改。
- **不做跨页保持勾选。** 见 4.6：整批操作走显式 URL 列表，不需要表格记住跨页选择状态。
- **不做校验历史时间线**（每次校验的完整前后对比）。本次只做"上一次 → 这一次"的聚合变化数。
- **不动 `record.probe_depth` 的既有语义**（它记录"实际执行到的深度"，由 `_probe_toc`/`_probe_content` 写 2/3）。新增字段与它并列，不合并。
- **不改 `search_hit` 等既有缓存字段的含义。**

---

## 四、关键设计决策

1. **缓存有效性按"探测能力"判，不只按时间 / 指纹 / 深度。**

   `min_depth` 解决的是"探得够不够深"，但"探没探搜索"是另一根轴，且它是深度 ≥ 2 的**前提**。两者都要判。

2. **新增字段 `search_probed` 记的是"这一次实际验了搜索"，不是"参数开着"。**

   源没有搜索规则（`has_search=False`）或域名不可达（`health != OK`）时，搜索本来就是不会跑的（`check_one` 的条件是 `health == Health.OK and self.probe_search and record.has_search`）。若记录成"参数开着"，这些源的缓存将**永远无法命中**，每次校验都重新发请求。

3. **命中条件必须给"本来就不会跑搜索"的源留出口。**

   精确形式（`is_cache_item_valid` 内）：

   ```python
   # 本次要验搜索，而缓存是"没验搜索"时写下的 → 不可复用。
   # 但对那些本来就走不到搜索的源（非 OK、无搜索规则）不设此要求，
   # 否则它们永远命中不了缓存，每次都得重新发请求。
   if (min_search and health == Health.OK and record.has_search
           and not item.get("search_probed")):
       return False
   ```

   注意方向：只在**本次要求更高**时作废，缓存比本次更"强"（验过搜索、本次不验）时照常复用。

4. **`CACHE_VERSION` 从 7 升到 8。**

   判定口径变了。现在缓存里的条目没有 `search_probed` 字段，`item.get("search_probed")` 为假——对一个开着搜索探测的用户来说，**所有历史 OK 缓存都会被视为"没验过搜索"而重验**。这正是想要的，但它必须显式发生：否则（若不加版本号而只靠缺字段）行为依赖"字段缺失"这一巧合，且未来加回该字段时会静默失效。升起版本号是既有惯例（v6/v7 都是这个理由）。

5. **批量删除走 POST body，不改软删除语义。**

   `POST /api/sources/delete`，body `{"urls": [...]}`，与既有的 `POST /api/sources/restore` 形状一致。`Store.soft_delete` 不动（它本来就能吃列表）。**旧的 `DELETE /api/sources?urls=...` 直接替换掉，不加兼容层**——唯一调用方是前端，同一次提交里一起改。

6. **「全选 N 条」传显式 URL 列表，不传筛选条件。**

   两个方案：
   - (a) 前端先拉全量 URL，再把列表交给删除接口
   - (b) 删除接口接受筛选条件，由后端解析

   选 (a)。理由：批量条上写的是「已选 N 条」，用户的心智是"我选中了这 N 条"；传筛选条件意味着后端在执行时重新解析一遍筛选，两处筛选口径要各自维护——而 `list_sources` 的筛选参数已经在 `st.query` / `st.count_query` 里有一份了。代价是多一次轻量请求（只取 URL）。

7. **变化摘要在 job 内算，不新增查询接口。**

   `run_check_job` 在跑之前已经握有 `Store` 实例，读一次上一版结论是现成的（`st.checks_map()`）。新增接口意味着前端要发第二个请求，还要处理"快照与结果之间数据变了"的窗口。

8. **摘要只报"变了什么"，不报全量分布。**

   全量分布（各健康度多少条）统计条上的 chip 已经实时展示了（`stats.health`）。重复一遍没有信息量，还会让摘要变长。

9. **「首次有结论」与「变成 X」分开报。**

   库里 3861 条里 3860 条从未校验过，第一次全量之后"新增可用 2000 条"并不代表"比上次好"——那是**首次**。混在一起这个数字就失去意义。

---

## 五、数据契约

### 5.1 缓存项新增字段

`AsyncChecker.save_cache_append` 写入的 item（`core/checker.py:513-）新增：

```python
"search_probed": bool,   # 本次是否真的跑过搜索探测
```

与既有 `probe_depth`（实际执行到的深度）并列，互不替代。

**store 后端必须同步加列**——这是原稿漏掉的一处。Web 链路走的是 `use_store=True`，
`save_cache_append` 写的 item 要经 `Store.save_checks` 落进 `checks` 表，再由
`checks_map()` 读回来。表里没有 `search_probed` 列的话，item 里写了也读不回来：
`item.get("search_probed")` 恒为 None，于是**所有 OK 源的缓存永远被判为「没验过
搜索」**，每次校验都重新发请求。表现是「校验跑完了、状态也变了」，看不出异常，只是慢
——而慢在 3861 条上是几分钟，很难归因到这里。四处都要改：`DDL` 的建表语句、
`NEW_COLUMNS`（给已有库幂等补列）、`save_checks` 的 INSERT 列与值、`checks_map`
读回时转成 bool。

### 5.2 check job 的结果新增 `transitions`

```jsonc
{
  "checked": 3861,
  "cached": 200, "fetched": 3661,
  "params": { ... },
  "transitions": {
    "first_checked": 3661,        // 之前没有校验记录、这次有了结论
    "changed": {                  // 相对上一条记录，health 变化了的分桶
      "ok": 5, "dead": 3, "auth": 1, "gfw": 0, "no_search": 0, "timeout": 0, "error": 0
    }
  },
  "items": [ ... ]
}
```

- `changed` 的 key 是**新的 health**，只统计"变了"的那些；没变的不出现或为 0。
- 完全没有历史记录的源不计入 `changed`，只计入 `first_checked`。
- 缓存命中的源 health 必然没变，不会进 `changed`。

### 5.3 新增 `GET /api/sources/urls`

给「全选 N 条」用。查询参数与 `GET /api/sources` **完全一致**（复用 `st.query` 的筛选口径），返回：

```jsonc
{ "urls": ["https://a.com", "..."], "total": 800 }
```

不分页——调用方的语义就是"要全部"。上限由 `total` 本身兜底（本地单用户，最大量级即全库 3861）。

### 5.4 `POST /api/sources/delete` 取代 `DELETE /api/sources`

```jsonc
// 请求
{ "urls": ["..."], "reason": "" }
// 响应（沿用既有形状）
{ "deleted": 800, "snapshot": "data/backups/deleted_20260916_120000.json" }
```

---

## 六、① 缓存判定：把「验过搜索」纳入有效性

### 6.1 写入

`core/checker.py` 的 `AsyncChecker.save_cache_append`，在 item 里加一个字段。值的来源是**这次实际有没有跑搜索探测**：

```python
"search_probed": bool(record.search_response_ms or record.search_hit),
```

这需要斟酌：`search_response_ms` 在 `_probe_search` 里被赋值（无论命中与否），未跑过时为 0；`search_hit` 只在命中时非空。**「跑过但没命中」** 是最需要与"没跑"区分的情况，而它会让 `search_response_ms > 0`。两者取或即可覆盖"跑过（命中/未命中）"。

**已核准**：`_probe_search` 里 `record.search_response_ms = int(s_cost)` 位于**全部 5 个 `return` 之前**（传输失败、403/401/429、5xx、反爬特征、命中各一），所以凡是发出了请求并拿到响应的路径都会写上它。

唯一为 0 的路径是 `parse_search_request` 或 `_request` **抛异常**后 `except: continue`、关键词耗尽 → 返回 `None`。即"跑了，但每次都在抛异常"。

这个方向是**对的**：那种情况下 `search_probed` 记成 False，表现为下次校验重新探测（保守、多打一次请求），而不是错误地复用（危险、把没验过的结论固化 14 天）。**实现时要在注释里写明这个方向性**，以免后来者把它"修正"成严格相等。

### 6.2 判定

`is_cache_item_valid` 新增参数 `min_search: bool = False`，判定见 4.3 的代码片段。

参数命名沿用既有的 `min_depth`（表示"本次要求的最低探测能力"）。

### 6.3 传递

`AsyncChecker.__init__` 新增 `min_search: bool = False`（或直接复用 `self.probe_search`）：

- `AsyncChecker.run()` 里调用 `is_cache_item_valid` 时传 `min_search=self.probe_search`
- 其余调用方（CLI 的 organize/report、`core/cache_parity.py`）不传，保持 `False` = 不因搜索作废——它们只是拿缓存算标签和报告，重跑不了探测。这与 `min_depth` 的既有处理完全一致。

### 6.4 版本

`CACHE_VERSION = 8`，并按要求改写它上方那段注释（v6/v7 的注释都在那里，追加 v8 的理由：判定口径新增"验过搜索"这一维）。`tests/test_checker_judge.py::test_version_bumped` 同步改。

### 6.5 测试

`tests/test_checker_cache.py::CacheValidityTests` 补：

| 用例 | 契约 |
|---|---|
| `test_search_unprobed_cache_is_not_reused_when_search_required` | 缓存 `search_probed=False` + `min_search=True` + `has_search=True` + health OK → 复用 |
| `test_search_probed_cache_is_reused` | 同上但 `search_probed=True` → 复用 |
| `test_cache_without_search_requirement_still_reused` | `min_search=False` 时 `search_probed=False` 的缓存照常复用（反向） |
| `test_source_without_search_rule_is_unaffected` | `has_search=False` 的源即使 `min_search=True` 也不作废 |
| `test_unreachable_source_is_unaffected` | health 非 OK 的浅缓存不受 `min_search` 影响 |

**前两条必须成对存在**——单独任何一条都拦不住"方向写反"（把"本次要求更高"写成"缓存要求更高"）。

---

## 七、② 全选筛选结果

### 7.1 后端

**新增 `GET /api/sources/urls`**（`backend/api/sources.py`）：参数与 `list_sources` 一致，返回全部匹配的 URL 与总数。实现上直接复用 `st.query(...)` 用一个足够大的 limit，或新增 `Store.query_urls(...)` 只取 `source_url` 一列——后者更省内存，**推荐后者**，形状参照既有的 `count_query` 与 `query` 的配对写法。

**新增 `POST /api/sources/delete`**（`backend/api/sources.py` + `backend/schemas.py` 加 `SourceDeleteIn`）：替换 `DELETE /api/sources`。逻辑与现有 `soft_delete_sources` 相同，只是 `urls` 从查询参数换成 body 列表。

> 注意 `list_sources` 的参数里有 `include_deleted`，而"全选"必须只在未删除范围内——`GET /api/sources/urls` 不接受 `include_deleted`（硬编码为 False），避免把回收站里的源也选进来。

### 7.2 前端

- `frontend/src/api/sources.js`：
  - `listSourceUrls(params)`（新）
  - `deleteSources(urls)` 改成 `api.post("/sources/delete", { urls })`
- `frontend/src/views/SourcesView.vue`：
  - 批量条（`:438` 附近）在「已选 N 条」旁加一个「选中全部 N 条筛选结果」。N 用 `total`（即当前筛选的结果总数），未做筛选时就是全库数
  - 点击后调 `listSourceUrls(query)` 拿回全部 URL，写入 `selected`
  - 移动端批量条（`v-if="isMobile"` 那段）同样加

**关于 `selected` 的内容**：现在是表格行对象数组（`selected.value.map((r) => r.source_url)`），"全选"拿不到行对象。要把 `selected` 的语义统一成 **URL 字符串数组**，用 `selectedUrls` 判断行是否选中。**是本项里改动面最大的地方，单独一步做**，改之前先把消费者列全：

| 位置 | 现在的用法 | 改成 URL 数组后 |
|---|---|---|
| `SourcesView.vue:179` `isSelected` | 走 `selectedUrls` 查 url | 不受影响 |
| `SourcesView.vue:182-184` `toggleCard` | 存/删**行对象** | 存/删 url |
| `SourcesView.vue:203` `removeSelected` | `.map((r) => r.source_url)` | 直接用 |
| `SourcesView.vue:213` `applyBatchTags` | 同上 | 直接用 |
| `SourcesView.vue:456` `openCheckDialog` | 同上 | 直接用 |
| `SourcesView.vue:510` `@selection-change` | el-table 给**行对象数组** | 在这里转成 url |
| `SourcesView.vue:637` → **`ExportDrawer.vue:75`** | `.map((r) => r.source_url)` | 直接用 |

**最后一行是最容易漏的一处**：`ExportDrawer` 只认 `props.selected` 的 `.length` 和 `.map((r) => r.source_url)`。语义换成字符串数组后，`.length` 照常（`:130,136` 显示「已勾选 N 条」），而 `.source_url` 对字符串恒为 `undefined`——**界面显示勾选了 N 条，导出出去的却是空列表，不报错、界面上看不出来**。改 `selected` 语义时必须一并改它。

- 回收站的批量恢复（`TrashDrawer`）可顺带对齐，但**不在本次范围**（它已有自己的实现）。

---

## 八、③ 校验结果的变化摘要

### 8.1 后端

`backend/api/ops.py::run_check_job`：

1. 在 `checker.run(records)` **之前**读一次上一版结论：`prev = st.checks_map()`（返回 `{url: item}`，item 里有 `health`）。注意键是**规范化后**的 URL，比较时两边都要规范化（`core.loader._normalize_url`）——这是本项目记过多次的坑。
2. 跑完后，对 `results` 逐条：`old = prev.get(norm(url), {}).get("health")`，`new = r.health`
   - `old` 不存在 → `first_checked += 1`
   - `old != new` → `changed[new] += 1`
3. 写进返回值（见 5.2）

**缓存命中的条目 old == new，自然不进 `changed`**，不需要特殊处理。

### 8.2 前端

- `SourcesView.vue::reportCheckResult`：除了现有的 `cached/fetched/save_failures`，再读 `transitions`，把变化用一条 `ElNotification`（比 `ElMessage` 停留久）报出来：
  - 有变化：「本次校验：新增失效 3、新恢复 5、首次可用 120」
  - 无变化：「本次校验：无状态变化」
  - 首次校验（`first_checked` 占绝大多数）单独一句：「其中 3661 条首次有结论」
- `JobsDrawer.vue`：任务详情里展示 `result_json` 的摘要（至少 transitions + cached/fetched），让结果**可回看**。现在这个抽屉只显示进度，是最容易补的一处。

### 8.3 不做的事

- 不做"点击变化数 → 跳转到对应筛选"。它有价值，但需要与统计条 chip 的筛选状态协同，独立成二期。

---

## 九、文件清单

| 文件 | 动作 |
|---|---|
| `core/checker.py` | `save_cache_append` 写 `search_probed`；`is_cache_item_valid` 加 `min_search`；`run()` 传参；`CACHE_VERSION` 7→8 |
| `core/store.py` | `checks` 表加 `search_probed` 列（DDL + `NEW_COLUMNS` + `save_checks` 的 INSERT + `checks_map` 读出）；新增 `query_urls(...)`（只取 URL，供 `/api/sources/urls`） |
| `backend/api/sources.py` | 新增 `GET /urls`；`DELETE /sources` 换成 `POST /sources/delete` |
| `backend/schemas.py` | 新增 `SourceDeleteIn` |
| `backend/api/ops.py` | 跑前快照 + 变化统计，写进返回值 |
| `frontend/src/api/sources.js` | `listSourceUrls`；`deleteSources` 改 POST |
| `frontend/src/views/SourcesView.vue` | 「选中全部 N 条」；`selected` 语义改为 URL 数组；摘要展示 |
| `frontend/src/components/ExportDrawer.vue` | 跟着 `selected` 改 URL 数组（`.map((r) => r.source_url)` 会静默变 `undefined`） |
| `frontend/src/components/JobsDrawer.vue` | 展示任务结果摘要 |
| `tests/test_checker_cache.py` | 5 条缓存判定用例 + 变异记录 |
| `tests/` | 变化统计的用例（`test_check_job_settings.py` 或新建） |
| `README.md` | API 表加 `/api/sources/urls`、删除接口改 POST；`CACHE_VERSION` 说明 |
| `AGENTS.md` | 硬性约定 5 补一句：缓存有效性的两根轴 |

---

## 十、测试与验收

### 10.1 单测

- 缓存判定 5 条（见 6.5），**成对的反向断言必须齐**
- 变化统计：构造 `prev` 与 `results`，断言 `first_checked` / `changed` 分桶正确；**至少要有一条"health 没变则不计入 changed"**
- `GET /api/sources/urls` 的筛选与 `GET /api/sources` 同口径（可用同参数比对 `total`）

### 10.2 变异验证（本仓库惯例）

- 去掉 4.3 的 `has_search` 出口 → `test_source_without_search_rule_is_unaffected` 红
- 把 4.3 的条件方向写反（缓存要求更高）→ `test_search_unprobed_cache_is_not_reused_when_search_required` 红
- 变化统计把"没变"也算进 `changed` → 对应用例红

### 10.3 端到端实测（不进真实库）

用 `LEGADO_DATA_DIR` 指向库副本，清空 `checks` 表后：

1. `probe_search=False` 跑一条源 → 再 `probe_search=True` 跑同一条 → **第二遍必须重新发请求**（这正是 1.2 那个实验，修好后结论要反过来）
2. 筛出任意子集 → 点「选中全部 N 条」→ 确认 `selected` 数 = `total`
3. 批量移入回收站 → 确认条数与快照文件
4. 全量校验后看摘要：首次校验时 `changed` 应接近全 0、`first_checked` ≈ 总数

### 10.4 验收标准

- 1.2 的实测表结论反转（第二遍真的发请求）
- 800 条筛选结果可以 3 次点击内处理完（筛选 → 全选 → 删除）
- 校验结束后不需要重跑就能知道"哪些变了"

---

## 十一、实施顺序与回退

1. **③ 变化摘要**（**不依赖 ①**，先做）
   - 论证：它比的是 `prev` 快照里的 `health` 与本次 `r.health`，与缓存有效性判定**无关**——缓存命中的源 old==new 天然不进 `changed`，① 修不修都不改变这个性质。原稿把它排在 ① 之后、理由是"依赖 ① 的口径稳定"，不成立。
   - 收益/成本最高：3861 条首次全量里唯一有价值的信息就是"哪些变了"，而后端只有约 20 行。
   - 顺带在真实数据上量一次 `checks_map()` 的耗时——① 的实现要在同一条数据上跑。
2. **① 缓存判定**（独立、可单独回退）
   - 成本极低（约 30 行 + 5 用例），修的是缓存不变量里缺的一根轴。
   - **紧迫性要说准**：默认参数（`probe_search=True`）下走不到 1.2 那个场景，必须先把 `probe_search` 关掉、再打开才触发。所以"必须先做否则污染第一次全量"这个说法偏强——它是纯正确性修复，不是上线阻塞项。另注：1.2 的静默失效只在 `probe_depth=1`（默认）时发生，深度 ≥2 时 `min_depth` 会兜住。
   - 回退：改回 `CACHE_VERSION`（但已写入的 v8 缓存会被判为过期，行为是"重新校验"，安全方向）
3. **② 全选 + 批量删除改 POST**（分两步：先后端接口，再前端）
   - `selected` 语义改动面较大（见 7.2 的消费者表，含 `ExportDrawer.vue`），单独一步，改完立刻手工验一遍勾选/取消/批量加标签/**导出勾选**。
   - 触发阈值（实测）：约 1600 条 / 57KB 通过、2000 条 / 72KB 返回 400。原稿估的
     「800 条 / 24KB 超长」与评审时推断的「一两百条」**都不成立**；但全库 3850 条
     （约 139KB）必然撞上——「全选全部」正是这个功能的用法。

每步一个提交。

---

## 十二、二期候选项

- 点击摘要里的变化数 → 按对应筛选跳转（需要与统计条 chip 的筛选状态协同）
- 校验历史时间线（每次校验的前后对比，而不只是聚合数）
- 快速体检作为一个**明确标注"只测连通、不给星级"**的独立动作（若 1.2 修好后仍有需求）——前提是它能不出星级、不污染 `calc_stars` 的输入
- 跨页保持勾选（本次用显式 URL 列表绕开了，但"我选了这几条、翻页后又想加几条"的场景仍在）
- `TrashDrawer` 的批量恢复对齐同一套 URL 语义

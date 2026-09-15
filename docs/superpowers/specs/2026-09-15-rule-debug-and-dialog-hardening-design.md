# 试跑调试与编辑弹窗加固设计

> 状态：待实施（2026-09-15）
> 目标：让「全链路试跑」对齐「阅读 App」的调试语义——把每一步的原始证据摊开，判定只做「非空 / 报错」的底线；同时修掉编辑弹窗里会静默丢数据的交互缺陷。
> 参照实现：`D:\Documents\GitHub\legado-with-MD3`（Legado MD3 分支），本设计的行为依据均来自该源码，下文标注 `文件:行号`。
> 范围：`core/quality.py`（新增）、`core/rules/replayer.py`、`core/fetch.py`、`core/verify.py`、`core/checker.py`、`backend/api/sources.py`、`frontend/src/components/RuleDebugDrawer.vue`（新增）、`frontend/src/components/SourceEditDialog.vue`

---

## 一、背景与问题

### 1.1 判定不可信

| 现象 | 根因 | 位置 |
|---|---|---|
| content 规则为空也显示绿色「通过」 | 规则为空时写死 `ok=True`，「跳过」和「通过」在数据结构里是同一个值 | `core/verify.py:102-108` |
| 提取到错误页 / 导航栏 / 整块 HTML 也算通过 | 正文判定只有一个条件 `total_len > 100`，不区分内容形态 | `core/verify.py:110` |
| 改规则全靠盲猜 | 只回传 `f"{total_len} 字符"`，看不到提取出了什么，更看不到页面 HTML | `core/verify.py:111` |
| 「规则写错」「页面改版」「规则根本测不了」分不清 | 用了 `extract_all()`，把 `extract_all_ex()` 已经算好的失败原因丢了 | `core/verify.py:8` |

### 1.2 同一源两个入口结论打架

「试跑」走 `core/verify.py`（二态、只看长度），「批量校验」走 `core/checker.py:_probe_content`（三态 `None/True/False`、有图片兜底、有失败归因）。同一个源，试跑说通过、批量校验说 `content_ok=False` 是完全可能的。

### 1.3 调试证据本身失真

`core/fetch.py:fetch()` 只带固定 UA，**不读取书源自身的 `header` / `charset`**。有些站必须带 Referer/Cookie 才吐正文，此时抓回来的 HTML 本身就是错的——「看 HTML 源码来决定规则」这件事失去地基。

### 1.4 编辑弹窗的静默数据丢失

| # | 问题 | 后果 |
|---|---|---|
| ① | `el-dialog` 无 `before-close`、未禁用点遮罩关闭 | 改了几十条规则后误点遮罩，无提示全部丢弃 |
| ② | 新建时填了已存在的 `bookSourceUrl` → `upsert_sources` 按 URL 主键覆盖 | **静默覆盖原源全部规则**，主库数据损坏 |
| ③ | `rawJsonText` 只在打开弹窗时同步，`applyRawJson()` 却是整份替换 | 表单改完再点「应用到表单」→ 静默回滚所有编辑 |
| ④ | `testResult` 不随表单改动失效 | 改了规则还挂着绿色的「全部通过」，用户据此保存 |
| ⑤ | `tabDot('basic')` 恒返回 `"ok"` | 永远亮的绿点，零信息量 |
| ⑥ | 编辑模式域名 `disabled`，提示「需要更换请另存为新源」，但没有「另存为」按钮 | 文案指路却无路 |

### 1.5 与「阅读 App」的对应关系（本次最重要的一条背景）

Legado 里**有两个不同的功能**，此前被混为一谈：

| | Legado 调试（Debug） | Legado 书源校验（Check） |
|---|---|---|
| 对应本项目 | **全链路试跑** | **批量校验** |
| 判定 | **二值**：非空 / 不抛异常 = Success，否则 Failed | **按异常类型分类** |
| 数值阈值 | **无** | 无 |
| 按 `bookSourceType` 分派 | **完全不引用** | 仅 `file` 型跳过目录校验 |
| 价值所在 | **展示原始响应体 + 逐字段值 + 正文全文** | 打「正文失效 / 目录失效」标签 |

证据：

- 调试判定只有「非空」：`model/Debug.kt:285-290`（`exploreBooks.isNotEmpty()` 否则 `state = -1`）、
  `model/webBook/BookContent.kt:203-205`（仅判 `contentStr.isBlank()` → 抛 `ContentEmptyException`）。
- 分类判定在另一处：`data/repository/BookSourceCheckRepository.kt:234-238`
  （`ContentEmptyException → "正文失效"`、`TocEmptyException → "目录失效"`）。
- 调试链路对 `bookSourceType` **零引用**：`model/Debug.kt` 全文不出现该字段；唯一相关分支是
  `Debug.kt:329-332` 的 `book.isWebFile`（由 `downloadUrls` 存在决定，见 `BookExtensions.kt:94`）→ 跳过目录并判完成。

**结论：Legado 的调试不做「正文是否合法」的判断——它把正文全文摊开，由人判断。**

对比本项目的现状（`core/verify.py:111`）：无论长短都只回传 `f"{total_len} 字符"`。
**这才是「无法判断 content 是否正常合法」的真正根因**，而不是判定写得不够聪明。

---

## 二、目标

1. **对齐 Legado 调试语义**：判定底线是「非空 / 不报错」，不自创数值阈值。
2. 每一步摊开**证据**：请求 URL、HTTP 状态、耗时、**提取值（含正文全文）**、命中节点的 HTML 片段、整页源码。
3. 保留我们优于 Legado 的两处增量：`unknown` 灰态（Legado 把「解析为空」与「请求失败」都归为 Error，只差 message 文本）、启发式 `notes` 附注（不改变已验证的通过结论）。
4. 「试跑」与「批量校验」共用同一套形态判定逻辑，消除 1.2 的「同源不同判」。
5. 试跑请求带上书源自身的 `header` / `charset`，与 `AnalyzeUrl(source = bookSource, ...)` 的复用路径对齐。
6. 修复编辑弹窗 P0/P1 六项交互缺陷。
7. **用实测正文形态提示类型不符**：试跑给出「声明为 X，实测像 Y」的 note，并附带切到「基本信息」页签的入口；**不自动改、不新增写路径**（见十五）。

## 三、非目标

- 不做 iframe 渲染、不做元素点选生成规则（本次只做只读证据视图）。
- 不引入新的第三方依赖。
- **不加 Explore（发现）调试步骤**：Legado 调试有 Search / Explore / Info / Toc / Content 五个目标，本次只对齐现有四步。
- **不改 `bookSourceType` 的取值语义**（虽然发现 `3` 与 `4` 的标注有误，见 16.2，但改动会波及存储、导出与全部源，独立成单独任务）。
- **不做书源类型判定的任何改造**（整体推迟，见十五）：不修 `reclassify.py` 的死分支与泛词，不补齐音频/下载源的静态出口，不把判据从「域名/名称」改为「规则形状」。本次只加一条**提示**（15.2）。
- 不做公网权限系统。
- **不做试跑页面缓存复用**（改选择器时只重解析不重抓）。这是调试效率提升最大的一条，但需要后端引入短期页面缓存与「复用」开关，独立成二期。
- **不在前端暴露 `pick` 参数**（搜索结果第 N 条）。后端已支持，前端这次不接。

---

## 四、关键设计决策

1. **判定底线对齐 Legado，不自创阈值**。`fail` 只有两种触发：**请求失败**、**解析结果为空**（对应 `ContentEmptyException` / `TocEmptyException`）。其余一律 `pass`。**明令禁止**再引入「中文字符 < N」「长度 > N」这类数值阈值——它们会误杀短章节，且与 `Debug.kt` 的行为不一致。
2. **启发式只产出附注，不改变结论**。启发式（见 6.3）命中的步骤**仍然是 `pass`**，只是额外挂一条 `warn` 级附注。`verdict` 的取值只在 `pass / fail / unknown` 三者间由「非空/报错/无法回放」决定；`warn` 不作为独立 verdict，而是 `pass` 上的一个标记位 `has_notes`。这样「绿即是真通过」，同时疑点不会被吞掉。
3. **`unknown` 灰态是我们的增量，保留**。Legado 把「解析为空」和「请求失败」都归为 Error（`Debug.kt:290/294`，仅 message 文本不同），也把「静态无法回放的 JS 规则」当成普通失败。我们把后者单独拎出来，避免用工具的能力边界去判源的好坏。
4. **`steps[].ok` 与 `all_ok` 保持原语义不变**（仅 `fail` → `false`），新增字段并存。`verify_chain` 有三个消费方（试跑接口、快速生成任务、AI 修复回放），保持兼容可让后两者零改动。
5. **判定逻辑抽成 `core/quality.py`**，「试跑」与「批量校验」共用。`checker` 的三态映射为 `pass→True`、`fail→False`、`unknown→None`，沿用其「无法验证不给分、不误杀」的既有策略。
6. **职责边界**：`quality` 只判定「提取结果本身是否为空、形态是什么」，不依赖任何外部参考数据；「目录完整度与参考表比例比对」仍留在 `checker`，由它在 `quality` 判定通过的基础上叠加。
7. **证据里必须包含提取值全文，不只是长度**。这是对齐 `BookContent.kt:194-205` 的关键：Legado 短则打长度、长则打全文。我们此前只给 `"1234 字符"`，正是「看不出 content 是否正常」的直接原因。
8. **整页 HTML 独立成 `pages[]` 数组，步骤用 `page_id` 引用**。搜索页在 `search` 与 `bookUrl` 两步共用，内联会重复传输。
9. **`has_notes` 在页签上显示黄点，`fail` 才红点**；`pass` 且无附注才是纯绿。
10. **噪声词只作附注，且只在提取值足够短时提示**。整页含「登录」太常见（导航栏就有），只有「提取值 < 500 字且命中噪声词」才是错误页的可靠信号。命中它**不判 fail**——因为短章节确实可能含「404」这类字样，交给人看全文判断。
11. **试跑请求必须带书源 `header` / `charset`**。Legado 的调试复用 `AnalyzeUrl(source = bookSource, ...)`（`WebBook.kt:62-67` 等），header 由 `AnalyzeUrl.kt:130-141` → `BaseSource.kt:102-124` 拼装。不带 header 的试跑不能宣称「与 App 行为一致」。
12. **对齐 `book.tocUrl` 非空则跳过详情页**（`Debug.kt:318-322`）。本项目已有 `ruleBookInfo.tocUrl` 字段，但试跑从未使用它。
13. **`file` 型书源（`bookSourceType == 3`）跳过目录步**（`Debug.kt:329-332`、`BookSourceCheckRepository.kt:221`）。注意 Legado 实际用的是 `book.isWebFile`（由 `downloadUrls` 存在决定），我们没有该字段，退化为按 `bookSourceType == 3` 判断。
14. **`CACHE_VERSION` 从 5 升到 6**。判定逻辑变了，但缓存里的 `toc_complete` / `content_ok` 是**旧逻辑算出来的**，而 `restore_from_cache` 正是用这些缓存值重算星级（`checker.py:196,263-302`）。不升版本，收拢会"改了不生效"——可用源 TTL 14 天，默认路径会一直读旧结论。代价是触发一次全量重校验，见 14.2。
15. **试跑透传 `proxy`**。`checker` 支持 `proxy` 参数而 `verify` 不支持——需要代理的源在试跑里会连接失败，用户会判「源坏了」，而这正是本次要消除的误判（见 8）。

---

## 五、数据契约

`POST /api/rules/chain` 返回结构（新增字段与旧字段并存）：

```jsonc
{
  "steps": [{
    "name": "content",
    "ok": true,                    // 兼容字段：仅 fail → false（原语义不变）
    "verdict": "pass",             // 新增：pass / fail / unknown（warn 不是 verdict，见下）
    "has_notes": true,             // 有启发式附注（UI 据此显示黄点）
    "detail": "提取到 1823 字符",
    "url": "https://site/read/26888/3.html",   // 实际请求的 URL
    "status": 200,
    "elapsed_ms": 412,
    "shape": "text",               // 实测形态：text / image / audio / mixed / empty
    "reason": "",                  // fail/unknown 时的一句话原因；pass 时为空
    "notes": ["提取值含结构性 HTML 标签（<div>），疑似选到容器而非正文"],
    "evidence": {
      "values_total": 1,           // 提取值条数
      "chars": 1823,               // 总字符数
      "cjk_chars": 812,            // 中文字符数（仅作展示，不参与判定）
      "block_seps": 3,             // 命中节点 HTML 里的块级分隔符数
      "tag_ratio": 0.18,           // 提取值中 HTML 标签字符占比
      "noise_hit": ""              // 命中的噪声词（空=未命中）
    },
    "rule_error": "",              // 规则不可回放的原因（JS/XPath/||/语法错）
    "values": ["提取值全文（对齐 BookContent.kt 的「正文长度或全文」）"],
    "page_id": "chapter"           // 引用 pages[]，无页面时为 ""
  }],
  "pages": [
    {"id": "search",  "url": "https://site/search?q=%E6%88%91", "status": 200,
     "charset": "utf-8", "html": "...", "len": 128394, "truncated": false},
    {"id": "detail",  "url": "https://site/book/26888", "...": "..."},
    {"id": "chapter", "url": "https://site/read/26888/3.html", "...": "..."}
  ],
  "all_ok": false                  // 兼容字段：无 fail 即 true
}
```

页面 `id` 取值：`search`（搜索结果页）/ `detail`（详情页即目录页）/ `chapter`（章节页）。

**三态语义 + 附注位**：

| `verdict` | 色 | 含义 | 触发（**只有这三种**） |
|---|---|---|---|
| `pass` | 绿 | 解析出了非空结果 | 提取值非空且未报错 |
| `fail` | 红 | 确认不可用 | **请求失败** 或 **解析结果为空** |
| `unknown` | 灰 | 无法判定 | 规则缺失、规则含 JS/XPath/`\|\|`、`file` 型跳过、静态无法回放 |

`has_notes: true` 时该步显示**黄点**（`verdict` 仍是 `pass`），表示「通过了，但有疑点，建议看一眼全文」。三色圆点映射：

| 圆点 | 条件 |
|---|---|
| 红 | `verdict == "fail"` |
| 灰 | `verdict == "unknown"` |
| 黄 | `verdict == "pass"` 且 `has_notes` |
| 绿 | `verdict == "pass"` 且无附注 |

前端的「全部通过」改为 `steps.every(s => s.verdict === 'pass')`，不再用 `all_ok`。**注意：有附注的 pass 仍算通过**——附注是提示，不是判决。

**快速生成任务必须剥离证据字段**

`backend/api/ops.py:117` 的「快速生成」任务也调 `verify_chain`，其结果会被 `backend/jobs/runner.py:51` **写入 SQLite 的 `result_json`**，并经 `backend/api/jobs.py:47` 走 SSE 推送。若带上整页 HTML 与正文全文，等于把几 MB 塞进 jobs 表和推送流。（Legado 自己也有同样的取舍：`BookSourceDebugWebSocket.kt:43` 的 WebSocket 版调试主动丢弃响应体。）

**做法：`verify_chain` 恒定返回全量证据，由 `ops.py` 在放进结果前剥掉 `pages` / `matched_html` / `values`。**

```python
v = await asyncio.to_thread(verify_chain, source, keyword, detail_url, pick)
# 快速生成的结果会写进 jobs 表并走 SSE，剥掉体积大的证据字段；判定结论完整保留
v = {**v, "pages": [], "steps": [{**s, "values": [], "matched_html": ""} for s in v["steps"]]}
```

> **为什么不做成 `verify_chain` 的参数**：那会引入两套截断常量、两份模式对比测试，以及前端「这一步有没有 `pages`」的分支判断。既然下面已有与模式无关的硬上限兜底，一个参数就纯属冗余——剥离动作放在**真正关心体积的那一方**（业务侧）更合适。

**截断上限**（模块常量，集中可调）：

| 常量 | 值 | 说明 |
|---|---|---|
| `MAX_PAGE_HTML_CHARS` | 1_000_000 | 单页整页源码上限，超出置 `truncated=true` 并在 UI 标注 |
| `MAX_MATCHED_HTML_CHARS` | 200_000 | 单个命中节点 HTML 上限 |
| `MATCHED_NODES_LIMIT` | 3 | 命中节点最多回传几个（目录列表类规则会命中上百个；3 个足够看出结构） |
| `MAX_VALUE_CHARS` | 0（不限） | 单条提取值**不截断**——正文全文是本次的核心产出（对齐 `BookContent.kt:194-205`） |
| `VALUES_PREVIEW_LIMIT` | 100 | 提取值最多回传几条。正文步通常只有 1 条；列表步（目录）可能上百条，需设上限 |
| `MAX_EVIDENCE_TOTAL_CHARS` | 2_000_000 | `values` + `matched_html` + `pages` 合计的硬上限。超出即截断并在 `notes` 里标注。**这是唯一一道与调用方无关的保险**——`ops.py` 的剥离若将来被改漏，它兜住 |

---

## 六、`core/quality.py`：判定模块

### 6.1 对外 API

```python
@dataclass
class Judgement:
    verdict: str              # pass / fail / unknown（warn 不是 verdict）
    reason: str               # fail/unknown 时的一句话原因；pass 时为空
    shape: str                # 实测形态
    notes: List[str]          # 启发式附注，不改 verdict
    evidence: Dict[str, Any]  # 结构化证据

    @property
    def has_notes(self) -> bool:
        return bool(self.notes)

    @property
    def ok(self) -> bool:     # 兼容映射：仅 fail → False
        return self.verdict != VERDICT_FAIL

    @property
    def checker_state(self) -> Optional[bool]:
        """给 checker 的三态映射：pass→True / fail→False / unknown→None。"""
        if self.verdict == VERDICT_PASS:  return True
        if self.verdict == VERDICT_FAIL:  return False
        return None

def sniff_shape(values: Sequence[str]) -> Tuple[str, Dict[str, int]]
    """实测形态嗅探。逐条分类后取占比最高者；最高占比未过半（<= 0.5）→ mixed；全空 → empty。"""

def judge_content(source_type: int, source: Dict[str, Any],
                  values: Sequence[str], rule: str,
                  matched_html: str = "", rule_error: str = "") -> Judgement
    """正文判定。底线是「非空 / 不报错」，见 6.3。"""

def judge_list_step(step: str, values: Sequence[str],
                    matched_html: str = "", rule_error: str = "") -> Judgement
    """目录 / 搜索结果列表判定（search / bookUrl / toc 三步共用）。"""

def static_misconfig_notes(source: Dict[str, Any]) -> List[str]
    """书源级静态错配检查（与抓取结果无关，一次检查挂到相关步骤的 notes 上）。

    这些错配靠「看 HTML 源码」是发现不了的，只有读 Legado 源码才知道：
      - `ruleContent.webJs` 非空，但 searchUrl / tocUrl 等 URL 规则未开 webView
        → webJs 不生效（`AnalyzeUrl.kt:441` 的 `if (this.useWebView && useWebView)`，
          以及 `:457,467` 的 `javaScript = webJs ?: jsStr`）
      - `bookSourceType == 4` → Legado 无此取值（`BookSourceType.kt:8-11` 的 `@IntDef` 只有 0~3）
    """
```

> **不做的一条**：原计划还检查「`ruleContent.image` 非空」以提示该字段 Legado 不认。已砍——我们自己的源里这个字段**恒为空**（无任何生产者，见 1.4 与 15.1③），只有导入外部源时才可能命中，边缘到不值得一行代码。

### 6.2 形态嗅探

按扩展名与标签双通道识别，单条值只要有任一通道命中即归类：

| 形态 | 扩展名 | 标签 |
|---|---|---|
| image | `.jpg .jpeg .png .webp .avif .gif .bmp` | `<img` |
| audio | `.mp3 .m4a .aac .ogg .flac .wav .m4b` | `<audio` |
| text | 其余非空值 | — |

> **不做 `video` 形态**。Legado 的 `bookSourceType` 里没有视频（`3` 是"只提供下载服务的网站"，见 16.2），所以 `video` 形态不参与任何分派，只占一个描述位。`.mp4` / `.m3u8` 之类的值归入 `text` 即可，不影响任何判定与附注。

### 6.3 正文判据

**底线规则（决定 `verdict`）**——前置分流，**顺序不可调换**：

1. `bookSourceType == 3`（下载源）→ `unknown`，reason「文件类书源，不解析正文」。
   依据：`Debug.kt:329-332` 对文件类书源跳过解析并直接判完成；`BookSourceCheckRepository.kt:221` 同样跳过。
   （Legado 实际用的是 `book.isWebFile`，由 `downloadUrls` 存在决定（`BookExtensions.kt:94`）；本项目无该字段，退化为按类型判断。）
2. **规则（去空格后）为空** → 按类型分派，理由来自 `WebBook.kt:400-403`（空规则时不报错，**把章节链接本身当正文返回**）：
   - 文本源（0）→ **`fail`**，reason「正文规则为空；Legado 会把章节链接当作正文，无法阅读」。
   - 音频(1) / 图片(2) → **`pass`**，reason「正文规则为空；Legado 回退为使用章节链接本身，这对音频/图片源是正常配置」。
   - 未知(4) → **`unknown`**，reason「正文规则为空，且类型未知，无法判断是否符合预期」。
3. `rule_error` 非空（JS / XPath / `||` / 语法错）→ `unknown`，reason 透传规则不可回放的原因。
4. 提取值为空 → **`fail`**，reason「正文提取为空」。
   依据：`BookContent.kt:203-205` 的 `contentStr.isBlank()` → `ContentEmptyException("内容为空")`。
5. 提取值非空 → **`pass`**，reason 为空。形态记入 `evidence.shape`。
   依据：`Debug.kt` 全链路的判定只有「非空 / 未抛异常」。

> ⚠️ **第 2 步必须在第 3 步之前**。`replayer.parse_rule("")` 会把空规则标成 `unsupported="空规则"`，若先判 `rule_error`，空规则会被当成「无法回放」而不是按类型的空规则语义处理。
>
> ⚠️ **禁止**在这一层再引入任何数值阈值（正文长度、中文字符数、图片张数……）。Legado 的调试没有阈值，自创阈值会误杀短章节，与本仓库 `checker.py` 反复强调的「不误杀」原则相悖。

**启发式附注（只写 `notes`，绝不改 `verdict`）**：

| 附注 | 触发条件 |
|---|---|
| 疑似选到容器而非正文 | 提取值含结构性标签（见下） |
| 疑似错误页 | 提取值总字符 < 500 且命中噪声词（见下） |
| 正文较短 | 提取值总字符 < 500（★ 这个 500 取自 Legado 自身 `BookContent.kt:194` 的 `if (contentStr.length < 500)`，不是我们自创的） |
| 类型可能标错 | `source_type` ∈ {0,1,2} 且实测形态与之不符（如声明小说却提取出一批 `.jpg`）。提示「建议核对 bookSourceType」，**不判失败**——那大概率是类型标错，不是源坏了 |

**结构标签信号**（高精度，用于抓「捞到容器而非正文」）：

```python
STRUCT_TAG_RE = re.compile(
    r"<\s*(div|script|style|nav|header|footer|aside|table|ul|section|form)\b", re.I)
```

命中 → note「提取值含结构性 HTML 标签（<div>），疑似选到容器而非正文；建议改用 `@text`，或加 `##<[^>]+>##` 清洗」。

**噪声词**（仅当提取值总字符 < 500 时启用）：

```python
CONTENT_NOISE_MARKERS = [
    "验证码", "人机验证", "安全验证", "访问验证", "安全检测", "继续访问",
    "页面不存在", "内容不存在", "章节不存在", "已下架", "正在审核",
    "维护中", "站点维护", "请开启JavaScript", "请开启javascript",
    "访问受限", "403 Forbidden", "Access Denied", "cloudflare", "captcha",
]
```

复用 `core/models.py` 的 `ANTI_BOT_MARKERS` 作为补充，但**不复用** `LOGIN_MARKERS`（含 `login` / `sign in`，在正文里误命中概率高）。命中噪声词**只产生附注，不判 fail**——短章节里完全可能出现「404」这类字样，交给人看全文判断。

**类型不符附注**：`EXPECTED_SHAPE = {0: "text", 1: "audio", 2: "image"}`。

> 注意这里**没有 `3`**。Legado 的 `bookSourceType` 是 `0 文本 / 1 音频 / 2 图片 / 3 只提供下载服务的网站`（`BookSourceType.kt:8-11`），**`3` 不是"视频"**。本项目 `core/models.py:23` 与 `SourceEditDialog.vue:41` 都把它标成了「🎬视频」，是错的；`4 = ❓未知` 在 Legado 里更是**不存在这个取值**。这两处标注问题见 16.2，本次不改语义，但判定模块**不得**基于错误标注做分派。

若 `source_type` ∈ {0,1,2} 且实测形态与之不符（且实测形态非 `empty`）：追加 note「声明为{类型}，但提取结果像{形态}，建议核对 bookSourceType」。**不判失败**——那大概率是类型标错，不是源坏了。

**改类型的落点**：note 里附带一个「去改类型」按钮，点击后**只切到「基本信息」页签**（`activeTab = 'basic'`），不做任何写入。用户在那里用已有的类型单选改完、正常保存即可。

> 为什么不做成一键写入：一是省掉一个新接口与一条绕过保存校验的写路径（类型标签重建、未保存确认、覆盖检测都在正常保存流程里）；二是**改类型是写操作，应该走用户明确确认的保存动作**，而不是藏在调试面板的一个按钮里。切页签的成本只有一次点击，收益是这条写路径不必存在。

### 6.4 列表步骤判据

`search` / `bookUrl`：解析出 ≥1 条 → `pass`；0 条且 `rule_error` 为空 → `fail`；`rule_error` 非空 → `unknown`。

`toc`：
- `bookSourceType == 3`（下载源）→ `unknown`，reason「文件类书源不解析目录」。依据 `Debug.kt:329-332`、`BookSourceCheckRepository.kt:221`。
- 章节数 ≥ 1 → `pass`（附注：章节数 < 3 时提示「章节数偏少，目录可能分页加载」）。
- 0 章 → `fail`「目录解析为空」。依据：`TocEmptyException`（`BookSourceCheckRepository.kt:236`）。

> `search` 步例外：Legado 搜索步判定是 `exploreBooks.isNotEmpty()`（`Debug.kt:285-290`），同样是「非空即过」，无阈值。

`checker` 在 `toc` 判定为 `pass` 后，继续叠加它原有的「与 `TEST_TITLES` 参考表比例比对」逻辑；比例不达标 → `toc_complete=False`（保持现有行为与阈值 `TOC_COMPLETE_THRESHOLD`）。

> **已知且接受的差异**：「试跑」没有参考表（用户填的关键词通常不在 `TEST_TITLES` 里），所以它的 `toc` 判定只做「非空」判定。一个只有 5 章的源，试跑会说 `pass`，而批量校验拿参考表比对后可能给 `toc_complete=False`。这不是「同源不同判」的残留，而是两者**掌握的信息不同**：本次收拢的是「提取结果本身是否为空、形态是什么」这一层，完整度比对依赖外部数据，天然只属于批量校验。章节数 < 3 时的附注就是对这种信息缺口的提示。

---

## 七、`core/rules/replayer.py` 增强

### 7.1 新增 `extract_all_nodes()`

`_walk()` 内部已经算出了命中节点，只是 `extract_all_ex()` 丢弃了。新增薄封装，**不改任何现有调用点**：

```python
def extract_all_nodes(content: str, rule: str,
                      limit: int = 5, max_chars: int = 200_000
                      ) -> Tuple[List[str], List[str], str]:
    """返回值列表 + 命中节点 outerHTML 列表 + 失败原因。

    命中节点在「attr 取值动作」执行前捕获——那正是"规则选中的 DOM 块"。
    """
```

实现方式：抽出内部 `_walk_hits(start_nodes, steps, kind)`（返回 `nodes, values, err, hits`），把现有 `_walk()` 改成它的薄封装（`_walk(...)[:3]`），**零调用点改动**。

截断策略：最多 `limit` 个节点，每个 `max_chars` 字符。

### 7.2 补齐 `parse_rule` 的不支持语法检测

**这是一条会让试跑误报 `fail` 的路径，必须修。**

`parse_rule` 目前只把 **JS / XPath / `||`** 标为 `unsupported`（`replayer.py:250-258`）。而 Legado 还支持一批我们回放不了的语法，它们现在**会被静默当成 CSS 选择器跑出空结果**，于是试跑报 `fail`「解析为空」——把「工具测不了」误判成「源坏了」，正是最严重的那一档。

需要补进 `unsupported` 检测的清单（依据 `AnalyzeRule.kt:600-634`、`AnalyzeByJSoup.kt:104-123,406-510`、`RuleAnalyzer.kt:176-199`）：

| 语法 | Legado 语义 | 我们 |
|---|---|---|
| `@@` 前缀 | 强制走 jsoup（默认模式） | 不支持 → `unknown` |
| `@webjs:` 前缀 | 注入 WebView 执行 JS | 不支持 → `unknown` |
| `&&` | 合并多个规则的结果 | 不支持 → `unknown` |
| `%%` | 按索引交替取多个规则的结果 | 不支持 → `unknown` |
| `[2:5]` / `[0:10:2]` / `[!0:2]` 区间索引 | start:end:step 切片，可负可反转 | 不支持 → `unknown` |
| `$1`（`##` 之前） | 取上一步结果列表的第 n 项（`AnalyzeRule.kt:949`） | 不支持 → `unknown` |
| `##正则##替换###`（四段） | 只替换第一个匹配（`AnalyzeRule.kt:487-497`） | 未实现 → 归 `unknown` |
| `@get:{name}` / `@put:{...}` | 变量读写 | 不支持 → `unknown` |

> 注意 `||` 的归类**已经是对的**：Legado 支持它，我们离线回放不了，所以归 `unknown` 而非 `fail`（`replayer.py:256-258`）。本次只是把同一原则贯彻到其余语法。

### 7.3 末段语义：列表规则 vs 取值规则（本次只记录 + 加注释，不改行为）

Legado 对两类规则的末段处理**完全不同**（`AnalyzeByJSoup.kt`）：

| 用途 | 末段含义 |
|---|---|
| 列表规则（`bookList` / `chapterList`） | **全是选择器**（`:140-180` 的 `getElements`） |
| 取值规则（`name` / `bookUrl` / `content`） | **末段是动作或属性名**（`:200-225` 的 `getResultLast`，`else -> element.attr(lastRule)`） |

我们的 `parse_rule` 对两者共用同一套 `_parse_css_steps`，靠 `_looks_like_attr()` 启发式猜（`replayer.py:166`）。**绝大多数写法碰巧一致**（`@text` / `@href` 被正确识别为属性；`chapterList: "class.chapter@tag.a"` 是列表规则、两段都是选择器）。

唯一分叉是：**取值规则的末段写成裸选择器**（如 `content: "id.content"`）。此时我们当选择器并取 text → 给出正文、判通过；Legado 走 `attr("id.content")` → 返回空 → 抛 `ContentEmptyException`。**这是"误放"方向。**

本次**不改**这个行为：改严会让一批现在通过、且在真人使用中"看起来能用"的源集体判失败，需要独立的评估与灰度。本次只在 `replayer` 里加注释记录该差异，并在 `parse_rule` 的返回值上标注该规则属于哪一类（列表/取值），为将来的修正留出结构。

### 7.4 保持不变

`extract_all` / `extract_all_ex` / `parse_list` / `parse_field` 的签名与语义全部不动。7.2 的改动只扩展 `unsupported` 的**检测范围**，不改变任何原本能跑通的规则的执行结果——检测命中的都是**现在跑不出来的**语法。

---

## 八、`core/fetch.py` 增强

```python
def fetch(url: str, timeout: int = 15,
          headers: Optional[Dict[str, str]] = None, charset: str = "",
          proxy: str = "") -> str
def parse_source_header(raw: str) -> Tuple[Dict[str, str], str]
    """解析 Legado 书源的 header 字段，返回 (请求头, 不可用原因)。"""
```

`proxy` 为空串时走直连（现有行为）。非空时用 `urllib` 的 `ProxyHandler` + `build_opener` 走代理。
**这是让试跑结果可信的必要条件之一**：`checker` 一直支持 `proxy`，而 `verify` 不支持——同一个需要代理的源，批量校验能过、试跑却显示连接失败，用户只会判「源坏了」。

`parse_source_header` 支持 Legado 的两种写法：

- JSON：`{"User-Agent":"...","Referer":"..."}`
- 换行分隔：`User-Agent: xxx\nReferer: yyy`

含 JS（`<js` / `@js:`）→ 返回空头 + 原因「header 含 JS 规则，无法离线应用」，调用方记入 `notes`，**不因此判失败**。

> Legado 的 `BaseSource.kt:102-124` 是支持 `@js:` / `<js>` 的（在 App 里有 Rhino 引擎跑），我们离线做不到，所以只能标为附注。
> `AnalyzeUrl.kt:130-141` 在缺 UA 时会补 `DEFAULT_UA`，本实现保持同样行为。

`charset` 优先用于解码，失败按现有顺序回退（utf-8 → gbk → gb2312 → big5）。

调用方 `core/analyzer.py`、`services/add_source.py` 使用默认参数，无破坏。

> **未纳入本次（明确记录）**：Legado 调试还会带上 cookie（`AnalyzeUrl.kt:706-727`，数据库 cookie ⊕ urlOption cookie，按 `enabledCookieJar` 注入）。本项目无 cookie 存储，本次不实现，因此**带登录态的源在试跑中仍会看到登录页**——这一现象会在 UI 的 `notes` 里提示，避免用户误判为源坏了。

---

## 九、`core/verify.py` 改造

`verify_chain()` 保持函数签名与返回值顶层结构（`steps` / `all_ok`）不变，内部改为：

1. 每一步记录 `url` / `status` / `elapsed_ms`。
2. 抓到的页面写入 `pages[]`（按 `page_id` 去重），步骤持 `page_id`。
3. 解析改用 `extract_all_nodes()`，拿到 `values` + `hits` + `rule_error`。
4. 判定改为调 `quality.judge_content()` / `quality.judge_list_step()`。
5. `ok` 由 `Judgement.ok` 映射（仅 `fail` → `False`），`all_ok = all(steps.ok)`。
6. 把 `quality.static_misconfig_notes(source)` 的结果挂到相关步骤的 `notes` 上（`webJs` 类错配挂 content 步，`bookSourceType == 4` 挂 search 步）。
7. **新增 `book.tocUrl` 优先分支**：`ruleBookInfo.tocUrl` 非空时跳过详情页解析，直接用该 URL 抓目录页。对齐 `Debug.kt:318-322`。
8. **新增 `file` 型跳过**：`bookSourceType == 3` 时 toc 步判 `unknown` 并给出对应 reason，不再走章节解析。
9. **透传 `proxy`**：`verify_chain(..., proxy="")` 参数，传给 `fetch`。

> ⚠️ **一个必须写明的交互**：`file` 型（3）的源，`toc` 与 `content` 两步都是 `unknown`。而 `ok` 的映射是「仅 `fail` → `False`」，所以 **`all_ok` 会是 `true`**。`core/repair/loop.py:184` 正是用 `if v.get("all_ok")` 判「验证通过」的——**AI 修复循环会认为这类源验证通过而停止修复**。
> 这可能正是期望行为（下载源本来就不该有目录/正文），但它是**本次改动引入的行为**，不能让它成为意外。若不符合预期，需要把 `all_ok` 改成「无 `fail` **且** 无 `unknown`」——**但那会同时改变其它消费方的语义，需单独评估，本次不改。**

`backend/api/rules.py` 只做透传，几乎不改。`core/repair/loop.py` **无需改动**。
`backend/api/ops.py` 需一处改动：在把试跑结果放进 job 结果前**剥掉 `pages` / `matched_html` / `values`**（见五、`快速生成任务必须剥离证据字段`）。

---

## 十、前端：`RuleDebugDrawer.vue`（新增）

用 `el-drawer`（沿用项目已有的 Drawer 模式：`GroupManageDrawer` / `LLMSettingsDrawer` / `ExportDrawer`）。

- **Props**：`modelValue`、`result`（完整试跑结果）、`initialStep`。
- **顶部**：四个步骤 Tab（`search` / `bookUrl` / `toc` / `content`），每个带三色圆点（红/灰/黄/绿，见第五节）。
- **每步概要**：URL、HTTP 状态、耗时、verdict 徽章、reason、notes 列表、`rule_error`（非空时醒目显示）。
- **三个子 Tab**：
  - **提取结果**：`values` **全文**（对齐 `BookContent.kt:194-205` 的「正文长度或全文」——这正是本次要补的核心）；`evidence` 摘要（条数 / 字符 / 中文字符 / 块级分隔 / 标签占比 / 噪声命中）。
  - **命中源码**：命中节点 outerHTML，等宽字体、可复制。这是「改规则时你看的是现在选到了哪块 DOM」的载体。
  - **整页源码**：对应 `pages[]` 条目；带搜索框、命中高亮、上一条/下一条跳转、`第 N 处 / 共 M 处` 计数；`truncated` 为真时顶部提示「仅显示前 N 字符（原文 M 字符）」。

> 与 Legado 的差异（有意为之）：Legado 的调试是**流式日志**（`Event` + `[mm:ss.SSS]` 前缀 + `+%.3fs` 相对耗时，纯文本，MarkdownSheet 全量查看），没有分步卡片，也没有高亮与点选。我们保留分步 Tab 的结构，因为右侧栏空间有限、分步更利于定位；但**不引入高亮与元素点选**，与 Legado 的取舍一致。

**已识别的实现风险 — 大 HTML 渲染**：
整页 HTML 可能 100 万字符且无换行（压缩输出），直接进 `v-html` 会卡死。

**方案（刻意选最省的一条）**：不做虚拟滚动，靠"DOM 里永远只有少量节点"来规避：

1. 后端在采集阶段做一次**仅用于展示的轻量换行**（在标签边界插入 `\n`，不改变语义、不影响解析结果），把长行拆开。
2. 前端源码视图**默认只渲染前 N 行**（N ≈ 2000），底部给「加载更多」按钮。
3. 搜索在**原始字符串**上做（用 `indexOf` 扫描，结果不进 DOM），只把每个命中位置的**上下文几行**渲染出来，配「上一条 / 下一条」跳转与 `第 N 处 / 共 M 处` 计数。
4. **不做全文高亮、不做虚拟滚动**——加了这两个，实现量翻倍而收益有限。

> 这个取舍的依据：用户看源码的真实动作是"搜一个 class 名、看它周围的写法"，不是"通读 100 万字符"。把渲染量压在几百行，比实现一套虚拟滚动划算得多。

---

## 十一、前端：编辑弹窗交互加固

### P0-① 未保存确认

- 打开弹窗 / 保存成功 / 应用生成时记录 `savedSnapshot = JSON.stringify(form.value)`。
- 加 `:close-on-click-modal="false"`。
- `before-close` 钩子：`form` 变动且与快照不一致时 `ElMessageBox.confirm` 二次确认。

### P0-② 覆盖已有源确认

- `backend/api/sources.py` 新增 `GET /api/sources/exists?url=` → `{"exists": bool, "name": str}`。
  比复用 `/sources/detail` 再解析 `404` 错误串更稳。
- 新建模式 `save()` 前调用；命中则确认「域名已存在（源名：xxx），继续将覆盖其全部规则」。
- 编辑模式域名 `disabled`，天然不受影响。

### P0-③ 原始 JSON 快照陈旧

**只有两个状态**，不做字符串 diff：

- `rawDirty`：用户在「原始 JSON」文本域里手改过（`@input` 置真，`applyRawJson` 成功后清零）。
- `watch(form, { deep: true })` → `rawDirty` 为假时**自动重新生成快照**（`syncRawFromForm()`）。

这样 `rawJsonText` 在用户没动过它的情况下**永远是新鲜的**，点「应用到表单」最多是无操作，不可能回滚。

`rawDirty` 为真时（用户正在手改 JSON）：顶部黄色提示条「表单已修改，此处是你手改的内容，点『从表单生成』会覆盖」+ 点「应用到表单」前二次确认。

> 比原方案省掉的：`rawSynced` 快照串、`rawStale` 的 `JSON.stringify` 比较、以及「切页签时才同步」这个延迟动作。原方案要在三个状态间协调，现在只需要问一个问题——「用户动过这个文本域吗」。

### P1-④ 试跑结果过期

- `watch(form, { deep: true })` → 有改动即置 `testStale = true`（`testStale` 仅在 `testResult` 非空时有意义）。
- 摘要卡片降级为灰态 + 「规则已改动，结果已过期，请重跑」；抽屉里的旧结果同样标注。
- 仅 `testRun()` 成功后清除 `testStale`。`applyRawJson()` 会替换整个 `form`，由上面的 watcher 自然置为过期——**不要**在这里清除，否则又变成「结果替一个已经不成立的规则背书」。

### P1-⑤ 基本信息页签圆点

`tabDot('basic')` 改为反映必填校验：名称或域名为空 → `err`；否则 `ok`。

### P1-⑥ 另存为新源

- 组件内新增 `isDuplicate` 状态。`isNew = computed(() => isDuplicate.value || !props.sourceUrl)`。
- 编辑模式标题旁增加「另存为新源」按钮：置 `isDuplicate = true`、清空 `form.bookSourceUrl`、解除域名 `disabled`、标题改为「另存为新源」。
- 保存走新建分支（含 P0-② 的重复检测）。

### 前端三态 + 附注渲染

- `SourceEditDialog.vue`：步骤行由 `s.ok ? 'success' : 'danger'` 改为按 `verdict` + `has_notes` 映射（`fail→danger` / `unknown→info` / `pass & has_notes→warning` / `pass→success`）。
- 「全部通过」改为 `steps.every(s => s.verdict === 'pass')`。**有附注的 pass 仍算通过**。
- `tabDot('rules')`：存在 `fail` → `err`；存在 `has_notes` → `warn`。
- `expandTestFailures` 增加「有 `fail` 或 `has_notes`」也触发切页签。
- 快速生成卡片（`quickVerify`）只做摘要渲染，**不接入调试抽屉**——它的结果已被 `ops.py` 剥掉 `pages` / `matched_html` / `values`（见五），且卡片位于「快速生成」页签内，空间与用途都不适合展示源码。调试抽屉只从「全链路试跑」进入。

- **提示**：`SourceEditDialog.vue:139` 的 `expandTestFailures` 与 `tabDot` 目前都基于 `s.ok`，改造后统一基于 `s.verdict` / `s.has_notes`，避免两套判定并存。

---

## 十二、文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `core/quality.py` | 新增 | 三态判定器、形态嗅探、启发式附注、静态错配检查 |
| `tests/test_quality.py` | 新增 | 判定矩阵单测 |
| `tests/test_verify_chain.py` | 新增 | 证据结构与兼容性单测 |
| `frontend/src/components/RuleDebugDrawer.vue` | 新增 | 调试抽屉 |
| `core/rules/replayer.py` | 改 | 新增 `extract_all_nodes()`；抽 `_walk_hits()`；补齐 `parse_rule` 不支持语法检测（7.2） |
| `core/reclassify.py` | **不动** | 类型判定改造整体推迟（十五） |
| `core/fetch.py` | 改 | `fetch()` 支持 headers / charset / **proxy**；新增 `parse_source_header()` |
| `core/verify.py` | 改 | 证据采集 + 调 `quality` 判定 + `tocUrl` 分支 + `file` 型分支 + `proxy` 透传 |
| `core/checker.py` | 改 | `_probe_content` / `_probe_toc` 判定改调 `quality`；删除 `ruleContent.image` 死分支；**`CACHE_VERSION` 5 → 6** |
| `backend/api/sources.py` | 改 | 仅新增 `GET /api/sources/exists` |
| `backend/api/rules.py` | 改 | 透传适配（薄） |
| `backend/api/ops.py` | 改 | 一处：入 job 结果前剥离 `pages` / `matched_html` / `values`（五） |
| `frontend/src/api/sources.js` | 改 | 仅新增 `sourceExists()` |
| `frontend/src/components/SourceEditDialog.vue` | 改 | 三态渲染 + 抽屉接入 + P0/P1 六项 |
| `frontend/src/styles.css` | 改 | 抽屉与圆点样式 |

不改动：`backend/schemas.py`、`core/repair/loop.py`、`core/reclassify.py`（类型判定推迟，见十五）、`services/add_source.py`（`verify_chain` 签名向后兼容，`proxy` 有默认值）。

---

## 十三、测试与验收

### 13.1 端到端人工验收清单

单元测试不能替代"看一眼"。以下每条都应在真实源上跑一遍，**每条都要能明确说"过了/没过"**：

**试跑与判定（批次 A + B）**

| # | 操作 | 预期 |
|---|---|---|
| 1 | 拿一个**已知可用的漫画源**点全链路试跑 | content 步 **绿**（`pass`）；「提取结果」里能看到图片 URL 列表；「命中源码」里能看到选中的 DOM 块 |
| 2 | 拿一个 **content 规则为空、未配 webView 的小说源** | content 步**红**，reason 明确写出「Legado 会把章节链接当作正文」 |
| 3 | 拿一个 **content 规则含 `<js>` 的源** | content 步**灰**（`unknown`），reason 写出「JS 规则…无法离线回放」，**不是红** |
| 4 | 拿一个正文只有几十字的源 | content 步**绿 + 黄点**（`pass` + `has_notes`），「提取结果」里能看到那几十个字 |
| 5 | 任意源跑完试跑，**改一个规则字段** | 结果卡片立刻变灰「规则已改动，结果已过期」；抽屉里的旧结果同样标注 |
| 6 | 点开「整页源码」，搜一个页面里存在的 class 名 | 能跳到命中处、显示计数；**页面不卡**（这条专门验 10.4 的渲染取舍） |
| 7 | 找一个**需要代理**的源 | 能配上代理跑通（而不是连接失败） |
| 8 | 找一个**必须带 Referer 才出正文**的源 | 试跑能抓到正文（验 header 透传真的生效了） |

**编辑弹窗（批次 B）**

| # | 操作 | 预期 |
|---|---|---|
| 9 | 改几条规则后**点遮罩空白处** | 弹确认框，取消后规则还在 |
| 10 | 新建书源，**填一个已存在的域名** | 弹「域名已存在（源名：xxx），继续将覆盖其全部规则」 |
| 11 | 改完表单 → 切「原始 JSON」→ 切回表单再改 → 再切「原始 JSON」 | 看到的是**最新表单**，不是旧快照；手改过 raw 时有黄色提示条 |
| 12 | 编辑模式点「另存为新源」 | 域名解禁、标题变为「另存为新源」、保存后成为独立的新源，原源不被覆盖 |

**类型提示（批次 B）**

| # | 操作 | 预期 |
|---|---|---|
| 13 | 拿一个**声明为小说、实际是漫画**的源试跑 | content 步出现 note「实测为图片，与声明的 📖小说 不一致」+「去改类型」按钮 |
| 14 | 点「去改类型」 | 只切到「基本信息」页签，**不发生任何写入**；用户手动改类型并保存后，类型标签同步重建 |

**批次 A 的星级迁移验收**见 14.2，是独立的、**必做**的一步。

### 13.2 单元测试

`tests/test_quality.py`（`unittest` 风格，与 `tests/test_legado_rules.py` 一致）：

- `sniff_shape`：纯文本 / 图片 URL / 音频直链 / 混合 / 空。（无 `video` 形态，见 6.2）
- `judge_content` 判定矩阵（**每条都要断言 `verdict` 与 `notes` 分开**）：
  - 正文非空 → `pass`（无论长短——**必须包含一个"极短正文"用例断言它是 `pass` + 附注，而不是 `fail`**，这是本次回归防误杀的关键用例）
  - 正文为空 → `fail`（对应 `ContentEmptyException`）
  - `bookSourceType == 0` 且规则为空 → `fail`
  - `bookSourceType == 1` 且规则为空 → `pass` + reason 含「章节链接」
  - `bookSourceType == 2` 且规则为空 → `pass` + reason 含「章节链接」
  - `bookSourceType == 3` → `unknown`（文件类跳过）
  - `bookSourceType == 4` 且规则为空 → `unknown`
  - `rule_error` 非空（JS/XPath/`||`）→ `unknown`
  - 提取值含 `<div>` → `verdict == "pass"` 且 `notes` 非空
  - 提取值总字符 < 500 且命中「页面不存在」→ `verdict == "pass"` 且 `notes` 非空（**不判 fail**）
  - 声明小说、实测图片 → `verdict == "pass"` 且 note 含「建议核对 bookSourceType」
  - 音频源提取 `.mp3` → `pass`
- `judge_list_step`：目录 0 章 → `fail`；1 章 → `pass` + note；`bookSourceType == 3` → `unknown`。
- `static_misconfig_notes`：`ruleContent.webJs` 非空但 URL 规则无 webView → 有 note；`bookSourceType == 4` → 有 note。
- `Judgement.checker_state` 三态映射：`pass→True` / `fail→False` / `unknown→None`。
- **反向断言**：判定模块里不存在任何基于长度的 `fail`——用一个 20 字的合法短正文用例守住这条底线。

`tests/test_verify_chain.py`：

- `unittest.mock.patch("core.verify.fetch")` 注入固定 HTML。
- 断言 `steps` 含 `verdict` / `has_notes` / `notes` / `page_id` / `evidence` / `values` / `url` / `status` / `elapsed_ms`。
- 断言 `ok` 兼容性：`fail` → `False`，`pass/unknown` → `True`。
- 断言 `all_ok` 仅在有 `fail` 时为 `False`（有 `notes` 的 `pass` 不影响 `all_ok`）。
- 断言 `pages` 去重（搜索页只出现一次，被 `search` 与 `bookUrl` 两步引用）。
- 断言超长 HTML 触发 `truncated=true`，且 `MAX_EVIDENCE_TOTAL_CHARS` 兜底生效并在 `notes` 里标注。
- 断言 `ruleBookInfo.tocUrl` 非空时跳过详情页（不产生 `detail` 页、`toc` 步直接抓 `tocUrl`）。

`tests/test_legado_rules.py` 补充（7.2 的不支持语法检测）：

- 断言 `@@`、`@webjs:`、`&&`、`%%`、`[2:5]`、`$1`、`##a##b###`（四段）、`@get:{x}` 均返回非空 `unsupported` 原因。
- **反向断言**：`@text` / `@href` / `class.a@tag.b@text` / `$.a.b` / `##正则##替换`（两段）仍照常跑通，不被新检测误伤。

---

## 十四、实施顺序与回退

### 14.1 两个批次

原计划是三批（含类型判定改造），**类型判定已整体推迟**（见十五），所以现在只剩两批：

#### 批次 A —— 后端判定与证据（无依赖）

1. `core/quality.py` + `tests/test_quality.py`
2. `core/rules/replayer.py`：`extract_all_nodes()` + `parse_rule` 不支持语法检测（7.2）+ `tests/test_legado_rules.py` 补充
3. `core/fetch.py`：headers / charset / proxy（八）
4. `core/verify.py`：证据采集 + `tocUrl` / `file` 分支 + `proxy` 透传（九）
5. `backend/api/rules.py` 透传适配
6. `core/checker.py`：判定收拢 + 删除 `ruleContent.image` 死分支 + **`CACHE_VERSION` 升到 6**
7. `backend/api/ops.py`：剥离证据字段（五）

#### 批次 B —— 前端（依赖 A）

8. `frontend/src/components/RuleDebugDrawer.vue`
9. `SourceEditDialog.vue`：三态渲染 + 结果过期 + 抽屉接入 + 类型不符 note 的「去改类型」切页签
10. `SourceEditDialog.vue`：P0/P1 六项交互加固
11. `backend/api/sources.py` 的 `/exists` + `frontend/src/api/sources.js` 的 `sourceExists()` + `styles.css`

### 14.2 批次 A 的验收（**必做，缺了就只能靠感觉说"改好了"**）

`checker` 判定收拢**必然造成星级迁移**，因为现在漫画源的 `content_ok` 实际走的是「文本长度 > 100」（图片兜底是死代码，见 1.4 与 15.1③）。收拢后空规则对图片源返回 `pass`，`content_ok` 从 `False` 变为 `True`/`None`，而 `calc_stars` 在 `None` 时回退静态判定 → 星级会变。

**步骤**：

1. **收拢前**：导出基线快照——全部源的 `quality_stars` / `toc_complete` / `content_ok` / `health`（走 `store` 的 checks 表即可，不必联网）。
2. 实施收拢 + 升 `CACHE_VERSION = 6`。
3. 触发一次全量重校验（**主动安排时间窗**：3700 源，耗时长且对源站有请求量）。
4. **diff 两份快照**，逐项确认每一处变化都落在"预期内的修正"里：

| 预期变化 | 方向 |
|---|---|
| 漫画源（实测正文为图片）的 `content_ok` 由 `False` → `True`/`None` | 修正误杀 |
| 空规则 + 无 webView 的文本源 `content_ok` 由 `True` → `False` | 修正误放 |
| 含 JS/XPath/`\|\|` 等语法的源由 `False` → `None`（`unknown`） | 修正误判（工具的能力边界） |

5. 出现**上表之外**的变化 → 停下排查，不要刷新缓存。

### 14.3 回退策略

- 两个批次互相独立，任一可单独 revert。
- **批次 A 里唯一有回归风险的是 `checker` 收拢**（影响 3700 源批量校验）：它独立成一个提交，便于单独 revert。回退后 `verify` 与 `checker` 恢复「同源不同判」，但试跑调试功能不受影响。**注意：revert 时若已升 `CACHE_VERSION`，需一并回退版本号，否则缓存会被反复作废。**
- 本次**不涉及任何批量写源数据的操作**（类型判定改造已推迟），因此回退不需要数据备份。

---

## 十五、书源类型判定：本次只给提示，不改判定逻辑

**结论先说**：类型判定的改造**整体推迟**。本次只在试跑里加一条提示，让人自己去改。

这样做减法的依据是：本次的核心诉求是「判断 content 是否合法」和「看源码辅助改源」，而类型判定与这两件事**没有任何关系**。它的收益是间接的（App 里的分类标签更准），成本却是本次唯一需要 `--write` 写数据、唯一需要独立测试文件、唯一需要 `--report` 对比 + 备份回滚的一块。

### 15.1 现状的问题（推迟的依据，不是本次要做的事）

| # | 问题 | 位置 |
|---|---|---|
| ① | **数学上判不出 1（音频）和 3（下载源）**——`infer_type_static` 只有 `2` / `0` / `-1` 三个出口 | `core/reclassify.py:121-127` |
| ② | 判据系统性偏「小说」：`NOVEL_HOST_HINTS` 含裸子串 `"xs"`（两字母）、`NOVEL_TEXT_HINTS` 含 `"阅读"`/`"txt"`（中文小说站几乎必中），而 `MANGA_HOST_HINTS` 全是高度特异词 | `core/reclassify.py:41-50` |
| ③ | **最强的漫画信号是死代码**：`image_rule = content.get("image","")` 恒为空 → `if image_rule.strip(): manga += 3`（`+3` 单独就能越过阈值 3 定案）从未执行；`:112` 的小说条件也因此蜕化 | `core/reclassify.py:80,94-96,112` |
| ④ | 已声明的 1 / 3 被完全无视——只处理 `declared == 2` 和 `declared == 0` | `core/reclassify.py:116-120` |
| ⑤ | `-1`（证据不足）的语义是「保持原样」，而多数源本来就是默认 `0` → **"保持原样"就是保持错误**；且没有任何路径能产出 `4`（❓未知） | `core/reclassify.py:127` |
| ⑥ | 根本问题：`bookSourceType` 决定的是**阅读器走哪条渲染路径**（`BookSourceType.kt` 的 `@IntDef` + `BookSourceExtensions.getBookType()`），而现有判据全是「域名/名称像什么」——**用内容线索猜渲染路径** | `core/reclassify.py:34-50` |

### 15.2 本次做什么（只有一件事）

`quality.judge_content()` 在实测形态与声明类型不符时产出一条 note（见 6.3），note 上附「去改类型」按钮，点击**只切到「基本信息」页签**。

用户在那里用已有的类型单选改完、走正常保存流程。**不新增接口、不新增写路径、不动 `reclassify.py`。**

### 15.3 推迟的部分（连同依据一起记入二期，不能丢）

| # | 推迟项 | 依据 |
|---|---|---|
| 1 | 修 `reclassify.py` 的死分支（`image_rule`，`+3` 信号从未生效） | 15.1 ③ |
| 2 | 收窄 `"xs"` / `"阅读"` / `"txt"` 等泛词（判据系统性偏小说） | 15.1 ② |
| 3 | 补齐音频 / 下载源的静态出口（`infer_type_static` 只有 `2`/`0`/`-1`） | 15.1 ①④⑤ |
| 4 | 判据从「域名/名称」改为「**规则形状**」 | 15.1 ⑥ |

第 4 项是真正的解法，对齐 Legado 的思路——`Debug.kt:329-332` 用 `book.isWebFile`（由 `downloadUrls` 存在决定，`BookExtensions.kt:94`）判下载源，**不是猜的**。`reclassify --write` 在那之前仍只能在 0 / 2 之间翻转。

### 15.4 连带影响（推迟的代价）

`organizer.group_title`（`:55`）与 `store._system_group_for`（`:237`）都用 `BOOK_SOURCE_TYPE_NAMES` 生成类型标签。所以**类型判定不改，App 里的类型分类就会一直带着 15.1 ②③ 的偏差**。

本次提供的是「**人主动纠正单条源**」的通道（15.2），不是批量修正。这是**已知且接受**的代价。

---

## 十六、与「阅读 App」的对齐清单

### 16.1 本次对齐的行为

| # | 行为 | Legado 依据 | 落地位置 |
|---|---|---|---|
| 1 | 判定底线是「非空 / 不报错」，无数值阈值 | `Debug.kt:285-290`、`BookContent.kt:203-205` | `quality.judge_*` |
| 2 | 证据含提取值全文 | `BookContent.kt:194-205` | `verify` + 抽屉「提取结果」 |
| 3 | 每步记录请求 URL 与相对耗时 | `Debug.kt:35,62-70`、`BookInfo.kt:39` 等 | `steps[].url` / `elapsed_ms` |
| 4 | 展示每步的原始响应体 | `BookList/BookInfo/BookChapterList/BookContent.kt` 的 `log(body)` | 抽屉「整页源码」 |
| 5 | 逐字段值成对展示（`┌获取X` / `└值`） | `BookInfo.kt:52-175` | 抽屉「提取结果」 |
| 6 | 搜索/发现成功后自动串联到下游 | `Debug.kt:285-288,303-306` | 现有行为，保持 |
| 7 | `tocUrl` 非空则跳过详情页 | `Debug.kt:318-322` | `verify` 新增分支 |
| 8 | `file` 型跳过目录解析 | `Debug.kt:329-332`、`BookSourceCheckRepository.kt:221` | `judge_list_step` |
| 9 | 请求带书源 header / UA 补全 | `AnalyzeUrl.kt:130-141`、`BaseSource.kt:102-124` | `core/fetch.py` |
| 10 | 窄通道下丢弃响应体 | `BookSourceDebugWebSocket.kt:43` | `ops.py` 剥离证据字段（五） |
| 11 | 解析为空 → 分类标签（正文失效/目录失效） | `BookSourceCheckRepository.kt:234-238` | `checker` 沿用 |

**有意偏离**：

| # | 偏离 | 理由 |
|---|---|---|
| 1 | `unknown` 灰态 | Legado 把「解析为空」与「请求失败」都归为 Error（`Debug.kt:290/294`），也把「静态无法回放的 JS 规则」当普通失败。我们用工具的能力边界判源的好坏是不对的 |
| 2 | 启发式附注 | Legado 调试完全没有提示；但我们有「提示而非判决」的余量，且明确不让它改变结论 |
| 3 | 分步 Tab 而非流式日志 | 右侧栏空间有限，分步利于定位。Legado 是 `[mm:ss.SSS]` 流式日志 |
| 4 | 不做元素点选 / 源码高亮 | 与 Legado 一致（全库无 elementPicker / DOM→选择器实现） |
| 5 | 不实现 cookie 注入 | 本项目无 cookie 存储；带登录态的源会在 `notes` 里提示「可能因未携带 Cookie 而看到登录页」 |
| 6 | 不做 Explore 调试步骤 | 见第三节非目标 |

### 16.2 顺带发现的问题（本次**不修**，仅记录）

| # | 问题 | 位置 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | 读取了 Legado 不存在的 `ruleContent.image` 字段 | `core/checker.py:761`、`core/reclassify.py:80,94,96` | `image_rule` 恒为空 → `checker.py:793` 的图片兜底**是死代码**；漫画源正文只走文本长度路径，两头都不准（多图源误放、少图源误杀） | `checker` 侧本次随判定收拢一并删除（在范围内）；`reclassify.py` 属范围外，建议单独清理 |
| 2 | `ruleContent.webView` 不是 `ContentRule` 的字段 | `SourceEditDialog.vue:65,483-485` | 前端提供的 webView 开关导出后**不会被 App 读取**（legado 的 `webView` 是 URL 规则选项） | 建议改为在 `searchUrl`/`tocUrl` 的 URL 选项里配置；需单独排期 |
| 3 | `ruleContent.webJs` 仅在 URL 规则开 webView 时生效 | `AnalyzeUrl.kt:441,457,467` | 只配 webJs 而 URL 规则没开 webView → **webJs 被静默忽略** | 本次以 `static_misconfig_notes` 给出提示，不改字段 |
| 4 | `bookSourceType` 的 3 与 4 标注错误 | `core/models.py:23-24`、`SourceEditDialog.vue:41` | 3 被标成「🎬视频」（Legado 是"只提供下载服务的网站"）；4 是 Legado 不存在的取值，会随导出写进源 | 改动波及存储、导出与全部源，建议独立任务 |
| 5 | 试跑不带 cookie | `core/fetch.py` | 带登录态的源在试跑中会看到登录页，可能误判为源坏了 | 本次以 `notes` 提示；cookie 存储属独立任务 |
| 6 | `@html:` 前缀是**自创的** | `core/rules/replayer.py:43,261-263` | Legado 的前缀集是 `@CSS:` / `@@` / `@XPath:` / `@Json:` / `@js:` / `@webjs:`（`AnalyzeRule.kt:600-634`），**没有 `@html:`**（`@html` 只是取值动作）。我们把"返回整页响应体"当成一条合法规则，实际 App 里跑不出东西 → **误放** | 独立评估后移除或降级为 `unknown` |
| 7 | `##` 的第四段形式未实现 | `core/rules/replayer.py:229-233,453-469` | `##正则##替换###` 在 Legado 里表示**只替换第一个匹配**（`AnalyzeRule.kt:487-497`）；我们只切三段，`###` 会被当成替换串的一部分 → 正则结果与 App 不一致 | 本次归入 `unsupported`（7.2），不实现 |
| 8 | 取值规则与列表规则的**末段语义**不同 | `core/rules/replayer.py:166` | 见 7.3——`content: "id.content"` 这类"末段写裸选择器"的取值规则，我们给出正文、Legado 返回空（`AnalyzeByJSoup.kt:200-225`）→ **误放** | 本次只记录并留结构，不改行为 |

---

## 十七、二期候选项

> 本节是**明确记录**的待办，不是"没想过"的东西。本次主动砍掉的项都在这里，连同砍掉的理由，避免下次翻 spec 时误以为从未考虑。

### 17.1 本次**主动砍掉**的（附理由）

| 项 | 砍掉的理由 | 详细依据 |
|---|---|---|
| **书源类型判定的全部改造** | 与「判断 content 是否合法 + 看源码」两条核心诉求**没有任何关系**；且是本次唯一需要 `--write` 写数据、需要独立测试文件、需要 `--report` 对比 + 备份回滚的一块 | 15.1 / 15.3 |
| **一键改类型（写入接口 + 按钮）** | note 已告诉用户改成什么，而基本信息页签本来就有类型单选。为省一次点击新增一条**绕过保存校验的写路径**不划算 | 6.3 / 15.2 |
| **`evidence_level` 双模式** | 已被 `MAX_EVIDENCE_TOTAL_CHARS` 兜底取代；剥离动作放在真正关心体积的业务侧更合适 | 五 |
| **`video` 形态** | Legado 无视频类型（`3` 是下载源），该形态不参与任何分派 | 6.2 |
| **`ruleContent.image` 静态错配检查** | 我们自己的源里该字段恒为空，只有导入外部源才可能命中 | 6.1 |

### 17.2 原有候选项

- **试跑页面缓存复用**：后端按 URL 短期缓存页面 HTML，加「只重解析不重抓」开关，把「改一次选择器试一次」的反馈从秒级压到毫秒级。这是调试效率提升最大的一条。
- **前端暴露 `pick`**：搜索结果多条时选第 N 条重试（后端 `pick` 参数已支持；Legado 固定取第 0 条，见 `Debug.kt:285-288`）。
- **探索步骤（Explore）**：补齐 Legado 的第五个调试目标。
- **元素点选生成规则**：iframe 渲染 + 点选元素反推 Legado 规则串。
- **取值规则末段语义对齐**（7.3）：把 `content: "id.content"` 这类"末段写裸选择器"的规则判为 `unknown` 而非「有正文」——需独立评估与灰度，因为会让一批现在通过的源集体判失败。
- **移除自创的 `@html:` 前缀**（16.2 第 6 条）。
- 「快速生成」的 `quickUrl` 与规则 `searchUrl` 概念混淆、「语法速查」卡片不可折叠。

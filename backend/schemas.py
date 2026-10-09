# -*- coding: utf-8 -*-
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceOut(BaseModel):
    id: int
    source_url: str
    name: str = ""
    source_type: int = 0
    group_name: str = ""
    user_tags: str = ""
    system_tags_locked: int = 0
    enabled: int = 1
    fingerprint: str = ""
    health: Optional[str] = None
    checked_at: Optional[str] = None
    probe_depth: Optional[int] = None
    toc_complete: Optional[int] = None
    content_ok: Optional[int] = None
    search_hit: Optional[str] = None
    #: 来源阶梯（S2）：最近一批 JVM 校验的结论。state 取 ok/no_result/
    #: empty_js_shell/login_wall/timeout/error/invalid，空串 = 没跑过。hit 是**命中的条数**
    #: （命中条数，不是命中率）。
    #: 这一行的结论**是谁判的**（`checks.engine`：jvm / device）。
    #: 界面用它标出处——藏"选择"不藏"证据来源"（口径见 lessons §七十二）。
    #: **Optional**：没有 checks 行的源这一列是 NULL（同 health），
    #: 声明成 `str` 会让「从未校验过的源」把整页的出参校验带走（AGENTS #22）
    engine: Optional[str] = None
    jvm_state: str = ""
    #: 结论跑到的最深一段（search/toc/content）。列表 tooltip 按它决定展示哪几行：
    #: 「跑到搜索」的行不该显示「目录：未验证」——那是**没跑**，不是**跑了没过**
    jvm_stage: str = ""
    jvm_hit: Optional[int] = None
    jvm_toc_count: Optional[int] = None
    jvm_toc_ok: Optional[bool] = None
    jvm_content_len: Optional[int] = None
    jvm_content_ok: Optional[bool] = None
    #: 正文偏短的附注（**不改结论**）：阈值与措辞都来自 `core.quality` 那一份，
    #: 这里只是显示。空串 = 没有附注
    jvm_content_note: str = ""
    #: A3：这次跑带上了多少字符的 cookie（0 = 没带：该域没登录过，或浏览器不可用）。
    #: **它是「这次的条件」，不是源的结论**——判断「需登录」时先看它
    jvm_cookie_len: Optional[int] = None
    jvm_batch: str = ""
    #: 结论是否经浏览器渲染（S3-4）：None=未走浏览器 / True=渲染成功 / False=渲染失败
    jvm_rendered: Optional[bool] = None
    jvm_render_reason: str = ""


class SourcePage(BaseModel):
    total: int
    items: List[SourceOut]


class GroupPatch(BaseModel):
    url: str
    group: str


class TagPatch(BaseModel):
    urls: List[str] = Field(default_factory=list)
    add: List[str] = Field(default_factory=list)
    remove: List[str] = Field(default_factory=list)


class SourceDeleteIn(BaseModel):
    """批量软删除的请求体。

    urls 走 body 而不是查询串。**阈值实测**（库副本 + uvicorn）：约 1600 条 / 57KB
    通过，2000 条 / 72KB 被服务端以 400 拒绝，而全库 3850 条拼起来约 139KB——
    「全选全部」正好落在会撞上的那一档。形状与既有的 ``POST /sources/restore`` 一致。
    """
    urls: List[str] = Field(default_factory=list)
    reason: str = ""


class TagRename(BaseModel):
    old: str
    new: str


class TagMerge(BaseModel):
    sources: List[str] = Field(default_factory=list)
    target: str


class TagDelete(BaseModel):
    tag: str


class SourceSave(BaseModel):
    source: Dict[str, Any]
    user_tags: List[str] = Field(default_factory=list)
    lock_system_tags: bool = False


class ImportBody(BaseModel):
    content: str = ""
    source: str = ""
    #: 同 URL 但规则不同时怎么办（**只对"在用的那行"有意义**）：
    #:   "keep"      保留现有，导入的那份进 conflicts/ 待查（默认，安全）
    #:   "overwrite" 用导入的覆盖在用的那行（用户标签不会被清掉：
    #:               upsert 的 DO UPDATE 不含 user_tags）
    #: 注意**回收站里的同 URL 历史版本与此无关**——它们不在用，不构成冲突，
    #: 导入会直接新建一行在用的。
    conflict_strategy: str = "keep"


class JvmRunRequest(BaseModel):
    """跑一批 JVM 校验（S5-A 第二期：支持只跑选中的几条）。

    `urls` 是**书源 URL 列表**（列表页勾选的那几条，前端给的是归一化过的 `source_url`）。
    空 = 全部在用源；**给了它范围就由它的条数决定**——不会再有别的上限去截断它。

    **两侧都必须归一 URL 再比**（AGENTS #5 的坑）：前端给的是库里归一化过的值，而导出
    的是源 JSON 里的原文——不归一就一条都对不上，而且**不报错**（跑出一批空结论）。
    """

    urls: List[str] = Field(default_factory=list)
    #: 当前筛选（列表页的查询条件，形状同 `POST /api/export` 的 `filter`）。
    #: **urls 优先**：勾选是明确意图，筛选是「这一屏里的」。
    #: 有它才做得到「只重跑待验证那批」——全量一次十几分钟，而站点是按 IP 认人的。
    filter: Dict[str, Any] = Field(default_factory=dict)
    #: **本次跑批的参数**（keyword / timeout / concurrency / depth）。
    #: 与本地校验的 `check: {...}` 同形：只作用于这一次，**不写回全局设置**——
    #: 这些是"这次怎么跑"，不是"这台机器的配置"（配置只有环境目录那几项）。
    #: 每个键都过 `settings_store.coerce`，取值范围仍然只有后端那一份（AGENTS #8）。
    params: Dict[str, Any] = Field(default_factory=dict)


class JvmDebugRequest(BaseModel):
    """在本机引擎里跑一次调试（S5-A4）：不填 IP、不推送，直接出分段结果。

    `source` 里的 ``bookSourceUrl`` 有两个用途：源的身份（JVM 侧解析）与 **cookie
    注入的键**（A3）——登录态是从我们自己的浏览器 profile 按这个 URL 读出来注进去的，
    所以它不能空、也不该被规范化（与连 App 那条同一个理由：那是源的身份原文）。
    """

    source: Dict[str, Any]
    #: 调试入参两选一：``target`` + ``query``（语义化，key 由 `core.debug_keys`
    #: 拼装——App 的 key 语法只有那一份，前端不再各抄一遍）；``key``（App 语法
    #: 原文，手工覆盖口，脚本直调用）。**``key`` 非空时优先**。
    target: str = "search"
    query: str = ""
    key: str = ""
    #: 整次调试的墙钟预算（秒）。**不给就吃设置里的 ``debug.timeout``**（默认值只在
    #: `core/settings_store`，前端不再写死 60——它曾与桥的渲染上限同为 60 而互相
    #: 掐死）；显式给要落在 ``LIMITS["debug_timeout"]`` 区间内，越界 400 不静默夹
    timeout: Optional[int] = None
    #: 手工注入的一条 cookie（可选）。不给就按源 URL 从浏览器 profile 读——
    #: 登录墙的源要先在同一个 profile 里登录一次（`scripts/jvm_login.py`）
    cookie: str = ""
    #: 页面缓存策略，只管**我们补抓的那几页**（A4 还没从 JVM 取回真实 HTML）；
    #: 取值校验在路由里做（不合法要 400，不能静默退回默认）
    cache: str = "auto"


class AppDebugRequest(BaseModel):
    """连 App 调试（借阅读 App 内建的调试 WebSocket 跑一次完整链路）。

    `source` 里的 ``bookSourceUrl`` 会被后端**直接当调试 tag 用**——它必须是
    导入原文（详情接口返回的源对象就是原文，前端不用做任何转换）；
    换成列表里的 ``source_url``（规范化过）App 会查不到源而静默无响应。
    """

    source: Dict[str, Any]
    #: 与 ``JvmDebugRequest`` 同一约定：``target`` + ``query`` 语义化入参，
    #: ``key`` 是手工覆盖口（App 语法原文），**``key`` 非空时优先**。
    target: str = "search"
    query: str = ""
    key: str = ""
    host: str = ""
    #: 0 = 用默认端口（App 的 HTTP 端口 1122 + 1 = 1123）
    port: int = 0
    #: 调试前先把源推送到 App。**会改动用户 App 里的书源数据**（新增或覆盖），
    #: 所以必须由用户显式触发，不要在流程里默认打开
    push: bool = False
    #: 页面缓存策略（``core.fetch`` 的 ``CACHE_*``）。只管**我们补抓的那几页**：
    #: auto 命中就用 / only 一页都不补抓 / refresh 忽略缓存重抓。
    #: 取值校验在路由里做（不合法要 400，不能静默退回默认）
    cache: str = "auto"


class AppHostRequest(BaseModel):
    """只需要 host 的 App 侧操作（预检 / 推送）。"""

    source: Dict[str, Any]
    host: str = ""
    port: int = 0


class CandidatesRequest(BaseModel):
    """从已抓到的 HTML 里找候选规则（交互候选面板）。

    启发式的唯一一份在 `core/candidates`；前端只在换页/换步时调一次
    （不逐键），点选与逐键高亮仍在前端做。
    """

    html: str = ""
    kind: str = ""
    #: 最多返回几条；越界由 core.candidates 夹住（纯排序问题，不值得 400）
    limit: int = 6


class SuggestRuleRequest(BaseModel):
    """让 AI 给某一步提几条候选规则。

    提示词拼装用的 ``step_label`` / ``want_label`` / ``field`` 由前端给——步骤与
    字段的对应关系只在 `frontend/src/utils/ruleCandidates.js` 定义，这里不抄第二份。
    """

    html: str = ""
    step: str = ""
    rule: str = ""
    step_label: str = ""
    want_label: str = ""
    field: str = ""
    #: DOM 大纲的**起点**（Legado 规则的首段，如 `class.book-list`）。
    #: 从命中容器起而不是从 `<html>` 起：深于 6 层的容器在大纲里根本到不了，
    #: 而提示词又要求「class 必须真实存在于大纲里」。缺省时退回整篇
    focus: str = ""
    source_type: int = 0
    #: 第 1 层算出的候选规则：**先让程序拿它们挑一遍**（免费），挑不出来才问模型。
    #: 每条都是 `core.candidates` 的产物（带 `rule` / `samples` / `count`），
    #: 免费那趟拿它自己报的样本与 App 实测值比对——**不在这里重跑规则**。
    candidates: List[Dict[str, Any]] = []
    #: 这一步要什么（`link` / `media` / `text` / `list`）：决定「页面上像 App 实测值的
    #: 那些值」从哪一族里取（`core.candidates.sample_values`）
    kind: str = ""
    #: App 实测取到的值样本：既是给模型的锚点，也是「程序先挑」用来比对的基准
    app_values: List[str] = []
    #: 只跑免费的那一趟（本地挑选 + 登录墙判断），**一个模型请求都不发**
    dry_run: bool = False
    #: 源有没有声明 cookie jar——登录墙判定要用（「200 + 登录词」那一档以它为前提）
    enabled_cookie_jar: bool = False
    #: 当前规则的本地回放结论（前端算好的一句话，进提示词当「现在的表现」）
    replay_note: str = ""
    #: 前端诊断（第 0 层）那几行文字，直接透传给模型
    diagnosis: List[str] = []


class CandidateVerifyRequest(BaseModel):
    """用本机真实引擎验收一条候选规则。

    只接受完整源的内存副本和一个规则字段；接口不会保存源，也不会推送到 App。
    """

    source: Dict[str, Any]
    field: str
    rule: str
    target: str = "search"
    query: str = ""
    timeout: Optional[int] = None
    cookie: str = ""
    cache: str = "auto"


class LLMProfileIn(BaseModel):
    id: str = ""
    name: str = ""
    provider: str = "openai-compatible"
    adapter: str = "openai-compatible"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: float = 90
    max_tokens: int = 0
    extra_headers: Dict[str, Any] = Field(default_factory=dict)
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sort_order: int = 0


class LLMProfilePatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    adapter: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[float] = None
    max_tokens: Optional[int] = None
    extra_headers: Optional[Dict[str, Any]] = None
    extra_body: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None




class JvmSettingsPatch(BaseModel):
    """JVM 校验服务的**环境**设置（core.settings_store.DEFAULTS["jvm"] 里的环境键）。
    同 CheckSettingsPatch 的约定：收敛全部交给 settings_store.coerce。

    **跑批那五个参数（keyword / timeout / concurrency / depth / limit）不在这里**：
    它们由 `settings_store` 的常量定默认值与区间、由 `JVM_RUN_PARAMS` 允许本次覆盖，
    没有「改永久默认值」这个动作——所以这里不设字段。Pydantic 默认忽略未声明的键，
    提交 `{"jvm": {"keyword": …}}` 会被静默丢弃（等价于没传）；要改这次的搜索词就在
    跑批弹框里改（`CheckJvmForm`），那才是它唯一的作用面。
    """

    app_repo: Optional[str] = None
    android_sdk_dir: Optional[str] = None


class NetworkSettingsPatch(BaseModel):
    """这台机器怎么出去（`settings_store.DEFAULTS["network"]`）。

    同「校验参数」那条老约定：全部 Optional、只提交显式给出的键，**这里不做校验或收敛**
    （区间 clamp / 类型 / 非法回落一律由 ``settings_store.coerce`` 负责）——
    在本模型上再写一份 rules 就是同一口径的第二个出处，必然漂移。
    """

    proxy: Optional[str] = None


class DebugSettingsPatch(BaseModel):
    """调试设置（core.settings_store.DEFAULTS["debug"]）。收敛交给 coerce。"""

    timeout: Optional[int] = None


class SettingsPatch(BaseModel):
    """按 section 分组，与 settings_store 的文件结构一一对应（不做映射层）。"""

    network: Optional[NetworkSettingsPatch] = None
    jvm: Optional[JvmSettingsPatch] = None
    debug: Optional[DebugSettingsPatch] = None


class JobCreate(BaseModel):
    kind: str = Field(description="任务类型：check / add / jvm_run")
    payload: Dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: str
    kind: str = ""
    status: str = ""
    progress: int = 0
    total: int = 0
    created_at: str = ""
    updated_at: str = ""
    result_json: str = ""


class NamePreviewIn(BaseModel):
    """名称清洗的预演请求。

    urls 为空 = 全库未删除的源。**只读**：预演不改任何数据。
    """
    urls: List[str] = Field(default_factory=list)


class NameChange(BaseModel):
    """一对待应用的改名（apply 传"新名"，undo 传"旧名"）。"""
    url: str
    name: str


class NameApplyIn(BaseModel):
    changes: List[NameChange] = Field(default_factory=list)


class NameUndoIn(BaseModel):
    prev: List[NameChange] = Field(default_factory=list)


class MergeIn(BaseModel):
    """合并一组重复源。判据由后端重算（跨站点 / 规则不同一律拒绝）。"""
    keep: str
    drop: List[str] = Field(default_factory=list)
    merge_tags: bool = True
    merge_comment: bool = False
    dry_run: bool = False


class MergeUndoIn(BaseModel):
    """撤销一次合并——四个字段全部来自 merge 的返回体，不需要调用方自己推算。"""
    keep: str
    restore_urls: List[str] = Field(default_factory=list)
    tags_added: List[str] = Field(default_factory=list)
    prev_comment: str = ""


class AgentPlanRequest(BaseModel):
    """首屏五格决策（现状 / 解决 / 取证 / AI 补足）的输入：**前端已观测到的事实**。

    判据一份在 `core/agent_plan`；这里只转交事实，不收也不产文案——中文句子在前端按
    缺口码取词（`frontend/src/utils/debugDecision.js`，机检扫的是那份）。
    """

    layer: str = ""
    target: Dict[str, Any] = Field(default_factory=dict)
    step: Dict[str, Any] = Field(default_factory=dict)
    page: Dict[str, Any] = Field(default_factory=dict)
    channel: str = ""
    replay: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    runtime: Dict[str, Any] = Field(default_factory=dict)
    network: List[Dict[str, Any]] = Field(default_factory=list)
    model_available: bool = False

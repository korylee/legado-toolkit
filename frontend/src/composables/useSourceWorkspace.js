import { computed, reactive, readonly } from "vue";

export const SOURCE_WORKSPACE_KEY = Symbol("source-workspace");

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

export function createSourceWorkspace() {
  const state = reactive({
    visible: false,
    mode: "debug",
    origin: "list",
    source: null,
    baseSnapshot: null,
    sourceUrl: "",
    initialStep: "",
    initialQuery: "",
    draftRevision: 0,
    savedRevision: 0,
    savedAt: 0,
    //: 草稿的伴随状态。保存动作在壳顶栏（唯一出口），两个编辑器都要读写同一份
    userTags: [],
    statusLocked: false,
    saveAsMode: false,
    //: 已持久化基线的 URL。空 = 草稿还没落过库（新建 / 生成的候选源）——
    //: 另存为入口、保存前的 exists 探测、编辑态「源地址可否改」都以它为基准
    savedUrl: "",
    loading: false,
  });

  const dirty = computed(() => state.draftRevision !== state.savedRevision);
  //: 进调试的前提是「有一份源 + 有源 URL」：新建源在填 URL 之前没有任何可跑的目标，
  //: 放进去只会让工作台拿上一份会话数据冒充这个空源（列表页与编辑入口共用这一条）。
  //: 有草稿时**只认草稿的地址**——另存态把地址腾空后，残留的入口 URL 不得让调试键亮着
  const canDebug = computed(() => {
    const url = state.source
      ? String(state.source.bookSourceUrl || "")
      : String(state.sourceUrl || "");
    return !!url.trim();
  });

  function open(context = {}) {

    const source = clone(context.source || null);
    state.mode = context.mode || "debug";
    state.origin = context.origin || "list";
    state.source = source;
    state.baseSnapshot = clone(source);
    state.sourceUrl = String(context.sourceUrl || source?.bookSourceUrl || "");
    state.initialStep = String(context.initialStep || context.step || "");
    state.initialQuery = String(context.initialQuery || context.query || "");
    state.draftRevision += 1;
    state.savedRevision = context.draft ? state.draftRevision - 1 : state.draftRevision;
    state.userTags = [];
    state.statusLocked = false;
    state.saveAsMode = false;
    state.savedUrl = "";
    state.visible = true;
  }

  //: 壳加载到详情后的落位：成为新的**干净基线**。与 open 不同——不动修订计数、
  //: 不动模式与落点，所以编辑器切走再切回来不会丢草稿
  function replaceSource(source) {

    state.source = clone(source || null);
    state.baseSnapshot = clone(state.source);
    state.sourceUrl = String(state.source?.bookSourceUrl || state.sourceUrl || "");
    state.savedUrl = String(state.source?.bookSourceUrl || "");
    state.savedRevision = state.draftRevision;
  }

  function updateDraft(source) {

    const next = clone(source || null);
    if (JSON.stringify(next) === JSON.stringify(state.source)) return;
    state.source = next;
    state.sourceUrl = String(state.source?.bookSourceUrl || state.sourceUrl || "");
    state.draftRevision += 1;
  }

  function markSaved(source = state.source) {

    state.source = clone(source || null);
    state.baseSnapshot = clone(state.source);
    state.sourceUrl = String(state.source?.bookSourceUrl || state.sourceUrl || "");
    state.savedUrl = String(state.source?.bookSourceUrl || "");
    state.savedRevision = state.draftRevision;
    state.savedAt = Date.now();
    state.saveAsMode = false;
  }

  function setUserTags(tags) {
    state.userTags = [...new Set((tags || []).map((t) => String(t || "").trim()).filter(Boolean))];
  }

  //: 播种基线：把「当前草稿形态」定为干净（编辑器外部替换草稿后，把规范化过的
  //: 表单认作基线用）。**不携带保存语义**——不动 savedUrl / saveAsMode / savedAt，
  //: 那些只属于「落库」这一件事；用 markSaved 顶替的话，另存态刚把地址腾空就会被
  //: 编辑器的重新播种静默清掉（另存按钮消失、快速生成页签回来）。
  function settleDraft() {
    state.savedRevision = state.draftRevision;
  }

  function setStatusLocked(value) {
    state.statusLocked = !!value;
  }

  function setLoading(value) {
    state.loading = !!value;
  }

  //: 另存为：腾空草稿的源地址（另存必须换地址，Legado 以 URL 为主键）。
  //: savedUrl 留作取消还原与保存前 exists 探测的基准
  function startSaveAs() {
    if (!state.savedUrl || !state.source) return;
    state.saveAsMode = true;
    updateDraft({ ...clone(state.source), bookSourceUrl: "" });
  }

  function cancelSaveAs() {
    if (!state.saveAsMode) return;
    state.saveAsMode = false;
    if (state.source && state.savedUrl) {
      updateDraft({ ...clone(state.source), bookSourceUrl: state.savedUrl });
    }
  }

  //: 编辑器交给调试：只换模式与落点，不动草稿与修订——dirty 语义保持。
  //: （调试结果是否有效由 resultRevision 对 draftRevision 判，不在这里清会话）
  function enterDebug(initialStep = "", initialQuery = "") {
    state.initialStep = String(initialStep || "");
    state.initialQuery = String(initialQuery || "");
    state.mode = "debug";
  }

  function setMode(mode) {
    state.mode = mode === "edit" ? "edit" : "debug";
  }

  function discard() {
    state.source = clone(state.baseSnapshot);
    state.sourceUrl = String(state.source?.bookSourceUrl || state.sourceUrl || "");
    state.draftRevision += 1;
    state.savedRevision = state.draftRevision;
    state.saveAsMode = false;
  }

  /**
   * 试关：干净就直接关并返回 true；有未保存改动返回 false，由调用方（壳组件）去问用户。
   *
   * **这里不弹框**：composable 不依赖任何 UI 库（它是纯 node 测试的对象）。
   * 也**不设「待确认」标记**——曾经设过 `closePending` 而没有消费者，结果是
   * 点关闭什么都没发生、也没有提示（用户以为关掉了）。
   */
  function requestClose() {
    if (dirty.value) return false;
    close();
    return true;
  }

  function close() {
    state.visible = false;
  }

  function clear() {
    close();
    state.source = null;
    state.baseSnapshot = null;
    state.sourceUrl = "";
    state.initialStep = "";
    state.initialQuery = "";
    state.draftRevision = 0;
    state.savedRevision = 0;
    state.userTags = [];
    state.statusLocked = false;
    state.saveAsMode = false;
    state.savedUrl = "";
    state.loading = false;
  }

  return {
    state: readonly(state),
    dirty,
    canDebug,
    open,
    replaceSource,
    updateDraft,
    markSaved,
    setUserTags,
    settleDraft,
    setStatusLocked,
    setLoading,
    startSaveAs,
    cancelSaveAs,
    enterDebug,
    setMode,
    discard,
    requestClose,
    close,
    clear,
  };
}

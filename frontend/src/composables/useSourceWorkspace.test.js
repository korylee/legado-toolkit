import test from "node:test";
import assert from "node:assert/strict";
import { createSourceWorkspace } from "./useSourceWorkspace.js";

const source = { bookSourceUrl: "https://example.com", ruleSearch: { bookList: ".item" } };

test("workspace opens a saved source with a clean draft", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ source, origin: "list", mode: "debug" });
  assert.equal(workspace.dirty.value, false);
  assert.equal(workspace.state.source.bookSourceUrl, source.bookSourceUrl);
});

test("workspace marks editor handoff as dirty draft", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ source, origin: "editor", mode: "debug", draft: true });
  assert.equal(workspace.dirty.value, true);
  // 脏着试关：被拦下（返回 false，由壳组件去问用户）
  assert.equal(workspace.requestClose(), false);
  // **但不能顺手把抽屉关掉，也不能留下无人消费的「待确认」标记**——
  // 曾经设过 closePending 而没有消费者，结果是点关闭什么都没发生、也没有提示
  assert.equal(workspace.state.visible, true);
  assert.equal("closePending" in workspace.state, false);
});

test("干净时请求关闭直接关上", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ source, origin: "list", mode: "debug" });
  assert.equal(workspace.requestClose(), true);
  assert.equal(workspace.state.visible, false);
});

test("workspace revisions and save baseline are explicit", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ source });
  const revision = workspace.state.draftRevision;
  workspace.updateDraft({ ...source, ruleSearch: { bookList: ".new" } });
  assert.equal(workspace.state.draftRevision, revision + 1);
  assert.equal(workspace.dirty.value, true);
  workspace.markSaved(workspace.state.source);
  assert.equal(workspace.dirty.value, false);
});

test("workspace discard restores the base snapshot", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ source });
  workspace.updateDraft({ ...source, bookSourceName: "changed" });
  workspace.discard();
  assert.equal(workspace.state.source.bookSourceName, undefined);
  assert.equal(workspace.dirty.value, false);
});

test("新建源在填 URL 之前不能进调试；列表直达调试可以", () => {
  const workspace = createSourceWorkspace();
  // 新建：没有源、也没有 URL —— 工作台放进去只能拿上一份会话数据冒充这个空源
  workspace.open({ origin: "list", mode: "edit", source: null, sourceUrl: "" });
  assert.equal(workspace.canDebug.value, false);
  // 列表直达调试：源对象是异步加载的，只有 URL —— 不能因为 source 还是 null 就拦掉
  workspace.open({ origin: "list", mode: "debug", sourceUrl: "https://example.com" });
  assert.equal(workspace.canDebug.value, true);
  // 编辑草稿带着 URL：可以进
  workspace.open({ origin: "editor", mode: "debug", source, draft: true });
  assert.equal(workspace.canDebug.value, true);
});

test("replaceSource 落位为干净基线，不动修订计数", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ origin: "list", mode: "debug", sourceUrl: "https://example.com" });
  const revision = workspace.state.draftRevision;
  workspace.replaceSource(source);
  assert.equal(workspace.dirty.value, false);
  assert.equal(workspace.state.draftRevision, revision);
  assert.equal(workspace.state.savedUrl, "https://example.com");
  // 编辑一下就脏
  workspace.updateDraft({ ...source, bookSourceName: "changed" });
  assert.equal(workspace.dirty.value, true);
});

test("另存为腾空草稿地址，取消还原；保存后落新基线", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ origin: "list", mode: "edit", sourceUrl: "https://example.com" });
  workspace.replaceSource(source);
  workspace.startSaveAs();
  assert.equal(workspace.state.saveAsMode, true);
  assert.equal(workspace.state.source.bookSourceUrl, "");
  assert.equal(workspace.state.savedUrl, "https://example.com");
  // 地址空了就进不了调试（另存必须先填新地址）
  assert.equal(workspace.canDebug.value, false);
  workspace.cancelSaveAs();
  assert.equal(workspace.state.saveAsMode, false);
  assert.equal(workspace.state.source.bookSourceUrl, "https://example.com");
  // 保存把另存态一并收掉，基线换到新地址
  workspace.startSaveAs();
  workspace.updateDraft({ ...source, bookSourceUrl: "https://other.com" });
  workspace.markSaved(workspace.state.source);
  assert.equal(workspace.state.saveAsMode, false);
  assert.equal(workspace.state.savedUrl, "https://other.com");
  assert.equal(workspace.dirty.value, false);
});

test("enterDebug 只换模式与落点，草稿与修订保持", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ origin: "editor", mode: "edit", source, draft: true });
  const revision = workspace.state.draftRevision;
  workspace.enterDebug("search", "我的");
  assert.equal(workspace.state.mode, "debug");
  assert.equal(workspace.state.initialStep, "search");
  assert.equal(workspace.state.initialQuery, "我的");
  assert.equal(workspace.state.draftRevision, revision);
  assert.equal(workspace.dirty.value, true);
});

test("setUserTags 去重去空", () => {
  const workspace = createSourceWorkspace();
  workspace.setUserTags(["精排", "精排", "", "  跟读  "]);
  assert.deepEqual(workspace.state.userTags, ["精排", "跟读"]);
});

test("settleDraft 只对齐修订，不带保存语义（另存态不能被播种清掉）", () => {
  const workspace = createSourceWorkspace();
  workspace.open({ origin: "list", mode: "edit", sourceUrl: "https://example.com" });
  workspace.replaceSource(source);
  workspace.startSaveAs();
  // 编辑器重新播种外部替换过的草稿：基线对齐，但另存态与基线地址必须原样保留——
  // 它们只属于「落库」这件事（markSaved），否则另存按钮消失、快速生成页签回来
  workspace.updateDraft({ ...source, bookSourceName: "renamed" });
  workspace.settleDraft();
  assert.equal(workspace.dirty.value, false);
  assert.equal(workspace.state.saveAsMode, true);
  assert.equal(workspace.state.savedUrl, "https://example.com");
  // 只有保存才收掉另存态、换基线地址
  workspace.markSaved({ ...source, bookSourceName: "renamed", bookSourceUrl: "https://other.com" });
  assert.equal(workspace.state.saveAsMode, false);
  assert.equal(workspace.state.savedUrl, "https://other.com");
});

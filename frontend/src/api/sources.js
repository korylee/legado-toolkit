import { api } from "./client";

//: 统一构造查询串：空值不进串（后端把空串当作「不筛」）。
//: 两个列表接口共用它——口径分叉的话，「全选」拿到的就不是列表上那批
function toQuery(params = {}) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.append(k, v);
  });
  return q.toString();
}

export function listSources(params = {}) {
  return api.get("/sources?" + toQuery(params));
}

// 「选中全部 N 条筛选结果」的数据来源：当前筛选下的全部 URL，不分页。
// 参数与 listSources 同形，后端也复用同一套 Store._where —— 多传的
// limit/offset/order 会被端点忽略。
export function listSourceUrls(params = {}) {
  return api.get("/sources/urls?" + toQuery(params));
}

export const getDetail = (url) => api.get("/sources/detail?url=" + encodeURIComponent(url));
export const listGroups = () => api.get("/sources/groups");
export const getStats = () => api.get("/sources/stats");
export const saveSource = (source, userTags, lockSystemTags = false) =>
  api.post("/sources/save", { source, user_tags: userTags, lock_system_tags: lockSystemTags });

export const listTags = () => api.get("/sources/tags");

// 系统标签枚举（类型/状态/质量）的定义，唯一来源是后端 core/tags.py。前端不硬编码。
export const getTagsMeta = () => api.get("/sources/tags/meta");
export const patchTags = (urls, add = [], remove = []) =>
  api.post("/sources/tags", { urls, add, remove });
export const renameTag = (oldTag, newTag) =>
  api.post("/sources/tags/rename", { old: oldTag, new: newTag });
export const mergeTags = (sources, target) =>
  api.post("/sources/tags/merge", { sources, target });
export const deleteTag = (tag) => api.post("/sources/tags/delete", { tag });
export const normalizeTags = () => api.post("/sources/tags/normalize");
// 批量软删除。**走 POST body**：旧写法把 URL 列表拼进查询串，实测约 1600 条 / 57KB
// 通过、2000 条 / 72KB 被服务端拒绝，而全库 3850 条约 139KB——「全选全部」正好撞上，
// 表现是请求失败而界面上看不出原因
// 重复源分组（只读）。判据全在后端 core/dups，前端不重算。
export const listDups = (params = {}) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== "" && v != null) q.append(k, v); });
  return api.get("/sources/dups?" + q.toString());
};

// 合并一组重复源。dry_run=true 时只返回「将发生的三件事」，不写库。
export const mergeSources = (body) => api.post("/sources/merge", body);
// 撤销：四个字段全部来自 merge 的返回体，不自己推算
export const undoMerge = (body) => api.post("/sources/merge/undo", body);

// 名称清洗三步：preview 只读出建议 → apply 改名并**返回旧名** → undo 拿旧名回写。
// 撤销不需要另存快照：apply 的返回体本身就是回滚信息（一次会话内有效，刷新即失效）。
export const previewNames = (urls = []) => api.post("/sources/names/preview", { urls });
export const applyNames = (changes) => api.post("/sources/names/apply", { changes });
export const undoNames = (prev) => api.post("/sources/names/undo", { prev });

// reason 会写进 data/backups/deleted.jsonl 的那条删除记录。它是**唯一**记下
// 「为什么删」的地方（库里的 sources 表没有这一列），所以界面别省这一步
export const deleteSources = (urls, reason = "") =>
  api.post("/sources/delete", { urls, reason });

export const listDeleted = (limit = 200, offset = 0) =>
  api.get("/sources/deleted?limit=" + limit + "&offset=" + offset);
// 恢复的粒度是**行 id**，不是 URL：同一个 URL 在回收站里可以有多份历史版本
// （删了再导入时旧版会留在回收站），按 URL 恢复会含糊——恢复哪一份？
// 返回体带 blocked：那些行的 URL 已有在用的版本，要先删掉才能恢复
export const restoreSources = (ids) => api.post("/sources/restore", { ids });

// 清空回收站——**不可逆**（全仓唯一的硬删除路径）。删之前后端会先落一份快照，
// 快照路径随返回体给出，界面上要显示出来
export const purgeTrash = () => api.post("/sources/purge", {});

// 新建书源保存前探测域名是否已存在，避免 upsert_sources 静默覆盖原源规则。
// 必须 encodeURIComponent：域名可能带路径或查询串（如 https://a.com/blog），
// 不编码会把 & 之后的部分当成新的查询参数而截断。
export const sourceExists = (url) =>
  api.get("/sources/exists?url=" + encodeURIComponent(url));

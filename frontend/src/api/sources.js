import { api } from "./client";

export function listSources(params = {}) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.append(k, v);
  });
  return api.get("/sources?" + q.toString());
}

export const getDetail = (url) => api.get("/sources/detail?url=" + encodeURIComponent(url));
export const listGroups = () => api.get("/sources/groups");
export const getStats = () => api.get("/sources/stats");
export const saveSource = (source, userTags, lockSystemTags = false) =>
  api.post("/sources/save", { source, user_tags: userTags, lock_system_tags: lockSystemTags });

export const listTags = () => api.get("/sources/tags");
export const patchTags = (urls, add = [], remove = []) =>
  api.post("/sources/tags", { urls, add, remove });
export const renameTag = (oldTag, newTag) =>
  api.post("/sources/tags/rename", { old: oldTag, new: newTag });
export const mergeTags = (sources, target) =>
  api.post("/sources/tags/merge", { sources, target });
export const deleteTag = (tag) => api.post("/sources/tags/delete", { tag });
export const normalizeTags = () => api.post("/sources/tags/normalize");
export const deleteSources = (urls) => api.del("/sources?urls=" + encodeURIComponent(urls.join(",")));

export const listDeleted = (limit = 200, offset = 0) =>
  api.get("/sources/deleted?limit=" + limit + "&offset=" + offset);
export const restoreSources = (urls) => api.post("/sources/restore", { urls });

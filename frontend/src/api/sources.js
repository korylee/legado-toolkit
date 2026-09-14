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
export const patchGroup = (url, group) => api.patch("/sources/group", { url, group });
export const deleteSources = (urls) => api.del("/sources?urls=" + encodeURIComponent(urls.join(",")));

import { api } from "./client";

export const listImportRepositories = () => api.get("/import/repositories");
export const browseImportRepository = (repositoryId, body) =>
  api.post(
    `/import/repositories/${encodeURIComponent(repositoryId)}/browse`,
    body,
  );
export const listImportRepositoryCollections = (repositoryId, page = 1) =>
  api.get(
    `/import/repositories/${encodeURIComponent(repositoryId)}/collections?page=${page}`,
  );
export const getImportRepositoryCollection = (repositoryId, collection) =>
  api.post(
    `/import/repositories/${encodeURIComponent(repositoryId)}/collection-details`,
    { collection_id: collection.id, collection_url: collection.link },
  );
export const getImportRepositoryDetails = (repositoryId, ids) =>
  api.post(`/import/repositories/${encodeURIComponent(repositoryId)}/details`, {
    ids,
  });

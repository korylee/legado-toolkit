import { api } from "./client";

export const listLLMProfiles = () => api.get("/llm/profiles");
export const listLLMPresets = () => api.get("/llm/presets");
export const getLLMStatus = () => api.get("/llm/status");
export const createLLMProfile = (body) => api.post("/llm/profiles", body);
export const updateLLMProfile = (id, body) => api.patch("/llm/profiles/" + id, body);
export const deleteLLMProfile = (id) => api.del("/llm/profiles/" + id);
export const activateLLMProfile = (id) => api.post("/llm/profiles/" + id + "/activate", {});
export const testLLMProfile = (id) => api.post("/llm/profiles/" + id + "/test", {});
